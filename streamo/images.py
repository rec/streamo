import hashlib
import io
import json
import random
import tempfile
import threading
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

import numpy as np
from PIL import Image, UnidentifiedImageError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)
from reccy.runtime.logging import get_logger

LOGGER = get_logger(__name__)


class ImageFeed(BaseModel, frozen=True):
    url: str
    token: SecretStr = Field(min_length=20)
    poll_interval: float = Field(default=2.0, ge=0.5)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.hostname is None:
            raise ValueError("image feed URL must use HTTPS")
        return value

    model_config = ConfigDict(hide_input_in_errors=True)


class ImageFeedItem(BaseModel, frozen=True):
    id: int = Field(gt=0)


class ImageFeedError(ValueError):
    pass


class ImageFeedPoller:
    def __init__(self, feed: ImageFeed, image_dir: Path) -> None:
        self.feed = feed
        self.image_dir = image_dir
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        identity = f"{feed.url}\0{feed.token.get_secret_value()}".encode()
        digest = hashlib.sha256(identity).hexdigest()[:12]
        self.feed_id = digest
        self.cursor_path = image_dir / f".streamo-image-feed-{digest}.cursor"

    def start(self) -> None:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.thread = threading.Thread(
            target=self.run,
            name="StreamoImageFeed",
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=15)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.poll()
            except (ImageFeedError, OSError) as error:
                host = urlsplit(self.feed.url).hostname or "image feed"
                LOGGER.error("Could not poll image feed at %s: %s", host, error)
            self.stop_event.wait(self.feed.poll_interval)

    def poll(self) -> list[Path]:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        cursor = read_feed_cursor(self.cursor_path)
        items = fetch_feed_items(self.feed, cursor)
        stored: list[Path] = []
        for item in items:
            if self.stop_event.is_set():
                break
            if item.id <= cursor:
                continue
            contents = fetch_feed_image(self.feed, item.id)
            validate_feed_image(contents)
            target = self.image_dir / f"remote-{self.feed_id}-{item.id:08}.jpg"
            publish_file(target, contents)
            write_feed_cursor(self.cursor_path, item.id)
            cursor = item.id
            stored.append(target)
        return stored


class ImageScheduler:
    def __init__(
        self,
        image_dir: Path,
        randomizer: random.Random | None = None,
        *,
        initial_paths: set[Path] | None = None,
        session_weight: int = 3,
    ) -> None:
        self.image_dir = image_dir
        self.randomizer = randomizer or random.Random()
        self.known = (
            set(image_paths(image_dir)) if initial_paths is None else set(initial_paths)
        )
        self.session_paths: set[Path] = set()
        self.unseen_session: list[Path] = []
        self.session_pending: list[Path] = []
        self.archive_pending: list[Path] = []
        self.session_weight = session_weight
        self.session_remaining = session_weight

    def next_image(self) -> Path | None:
        current = set(image_paths(self.image_dir))
        new_session = sorted(current - self.known)
        self.known = current
        self.session_paths.intersection_update(current)
        self.unseen_session = [p for p in self.unseen_session if p in current]
        self.session_pending = [p for p in self.session_pending if p in current]
        self.archive_pending = [p for p in self.archive_pending if p in current]
        if new_session:
            self.randomizer.shuffle(new_session)
            self.session_paths.update(new_session)
            self.unseen_session = new_session + self.unseen_session
        if self.unseen_session:
            return self.unseen_session.pop(0)
        if not current:
            return None
        session = self.session_paths & current
        archive = current - session
        if session and (not archive or self.session_remaining > 0):
            self.session_remaining = max(0, self.session_remaining - 1)
            return self.next_from(session, self.session_pending)
        self.session_remaining = self.session_weight
        return self.next_from(archive, self.archive_pending)

    def next_from(self, paths: set[Path], pending: list[Path]) -> Path:
        if not pending:
            pending.extend(sorted(paths))
            self.randomizer.shuffle(pending)
        return pending.pop(0)


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


def fetch_feed_items(feed: ImageFeed, after: int) -> list[ImageFeedItem]:
    try:
        url = feed_request_url(feed, "feed", after=after)
        with urlopen(url, timeout=10) as response:
            contents = response.read(MAX_FEED_BYTES + 1)
    except (HTTPError, OSError, URLError) as error:
        raise ImageFeedError(f"feed request failed ({type(error).__name__})") from error
    if len(contents) > MAX_FEED_BYTES:
        raise ImageFeedError("feed response is too large")
    try:
        items = [
            ImageFeedItem.model_validate(json.loads(line))
            for line in contents.decode().splitlines()
            if line.strip()
        ]
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
        raise ImageFeedError("feed returned invalid JSON Lines") from error
    if any(a.id >= b.id for a, b in pairwise(items)):
        raise ImageFeedError("feed item IDs are not increasing")
    return items


def fetch_feed_image(feed: ImageFeed, image_id: int) -> bytes:
    try:
        with urlopen(
            feed_request_url(feed, "image", id=image_id), timeout=10
        ) as response:
            contents = response.read(MAX_IMAGE_BYTES + 1)
    except (HTTPError, OSError, URLError) as error:
        raise ImageFeedError(
            f"image {image_id} request failed ({type(error).__name__})"
        ) from error
    if len(contents) > MAX_IMAGE_BYTES:
        raise ImageFeedError(f"image {image_id} is too large")
    return contents


def validate_feed_image(contents: bytes) -> None:
    try:
        with Image.open(io.BytesIO(contents)) as image:
            if image.format != "JPEG":
                raise ImageFeedError("image feed returned a non-JPEG image")
            if image.width > MAX_IMAGE_SIDE or image.height > MAX_IMAGE_SIDE:
                raise ImageFeedError("image feed returned an oversized image")
            image.load()
    except (OSError, UnidentifiedImageError) as error:
        raise ImageFeedError("image feed returned an invalid JPEG") from error


def feed_request_url(feed: ImageFeed, action: str, **parameters: object) -> str:
    parts = urlsplit(feed.url)
    query = urlencode(
        {
            "action": action,
            "token": feed.token.get_secret_value(),
            **parameters,
        }
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def read_feed_cursor(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        cursor = int(path.read_text())
    except (OSError, ValueError) as error:
        raise ImageFeedError("image feed cursor is invalid") from error
    if cursor < 0:
        raise ImageFeedError("image feed cursor is invalid")
    return cursor


def write_feed_cursor(path: Path, cursor: int) -> None:
    publish_file(path, f"{cursor}\n".encode())


def publish_file(target: Path, contents: bytes) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=f".{target.name}.", suffix=".part", delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(contents)
        temporary.replace(target)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
MAX_FEED_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_SIDE = 2048
