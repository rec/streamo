import io
import random
from collections.abc import Callable, MutableSequence
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest
from PIL import Image

import streamo.images
from streamo.images import (
    ImageFeed,
    ImageFeedPoller,
    ImageFrameProducer,
    ImageScheduler,
)


class NoShuffleRandom(random.Random):
    def shuffle(
        self,
        x: MutableSequence[object],
        random: Callable[[], float] | None = None,
    ) -> None:
        pass


def test_scheduler_shows_every_image_before_repeating(tmp_path: Path) -> None:
    paths = [tmp_path / f"{n}.png" for n in "abc"]
    for path in paths:
        path.touch()
    scheduler = ImageScheduler(tmp_path, random.Random(3))

    first_cycle = [scheduler.next_image() for _ in paths]
    repeated = scheduler.next_image()

    assert set(first_cycle) == set(paths)
    assert repeated in paths


def test_scheduler_prioritizes_new_images(tmp_path: Path) -> None:
    paths = [tmp_path / f"{n}.png" for n in "abc"]
    for path in paths:
        path.touch()
    scheduler = ImageScheduler(tmp_path, random.Random(4))
    shown = scheduler.next_image()
    new = tmp_path / "new.png"
    new.touch()

    selected = scheduler.next_image()

    assert shown in paths
    assert selected == new


def test_scheduler_shows_unseen_session_images_before_old_images(
    tmp_path: Path,
) -> None:
    old = tmp_path / "old.png"
    old.touch()
    scheduler = ImageScheduler(tmp_path, NoShuffleRandom())
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.touch()
    second.touch()

    assert scheduler.next_image() == first
    assert scheduler.next_image() == second


def test_scheduler_weights_seen_session_images_against_old_images(
    tmp_path: Path,
) -> None:
    old = tmp_path / "old.png"
    old.touch()
    scheduler = ImageScheduler(tmp_path, NoShuffleRandom(), session_weight=2)
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.touch()
    second.touch()

    selected = [scheduler.next_image() for _ in range(5)]

    assert selected == [first, second, first, second, old]


def test_scheduler_with_zero_session_weight_still_shows_new_image_first(
    tmp_path: Path,
) -> None:
    old = tmp_path / "old.png"
    old.touch()
    scheduler = ImageScheduler(tmp_path, NoShuffleRandom(), session_weight=0)
    current = tmp_path / "current.png"
    current.touch()

    assert scheduler.next_image() == current
    assert scheduler.next_image() == old


def test_scheduler_treats_recreated_path_as_new(tmp_path: Path) -> None:
    for name in "abc":
        (tmp_path / f"{name}.png").touch()
    scheduler = ImageScheduler(tmp_path, NoShuffleRandom())
    first = scheduler.next_image()
    assert first is not None
    first.unlink()
    scheduler.next_image()
    first.touch()

    assert scheduler.next_image() == first


def test_frame_producer_scales_and_centers_image(tmp_path: Path) -> None:
    path = tmp_path / "green.png"
    Image.new("RGBA", (1, 2), (0, 255, 0, 255)).save(path)
    producer = ImageFrameProducer(
        ImageScheduler(tmp_path),
        width=4,
        height=2,
        frame_rate=1,
        interval=2,
        duration=1,
        fade=0,
    )

    frame = np.frombuffer(next(producer.frames()), dtype=np.uint8).reshape((2, 4, 4))

    assert frame[:, :, 3].tolist() == [[0, 255, 0, 0], [0, 255, 0, 0]]


def test_frame_producer_fades_and_then_emits_transparency(tmp_path: Path) -> None:
    Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(tmp_path / "red.png")
    producer = ImageFrameProducer(
        ImageScheduler(tmp_path),
        width=1,
        height=1,
        frame_rate=4,
        interval=3,
        duration=2,
        fade=1,
    )
    frames = producer.frames()

    alpha = [next(frames)[3] for _ in range(12)]

    assert alpha == [0, 64, 128, 191, 255, 191, 128, 64, 0, 0, 0, 0]


def test_frame_producer_skips_invalid_images(tmp_path: Path) -> None:
    (tmp_path / "bad.png").write_text("not an image")
    Image.new("RGBA", (1, 1), (0, 0, 255, 255)).save(tmp_path / "good.png")
    producer = ImageFrameProducer(
        ImageScheduler(tmp_path, NoShuffleRandom()),
        width=1,
        height=1,
        frame_rate=1,
        interval=2,
        duration=1,
        fade=0,
    )

    assert next(producer.frames()) == bytes((0, 0, 255, 255))


def test_image_feed_poller_downloads_new_jpegs_and_records_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_dir = tmp_path / "images"
    jpeg = io.BytesIO()
    Image.new("RGB", (8, 6), "blue").save(jpeg, "JPEG")
    requests: list[tuple[str, dict[str, list[str]]]] = []

    def urlopen(url: str, timeout: int) -> FakeHttpResponse:
        query = parse_qs(urlsplit(url).query)
        requests.append((urlsplit(url).path, query))
        assert timeout == 10
        if query["action"] == ["feed"]:
            return FakeHttpResponse(b'{"id":1}\n{"id":2}\n')
        return FakeHttpResponse(jpeg.getvalue())

    monkeypatch.setattr(streamo.images, "urlopen", urlopen)
    poller = ImageFeedPoller(
        ImageFeed(
            url="https://ax.to/show/photos.php",
            token="room-secret-at-least-20-characters",
        ),
        image_dir,
    )

    stored = poller.poll()

    assert stored == [
        image_dir / f"remote-{poller.feed_id}-00000001.jpg",
        image_dir / f"remote-{poller.feed_id}-00000002.jpg",
    ]
    assert all(p.read_bytes() == jpeg.getvalue() for p in stored)
    assert poller.cursor_path.read_text() == "2\n"
    assert [q["action"] for _, q in requests] == [["feed"], ["image"], ["image"]]
    assert all(
        q["token"] == ["room-secret-at-least-20-characters"] for _, q in requests
    )


def test_image_feed_poller_requests_only_items_after_saved_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_dir = tmp_path / "images"
    feed = ImageFeed(
        url="https://ax.to/show/photos.php",
        token="room-secret-at-least-20-characters",
    )
    poller = ImageFeedPoller(feed, image_dir)
    image_dir.mkdir()
    poller.cursor_path.write_text("12\n")

    def urlopen(url: str, timeout: int) -> FakeHttpResponse:
        assert parse_qs(urlsplit(url).query)["after"] == ["12"]
        return FakeHttpResponse(b"")

    monkeypatch.setattr(streamo.images, "urlopen", urlopen)

    assert poller.poll() == []


class FakeHttpResponse:
    def __init__(self, contents: bytes) -> None:
        self.contents = contents

    def __enter__(self) -> "FakeHttpResponse":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        pass

    def read(self, limit: int) -> bytes:
        return self.contents[:limit]
