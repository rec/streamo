import io
import random
import threading
from collections.abc import Callable, MutableSequence
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest
from PIL import Image

import streamo.image_feed
import streamo.images
from streamo.image_feed import ImageFeed, ImageFeedPoller
from streamo.images import ImageFrameProducer, ImageScheduler


class NoShuffleRandom(random.Random):
    def shuffle(
        self,
        x: MutableSequence[object],
        random: Callable[[], float] | None = None,
    ) -> None:
        pass


def test_feed_stop_waits_for_inflight_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed = ImageFeed(url='https://example.test/foto.php', token='x' * 20)
    poller = ImageFeedPoller(feed, tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def poll() -> list[Path]:
        entered.set()
        assert release.wait(5)
        return []

    monkeypatch.setattr(poller, 'poll', poll)
    poller.start()
    assert entered.wait(5)
    stopped = threading.Event()
    closer = threading.Thread(target=lambda: (poller.stop(), stopped.set()))
    closer.start()
    assert not stopped.wait(0.05)
    release.set()
    assert stopped.wait(5)
    closer.join()


def test_invalid_oversized_image_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / 'bad.png'
    good = tmp_path / 'good.png'
    bad.touch()
    good.touch()
    scheduler = ImageScheduler(tmp_path, NoShuffleRandom(), initial_paths=set())
    producer = ImageFrameProducer(
        scheduler,
        width=16,
        height=16,
        frame_rate=10,
        interval=2,
        duration=1,
        fade=0,
    )

    def load(path: Path, width: int, height: int) -> np.ndarray:
        if path == bad:
            raise Image.DecompressionBombError('oversized')
        return np.zeros((height, width, 4), dtype=np.uint8)

    monkeypatch.setattr(streamo.images, 'load_image', load)
    assert producer.next_frame_image() is not None
    assert producer.current_path == good


def test_scheduler_shows_every_image_before_repeating(tmp_path: Path) -> None:
    paths = [tmp_path / f'{n}.png' for n in 'abc']
    for path in paths:
        path.touch()
    scheduler = ImageScheduler(tmp_path, random.Random(3))

    first_cycle = [scheduler.next_image() for _ in paths]
    repeated = scheduler.next_image()

    assert set(first_cycle) == set(paths)
    assert repeated in paths


def test_scheduler_prioritizes_new_images(tmp_path: Path) -> None:
    paths = [tmp_path / f'{n}.png' for n in 'abc']
    for path in paths:
        path.touch()
    scheduler = ImageScheduler(tmp_path, random.Random(4))
    shown = scheduler.next_image()
    new = tmp_path / 'new.png'
    new.touch()

    selected = scheduler.next_image()

    assert shown in paths
    assert selected == new


def test_scheduler_shows_unseen_session_images_before_old_images(
    tmp_path: Path,
) -> None:
    old = tmp_path / 'old.png'
    old.touch()
    scheduler = ImageScheduler(tmp_path, NoShuffleRandom())
    first = tmp_path / 'first.png'
    second = tmp_path / 'second.png'
    first.touch()
    second.touch()

    assert scheduler.next_image() == first
    assert scheduler.next_image() == second


def test_scheduler_weights_seen_session_images_against_old_images(
    tmp_path: Path,
) -> None:
    old = tmp_path / 'old.png'
    old.touch()
    scheduler = ImageScheduler(tmp_path, NoShuffleRandom(), session_weight=2)
    first = tmp_path / 'first.png'
    second = tmp_path / 'second.png'
    first.touch()
    second.touch()

    selected = [scheduler.next_image() for _ in range(5)]

    assert selected == [first, second, first, second, old]


def test_scheduler_with_zero_session_weight_still_shows_new_image_first(
    tmp_path: Path,
) -> None:
    old = tmp_path / 'old.png'
    old.touch()
    scheduler = ImageScheduler(tmp_path, NoShuffleRandom(), session_weight=0)
    current = tmp_path / 'current.png'
    current.touch()

    assert scheduler.next_image() == current
    assert scheduler.next_image() == old


def test_scheduler_treats_recreated_path_as_new(tmp_path: Path) -> None:
    for name in 'abc':
        (tmp_path / f'{name}.png').touch()
    scheduler = ImageScheduler(tmp_path, NoShuffleRandom())
    first = scheduler.next_image()
    assert first is not None
    first.unlink()
    scheduler.next_image()
    first.touch()

    assert scheduler.next_image() == first


def test_frame_producer_scales_and_centers_image(tmp_path: Path) -> None:
    path = tmp_path / 'green.png'
    Image.new('RGBA', (1, 2), (0, 255, 0, 255)).save(path)
    producer = ImageFrameProducer(
        ImageScheduler(tmp_path),
        width=4,
        height=2,
        frame_rate=1,
        interval=2,
        duration=1,
        fade=0,
    )

    frame = np.frombuffer(producer.frame(), dtype=np.uint8).reshape((2, 4, 4))

    assert frame[:, :, 3].tolist() == [[0, 255, 0, 0], [0, 255, 0, 0]]


def test_frame_producer_fades_and_then_emits_transparency(tmp_path: Path) -> None:
    Image.new('RGBA', (1, 1), (255, 0, 0, 255)).save(tmp_path / 'red.png')
    producer = ImageFrameProducer(
        ImageScheduler(tmp_path),
        width=1,
        height=1,
        frame_rate=4,
        interval=3,
        duration=2,
        fade=1,
    )

    alpha = [producer.frame()[3] for _ in range(12)]

    assert alpha == [0, 64, 128, 191, 255, 191, 128, 64, 0, 0, 0, 0]


def test_frame_producer_skips_invalid_images(tmp_path: Path) -> None:
    (tmp_path / 'bad.png').write_text('not an image')
    Image.new('RGBA', (1, 1), (0, 0, 255, 255)).save(tmp_path / 'good.png')
    producer = ImageFrameProducer(
        ImageScheduler(tmp_path, NoShuffleRandom()),
        width=1,
        height=1,
        frame_rate=1,
        interval=2,
        duration=1,
        fade=0,
    )

    assert producer.frame() == bytes((0, 0, 255, 255))


def test_image_feed_poller_downloads_new_jpegs_and_records_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_dir = tmp_path / 'images'
    jpeg = io.BytesIO()
    Image.new('RGB', (8, 6), 'blue').save(jpeg, 'JPEG')
    requests: list[tuple[str, dict[str, list[str]]]] = []

    def urlopen(url: str, timeout: int) -> FakeHttpResponse:
        query = parse_qs(urlsplit(url).query)
        requests.append((urlsplit(url).path, query))
        assert timeout == 10
        if query['action'] == ['feed']:
            return FakeHttpResponse(b'{"id":1}\n{"id":2}\n')
        return FakeHttpResponse(jpeg.getvalue())

    monkeypatch.setattr(streamo.image_feed, 'urlopen', urlopen)
    poller = ImageFeedPoller(
        ImageFeed(
            url='https://ax.to/show/photos.php',
            token='room-secret-at-least-20-characters',
        ),
        image_dir,
    )

    stored = poller.poll()

    assert stored == [
        image_dir / f'remote-{poller.feed_id}-00000001.jpg',
        image_dir / f'remote-{poller.feed_id}-00000002.jpg',
    ]
    assert all(p.read_bytes() == jpeg.getvalue() for p in stored)
    assert poller.cursor_path.read_text() == '2\n'
    assert [q['action'] for _, q in requests] == [['feed'], ['image'], ['image']]
    assert all(
        q['token'] == ['room-secret-at-least-20-characters'] for _, q in requests
    )


def test_image_feed_poller_requests_only_items_after_saved_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_dir = tmp_path / 'images'
    feed = ImageFeed(
        url='https://ax.to/show/photos.php',
        token='room-secret-at-least-20-characters',
    )
    poller = ImageFeedPoller(feed, image_dir)
    image_dir.mkdir()
    poller.cursor_path.write_text('12\n')

    def urlopen(url: str, timeout: int) -> FakeHttpResponse:
        assert parse_qs(urlsplit(url).query)['after'] == ['12']
        return FakeHttpResponse(b'')

    monkeypatch.setattr(streamo.image_feed, 'urlopen', urlopen)

    assert poller.poll() == []


def test_invalid_feed_image_does_not_block_later_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jpeg = io.BytesIO()
    Image.new('RGB', (8, 8), 'blue').save(jpeg, 'JPEG')

    def urlopen(url: str, timeout: int) -> FakeHttpResponse:
        query = parse_qs(urlsplit(url).query)
        if query['action'] == ['feed']:
            return FakeHttpResponse(b'{"id":1}\n{"id":2}\n')
        return FakeHttpResponse(b'bad' if query['id'] == ['1'] else jpeg.getvalue())

    monkeypatch.setattr(streamo.image_feed, 'urlopen', urlopen)
    poller = ImageFeedPoller(
        ImageFeed(
            url='https://example.test/photos',
            token='room-secret-at-least-20-characters',
        ),
        tmp_path,
    )
    paths = poller.poll()
    assert len(paths) == 1
    assert paths[0].read_bytes() == jpeg.getvalue()
    assert poller.cursor_path.read_text() == '2\n'


class FakeHttpResponse:
    def __init__(self, contents: bytes) -> None:
        self.contents = contents

    def __enter__(self) -> 'FakeHttpResponse':
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        pass

    def read(self, limit: int) -> bytes:
        return self.contents[:limit]


def test_paused_fade_and_skip_preserve_next_selection(tmp_path: Path) -> None:
    for name, color in (('a.png', 'red'), ('b.png', 'blue')):
        Image.new('RGBA', (1, 1), color).save(tmp_path / name)
    producer = ImageFrameProducer(
        ImageScheduler(tmp_path, NoShuffleRandom()),
        width=1,
        height=1,
        frame_rate=4,
        interval=3,
        duration=2,
        fade=1,
    )
    producer.frame()
    held = producer.frame()
    assert held == bytes((255, 0, 0, 64))
    assert producer.snapshot()['next_id'] == 'trusted/1/b.png'
    producer.paused = True
    for _ in range(20):
        assert producer.frame() == held
    producer.skip()
    assert producer.frame() == bytes(4)
    assert producer.snapshot()['next_id'] == 'trusted/1/b.png'
    producer.paused = False
    for _ in range(10):
        assert producer.frame() == bytes(4)
    producer.frame()  # Next interval starts at zero opacity.
    assert producer.frame() == bytes((0, 0, 255, 64))
    assert (tmp_path / 'a.png').exists()


def test_next_selection_is_validated_and_rechecked_before_display(
    tmp_path: Path,
) -> None:
    from streamo.moderation import ImageApproval, ImageReview

    for name in ('a.png', 'b.png', 'c.png'):
        Image.new('RGBA', (1, 1), 'red').save(tmp_path / name)
    (tmp_path / 'invalid.png').write_text('not an image')
    approval = ImageApproval(tmp_path, required=False)
    producer = ImageFrameProducer(
        ImageScheduler(tmp_path, NoShuffleRandom(), approval=approval),
        width=1,
        height=1,
        frame_rate=1,
        interval=2,
        duration=1,
        fade=0,
    )
    producer.frame()
    producer.select_next(tmp_path / 'c.png')
    with pytest.raises(OSError):
        producer.select_next(tmp_path / 'invalid.png')
    assert producer.snapshot()['next_id'] == 'incoming/c.png'
    approval.review(ImageReview(id='incoming/c.png', decision='rejected'))
    assert producer.snapshot()['next_id'] is None
    with pytest.raises(ValueError, match='approved'):
        producer.select_next(tmp_path / 'c.png')
    producer.frame()
    producer.frame()
    assert producer.visible_id != 'incoming/c.png'
    producer.select_next(tmp_path / 'b.png')
    (tmp_path / 'b.png').unlink()
    assert producer.snapshot()['next_id'] is None
    producer.frame()
    producer.frame()
    assert producer.visible_id == 'incoming/a.png'


def test_atomic_image_is_invisible_to_rotation_and_removal_until_published(
    tmp_path: Path,
) -> None:
    from reccy.runtime.files import atomic_output

    from streamo.control import remove_last_image
    from streamo.images import image_paths

    path = tmp_path / 'photo.png'
    scheduler = ImageScheduler(tmp_path)
    with atomic_output(path) as temporary:
        Image.new('RGBA', (1, 1), 'red').save(temporary)
        assert temporary.suffix == '.png'
        assert image_paths(tmp_path) == []
        assert scheduler.next_image() is None
        assert remove_last_image(tmp_path) is None
        assert temporary.exists()
    assert image_paths(tmp_path) == [path]
    assert scheduler.next_image() == path


def test_scheduler_selects_directories_by_weight(tmp_path: Path) -> None:
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    (first / 'one.png').touch()
    (second / 'two.png').touch()

    class DirectoryRandom(NoShuffleRandom):
        def __init__(self) -> None:
            super().__init__()
            self.selections = iter([0, 3])

        def randrange(self, stop: int) -> int:
            if stop == 1:
                return 0
            assert stop == 4
            return next(self.selections)

    scheduler = ImageScheduler(
        [first, second], DirectoryRandom(), directory_weights=[3, 1]
    )

    assert scheduler.next_image() == first / 'one.png'
    assert scheduler.next_image() == second / 'two.png'


def test_scheduler_applies_directory_weights_to_new_images(tmp_path: Path) -> None:
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    first.mkdir()
    second.mkdir()

    class DirectoryRandom(NoShuffleRandom):
        def __init__(self) -> None:
            super().__init__()
            self.selections = iter([0, 3])

        def randrange(self, stop: int) -> int:
            if stop == 1:
                return 0
            assert stop == 4
            return next(self.selections)

    scheduler = ImageScheduler(
        [first, second], DirectoryRandom(), directory_weights=[3, 1]
    )
    (first / 'one.png').touch()
    (second / 'two.png').touch()

    assert scheduler.next_image() == first / 'one.png'
    assert scheduler.next_image() == second / 'two.png'
