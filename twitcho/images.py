import random
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

import numpy as np
from PIL import Image, UnidentifiedImageError
from reccy.runtime.logging import get_logger

LOGGER = get_logger(__name__)


class ImageScheduler:
    def __init__(
        self, image_dir: Path, randomizer: random.Random | None = None
    ) -> None:
        self.image_dir = image_dir
        self.randomizer = randomizer or random.Random()
        self.known: set[Path] = set()
        self.pending: list[Path] = []

    def next_image(self) -> Path | None:
        current = set(image_paths(self.image_dir))
        new = sorted(current - self.known)
        self.known = current
        self.pending = [p for p in self.pending if p in current]

        if new:
            self.randomizer.shuffle(new)
            self.pending = new + self.pending
        if not self.pending and current:
            self.pending = sorted(current)
            self.randomizer.shuffle(self.pending)
        if not self.pending:
            return None

        image = self.pending.pop(0)
        return image


class ImageFrameProducer:
    def __init__(
        self,
        scheduler: ImageScheduler,
        *,
        width: int,
        height: int,
        frame_rate: int,
        interval: float,
        duration: float,
        fade: float,
    ) -> None:
        self.scheduler = scheduler
        self.width = width
        self.height = height
        self.frame_rate = frame_rate
        self.duration = duration
        self.fade = fade
        self.interval_frames = max(1, round(interval * frame_rate))
        self.transparent = bytes(width * height * 4)

    def frames(self) -> Iterator[bytes]:
        while True:
            image = self.next_frame_image()
            for index in range(self.interval_frames):
                opacity = self.opacity(index)
                if image is None or opacity <= 0:
                    yield self.transparent
                else:
                    yield faded_frame(image, opacity)

    def next_frame_image(self) -> np.ndarray | None:
        attempted: set[Path] = set()
        while (path := self.scheduler.next_image()) is not None:
            if path in attempted:
                return None
            attempted.add(path)
            try:
                return load_image(path, self.width, self.height)
            except (OSError, UnidentifiedImageError) as error:
                LOGGER.error("Could not load image %s: %s", path, error)
        return None

    def opacity(self, frame: int) -> float:
        elapsed = frame / self.frame_rate
        if elapsed >= self.duration:
            return 0.0
        if self.fade == 0:
            return 1.0
        return max(
            0.0,
            min(1.0, elapsed / self.fade, (self.duration - elapsed) / self.fade),
        )


def write_image_frames(stream: BinaryIO, producer: ImageFrameProducer) -> None:
    try:
        for frame in producer.frames():
            stream.write(frame)
    except BrokenPipeError:
        pass
    finally:
        stream.close()


def image_paths(image_dir: Path) -> list[Path]:
    if not image_dir.exists():
        return []
    return sorted(
        p
        for p in image_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )


def load_image(path: Path, width: int, height: int) -> np.ndarray:
    with Image.open(path) as source:
        image = source.convert("RGBA")
    image.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (width, height))
    position = ((width - image.width) // 2, (height - image.height) // 2)
    canvas.alpha_composite(image, position)
    return np.asarray(canvas, dtype=np.uint8)


def faded_frame(image: np.ndarray, opacity: float) -> bytes:
    if opacity >= 1:
        return image.tobytes()
    frame = image.copy()
    alpha = frame[:, :, 3].astype(np.uint16)
    frame[:, :, 3] = (alpha * round(opacity * 255) // 255).astype(np.uint8)
    return frame.tobytes()


IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
