import random
from collections.abc import Callable, MutableSequence
from pathlib import Path

import numpy as np
from PIL import Image

from streamo.images import ImageFrameProducer, ImageScheduler


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
