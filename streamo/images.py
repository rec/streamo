import random
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image, UnidentifiedImageError
from reccy.runtime.logging import get_logger

LOGGER = get_logger(__name__)

if TYPE_CHECKING:
    from .moderation import ImageApproval


class ImageScheduler:
    def __init__(
        self,
        image_dirs: list[Path] | Path,
        randomizer: random.Random | None = None,
        *,
        initial_paths: set[Path] | None = None,
        session_weight: int = 3,
        approval: 'ImageApproval | None' = None,
        directory_weights: list[int] | None = None,
    ) -> None:
        self.image_dirs = [image_dirs] if isinstance(image_dirs, Path) else image_dirs
        self.approval = approval
        self.trusted_dirs = [
            d for d in self.image_dirs if approval is None or d != approval.image_dir
        ]
        self.randomizer = randomizer or random.Random()
        self.known = (
            {p for d in self.image_dirs for p in image_paths(d)}
            if initial_paths is None
            else set(initial_paths)
        )
        self.session_paths: set[Path] = set()
        self.unseen_session: dict[Path, list[Path]] = {}
        self.session_pending: dict[Path, list[Path]] = {}
        self.archive_pending: dict[Path, list[Path]] = {}
        self.session_weight = session_weight
        self.session_remaining = session_weight
        self.directory_weights = (
            directory_weights
            if directory_weights is not None
            else list(range(len(self.image_dirs), 0, -1))
        )

    def next_image(self) -> Path | None:
        current = {p for d in self.image_dirs for p in image_paths(d)}
        if self.approval is not None:
            current = {p for p in current if self.approval.allows(p)}
        new_session = sorted(current - self.known)
        self.known = current
        self.session_paths.intersection_update(current)
        self.unseen_session = self.pending_paths(self.unseen_session, current)
        self.session_pending = self.pending_paths(self.session_pending, current)
        self.archive_pending = self.pending_paths(self.archive_pending, current)
        if new_session:
            self.randomizer.shuffle(new_session)
            self.session_paths.update(new_session)
            for directory in self.image_dirs:
                paths = [p for p in new_session if p.parent == directory]
                if paths:
                    self.unseen_session.setdefault(directory, []).extend(paths)
        if unseen := {p for paths in self.unseen_session.values() for p in paths}:
            return self.next_from(unseen, self.unseen_session)
        if not current:
            return None
        session = self.session_paths & current
        archive = current - session
        if session and (not archive or self.session_remaining > 0):
            self.session_remaining = max(0, self.session_remaining - 1)
            return self.next_from(session, self.session_pending)
        self.session_remaining = self.session_weight
        return self.next_from(archive, self.archive_pending)

    def id_for(self, path: Path) -> str:
        if self.approval is not None and path.parent == self.approval.image_dir:
            return f'incoming/{path.name}'
        return f'trusted/{self.trusted_dirs.index(path.parent) + 1}/{path.name}'

    def path_for_id(self, image_id: str) -> Path:
        parts = image_id.split('/')
        if len(parts) == 2 and parts[0] == 'incoming' and self.approval is not None:
            directory = self.approval.image_dir
            name = parts[1]
        elif len(parts) == 3 and parts[0] == 'trusted' and parts[1].isdigit():
            index = int(parts[1]) - 1
            if index < 0 or index >= len(self.trusted_dirs):
                raise ValueError('Unknown image directory in ID')
            directory = self.trusted_dirs[index]
            name = parts[2]
        else:
            raise ValueError('Image ID must identify a trusted or incoming image')
        if name in {'', '.', '..'} or Path(name).name != name:
            raise ValueError('Image ID must contain a filename')
        return directory / name

    def next_from(self, paths: set[Path], pending: dict[Path, list[Path]]) -> Path:
        directories = [d for d in self.image_dirs if any(p.parent == d for p in paths)]
        weights = [
            self.directory_weights[self.image_dirs.index(d)] for d in directories
        ]
        directory = self.choose_directory(directories, weights)
        directory_paths = {p for p in paths if p.parent == directory}
        directory_pending = pending.setdefault(directory, [])
        if not directory_pending:
            directory_pending.extend(sorted(directory_paths))
            self.randomizer.shuffle(directory_pending)
        return directory_pending.pop(0)

    def choose_directory(self, directories: list[Path], weights: list[int]) -> Path:
        selection = self.randomizer.randrange(sum(weights))
        for directory, weight in zip(directories, weights, strict=True):
            if selection < weight:
                return directory
            selection -= weight
        raise AssertionError('directory selection exceeded configured weights')

    @staticmethod
    def pending_paths(
        pending: dict[Path, list[Path]], current: set[Path]
    ) -> dict[Path, list[Path]]:
        return {
            directory: [p for p in paths if p in current]
            for directory, paths in pending.items()
        }


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
        self.current_path: Path | None = None
        self.current_image: np.ndarray | None = None
        self.next_path: Path | None = None
        self.paused = False
        self.frame_index = -1
        self.visible_id: str | None = None

    def frame(self) -> bytes:
        if not self.paused:
            self.frame_index = (self.frame_index + 1) % self.interval_frames
            if self.frame_index == 0:
                self.current_image = self.next_frame_image()
                self.next_path = self.scheduler.next_image()
        opacity = self.opacity(self.frame_index)
        if (
            self.current_image is None
            or opacity <= 0
            or not self.eligible(self.current_path)
        ):
            self.visible_id = None
            return self.transparent
        assert self.current_path is not None
        self.visible_id = self.scheduler.id_for(self.current_path)
        return faded_frame(self.current_image, opacity)

    def skip(self) -> None:
        self.current_image = None
        self.current_path = None
        self.visible_id = None

    def select_next(self, path: Path) -> None:
        if not self.eligible(path):
            raise ValueError('Next image must exist and be approved')
        # Decode before replacing a pending choice; invalid files preserve it.
        load_image(path, self.width, self.height)
        self.next_path = path

    def snapshot(self) -> dict[str, object]:
        return {
            'paused': self.paused,
            'next_id': self.scheduler.id_for(self.next_path)
            if self.eligible(self.next_path) and self.next_path
            else None,
        }

    def eligible(self, path: Path | None) -> bool:
        return (
            path is not None
            and path.is_file()
            and (
                self.scheduler.approval is None or self.scheduler.approval.allows(path)
            )
        )

    def next_frame_image(self) -> np.ndarray | None:
        self.current_path = None
        attempted: set[Path] = set()
        path = (
            self.next_path
            if self.eligible(self.next_path)
            else self.scheduler.next_image()
        )
        self.next_path = None
        while path is not None:
            if path in attempted:
                return None
            attempted.add(path)
            try:
                image = load_image(path, self.width, self.height)
                self.current_path = path
                return image
            except (
                OSError,
                UnidentifiedImageError,
                Image.DecompressionBombError,
            ) as error:
                LOGGER.error('Could not load image %s: %s', path, error)
            path = self.scheduler.next_image()
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


def image_paths(image_dir: Path) -> list[Path]:
    if not image_dir.exists():
        return []
    return sorted(
        p
        for p in image_dir.iterdir()
        if not p.name.startswith('.')
        and p.is_file()
        and p.suffix.lower() in IMAGE_SUFFIXES
    )


def load_image(path: Path, width: int, height: int) -> np.ndarray:
    with Image.open(path) as source:
        image = source.convert('RGBA')
    image.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new('RGBA', (width, height))
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


IMAGE_SUFFIXES = {'.gif', '.jpeg', '.jpg', '.png', '.webp'}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_SIDE = 2048
