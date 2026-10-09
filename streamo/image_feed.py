import hashlib
import io
import json
import threading
from itertools import pairwise
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

from PIL import Image, UnidentifiedImageError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)
from reccy.configuration import units
from reccy.runtime.files import atomic_output
from reccy.runtime.logging import get_logger

from .images import MAX_IMAGE_BYTES, MAX_IMAGE_SIDE

LOGGER = get_logger(__name__)


class ImageFeed(BaseModel, frozen=True):
    url: str
    token: SecretStr = Field(min_length=20)
    poll_interval: units.Seconds = Field(default=2.0, ge=0.5)

    @field_validator('url')
    @classmethod
    def validate_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme != 'https' or parts.hostname is None:
            raise ValueError('image feed URL must use HTTPS')
        return value

    model_config = ConfigDict(hide_input_in_errors=True, extra='forbid')


class ImageFeedItem(BaseModel, frozen=True):
    id: int = Field(gt=0)


class ImageFeedError(ValueError):
    pass


class RejectedFeedImage(ImageFeedError):
    pass


class ImageFeedPoller:
    def __init__(self, feed: ImageFeed, image_dir: Path) -> None:
        self.feed = feed
        self.image_dir = image_dir
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        identity = f'{feed.url}\0{feed.token.get_secret_value()}'.encode()
        digest = hashlib.sha256(identity).hexdigest()[:12]
        self.feed_id = digest
        self.cursor_path = image_dir / f'.streamo-image-feed-{digest}.cursor'

    def start(self) -> None:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.thread = threading.Thread(
            target=self.run,
            name='StreamoImageFeed',
            daemon=True,
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.poll()
            except (ImageFeedError, OSError) as error:
                host = urlsplit(self.feed.url).hostname or 'image feed'
                LOGGER.error('Could not poll image feed at %s: %s', host, error)
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
            try:
                contents = fetch_feed_image(self.feed, item.id)
                validate_feed_image(contents)
            except RejectedFeedImage as error:
                LOGGER.error('Skipping image feed item %s: %s', item.id, error)
            else:
                target = self.image_dir / f'remote-{self.feed_id}-{item.id:08}.jpg'
                with atomic_output(target) as temporary:
                    temporary.write_bytes(contents)
                stored.append(target)
            write_feed_cursor(self.cursor_path, item.id)
            cursor = item.id
        return stored


def fetch_feed_items(feed: ImageFeed, after: int) -> list[ImageFeedItem]:
    try:
        url = feed_request_url(feed, 'feed', after=after)
        with urlopen(url, timeout=10) as response:
            contents = response.read(MAX_FEED_BYTES + 1)
    except (HTTPError, OSError, URLError) as error:
        raise ImageFeedError(f'feed request failed ({type(error).__name__})') from error
    if len(contents) > MAX_FEED_BYTES:
        raise ImageFeedError('feed response is too large')
    try:
        items = [
            ImageFeedItem.model_validate(json.loads(line))
            for line in contents.decode().splitlines()
            if line.strip()
        ]
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
        raise ImageFeedError('feed returned invalid JSON Lines') from error
    if any(a.id >= b.id for a, b in pairwise(items)):
        raise ImageFeedError('feed item IDs are not increasing')
    return items


def fetch_feed_image(feed: ImageFeed, image_id: int) -> bytes:
    try:
        with urlopen(
            feed_request_url(feed, 'image', id=image_id), timeout=10
        ) as response:
            contents = response.read(MAX_IMAGE_BYTES + 1)
    except HTTPError as error:
        if error.code in {404, 410}:
            raise RejectedFeedImage(f'image {image_id} no longer exists') from error
        raise ImageFeedError(
            f'image {image_id} request failed (HTTP {error.code})'
        ) from error
    except (OSError, URLError) as error:
        raise ImageFeedError(
            f'image {image_id} request failed ({type(error).__name__})'
        ) from error
    if len(contents) > MAX_IMAGE_BYTES:
        raise RejectedFeedImage(f'image {image_id} is too large')
    return contents


def validate_feed_image(contents: bytes) -> None:
    try:
        with Image.open(io.BytesIO(contents)) as image:
            if image.format != 'JPEG':
                raise RejectedFeedImage('image feed returned a non-JPEG image')
            if image.width > MAX_IMAGE_SIDE or image.height > MAX_IMAGE_SIDE:
                raise RejectedFeedImage('image feed returned an oversized image')
            image.load()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise RejectedFeedImage('image feed returned an invalid JPEG') from error


def feed_request_url(feed: ImageFeed, action: str, **parameters: object) -> str:
    parts = urlsplit(feed.url)
    query = urlencode(
        {
            'action': action,
            'token': feed.token.get_secret_value(),
            **parameters,
        }
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ''))


def read_feed_cursor(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        cursor = int(path.read_text())
    except (OSError, ValueError) as error:
        raise ImageFeedError('image feed cursor is invalid') from error
    if cursor < 0:
        raise ImageFeedError('image feed cursor is invalid')
    return cursor


def write_feed_cursor(path: Path, cursor: int) -> None:
    with atomic_output(path) as temporary:
        temporary.write_text(f'{cursor}\n')


MAX_FEED_BYTES = 256 * 1024
