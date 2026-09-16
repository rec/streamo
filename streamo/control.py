import io
import queue
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import URLError
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname, urlopen

from PIL import Image
from reccy.protocol import ipc, rpc
from reccy.runtime.files import atomic_output

from .images import IMAGE_SUFFIXES, MAX_IMAGE_BYTES, MAX_IMAGE_SIDE, image_paths
from .kick_api import KickApiError
from .moderation import ImagePreview, ImageQueue, ImageReview
from .overlays import LiveOverlays, SlateCue, TitleCue
from .providers import (
    COMMAND_CAPABILITIES,
    StreamingServiceAdapter,
    UnsupportedServiceOperation,
)
from .runtime import RuntimeState
from .twitch_api import TwitchApiError
from .youtube_api import YouTubeApiError


@dataclass
class ControlCommand:
    name: str
    payload: dict[str, object] = field(default_factory=dict)


@dataclass
class ControlController:
    state: RuntimeState
    image_dir: Path = Path('images')
    service: StreamingServiceAdapter | None = None
    overlays: LiveOverlays | None = None
    commands: queue.Queue[ControlCommand] = field(default_factory=queue.Queue)

    def handle_request(self, request: rpc.Request) -> rpc.Result:
        command = request.command
        if command == 'ping':
            return 'pong'
        if command == 'status':
            return {
                **self.state.snapshot(),
                'overlays': self.overlays.snapshot()
                if self.overlays
                else {'enabled': False},
            }
        if command in {'image_queue', 'image_preview', 'image_review'}:
            if self.overlays is None:
                return ipc.Error(
                    type='error', message='Image approval requires live overlays'
                )
            approval = self.overlays.approval
            try:
                if command == 'image_queue':
                    return approval.queue(ImageQueue.model_validate(request.params))
                if command == 'image_preview':
                    return approval.preview(ImagePreview.model_validate(request.params))
                return approval.review(ImageReview.model_validate(request.params))
            except (ValueError, OSError, Image.DecompressionBombError) as error:
                return ipc.Error(type='error', message=str(error))
        if command in {'title', 'slate', 'image_skip', 'image_pause', 'image_next'}:
            if self.overlays is None:
                return ipc.Error(
                    type='error',
                    message=(
                        'Live overlays unavailable; restart a video stream '
                        'with live_overlays=true'
                    ),
                )
            try:
                if command.startswith('image_'):
                    return self.overlays.control_images(command, request.params)
                if command == 'title':
                    return self.overlays.set_title(
                        TitleCue.model_validate(request.params)
                    )
                return self.overlays.set_slate(SlateCue.model_validate(request.params))
            except (ValueError, OSError, Image.DecompressionBombError) as error:
                return ipc.Error(type='error', message=str(error))
        if command == 'incidents':
            after = request.params.get('after', 0)
            if type(after) is not int or after < 0:
                return ipc.Error(
                    type='error', message='after must be a nonnegative integer'
                )
            return self.state.events_since(after)
        if command == 'mute':
            self.state.set_muted(True)
            return 'ok'
        if command == 'unmute':
            self.state.set_muted(False)
            return 'ok'
        if command == 'stop':
            self.commands.put(ControlCommand(name=command, payload=request.params))
            return 'ok'
        if command == 'image':
            return self.handle_image_command(request.params)
        if command == 'remove_last_image':
            removed = remove_last_image(self.image_dir)
            return {'removed': None if removed is None else removed.as_posix()}
        if command in SERVICE_COMMANDS:
            return self.handle_service_command(command, request.params)
        return ipc.Error(type='error', message=f'unknown command {command}')

    def handle_image_command(self, payload: dict[str, object]) -> rpc.Result:
        urls = payload.get('urls')
        if not isinstance(urls, list) or not urls:
            return ipc.Error(type='error', message='image requires one or more urls')
        validated_urls: list[str] = []
        for url in urls:
            if not isinstance(url, str):
                return ipc.Error(type='error', message='image urls must be strings')
            validated_urls.append(url)
        try:
            paths = store_images(self.image_dir, validated_urls)
        except ImageStoreError as error:
            return ipc.Error(type='error', message=str(error))
        return {'images': [p.as_posix() for p in paths]}

    def handle_service_command(
        self, command: str, payload: dict[str, object]
    ) -> rpc.Result:
        if self.service is None:
            return ipc.Error(
                type='error', message='streaming service is not configured'
            )
        try:
            return self.service.perform(command, payload)
        except (
            TwitchApiError,
            YouTubeApiError,
            KickApiError,
            UnsupportedServiceOperation,
        ) as error:
            return ipc.Error(type='error', message=str(error))


SERVICE_COMMANDS = set(COMMAND_CAPABILITIES)


class HealthPoller:
    def __init__(self, service: StreamingServiceAdapter, state: RuntimeState) -> None:
        self.service = service
        self.state = state
        self.stopped = threading.Event()
        self.thread = threading.Thread(
            target=self.run, daemon=True, name='ProviderHealth'
        )

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=1)

    def poll(self) -> None:
        try:
            health = self.service.health()
        except (
            TwitchApiError,
            YouTubeApiError,
            KickApiError,
            OSError,
            ValueError,
        ) as error:
            self.state.set_remote_health(
                None, f'Provider health unavailable ({type(error).__name__})'
            )
        else:
            self.state.set_remote_health(
                None if health is None else health.model_dump()
            )

    def run(self) -> None:
        while not self.stopped.is_set():
            self.poll()
            self.stopped.wait(30)


class ImageStoreError(ValueError):
    pass


def store_images(image_dir: Path, urls: list[str]) -> list[Path]:
    image_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=image_dir) as directory:
        staged = [store_image(Path(directory), u) for u in urls]
        published = []
        try:
            for source in staged:
                target = unique_image_path(image_dir, source.name)
                source.replace(target)
                published.append(target)
        except OSError as error:
            for path in published:
                path.unlink(missing_ok=True)
            raise ImageStoreError('could not publish image batch') from error
    return published


def store_image(image_dir: Path, url: str) -> Path:
    parsed = urlparse(url)
    if parsed.scheme == 'file':
        source = Path(url2pathname(parsed.path))
        target = unique_image_path(image_dir, image_name(parsed.path))
        try:
            with source.open('rb') as input_file:
                publish_image(target, input_file.read(MAX_IMAGE_BYTES + 1))
        except OSError as error:
            raise ImageStoreError(f'could not copy {url}: {error}') from error
        return target
    if parsed.scheme in {'http', 'https'}:
        target = unique_image_path(image_dir, image_name(parsed.path))
        try:
            with urlopen(url, timeout=10) as response:
                publish_image(target, response.read(MAX_IMAGE_BYTES + 1))
        except (OSError, URLError) as error:
            raise ImageStoreError(f'could not download {url}: {error}') from error
        return target
    raise ImageStoreError(f'unsupported image URL {url}')


def publish_image(target: Path, contents: bytes) -> None:
    if target.suffix.lower() not in IMAGE_SUFFIXES:
        raise ImageStoreError('unsupported image filename extension')
    if len(contents) > MAX_IMAGE_BYTES:
        raise ImageStoreError('image exceeds 8 MiB')
    try:
        with Image.open(io.BytesIO(contents)) as image:
            if image.format not in {'GIF', 'JPEG', 'PNG', 'WEBP'}:
                raise ImageStoreError('unsupported image format')
            if image.width > MAX_IMAGE_SIDE or image.height > MAX_IMAGE_SIDE:
                raise ImageStoreError('image dimensions exceed 2048 pixels')
            image.load()
    except (OSError, Image.DecompressionBombError) as error:
        raise ImageStoreError('invalid image') from error
    with atomic_output(target) as temporary:
        temporary.write_bytes(contents)


def remove_last_image(image_dir: Path) -> Path | None:
    if not image_dir.exists():
        return None
    images = image_paths(image_dir)
    if not images:
        return None
    latest = max(images, key=lambda p: (p.stat().st_mtime_ns, p.name))
    latest.unlink()
    return latest


def image_name(path: str) -> str:
    name = Path(unquote(path)).name
    if name in {'', '.', '..'}:
        name = 'image.png'
    if Path(name).suffix:
        return name
    return f'{name}.png'


def unique_image_path(image_dir: Path, name: str) -> Path:
    target = image_dir / name
    if not target.exists():
        return target
    suffix = target.suffix or '.png'
    stem = target.stem if target.suffix else target.name
    index = 2
    while (candidate := image_dir / f'{stem}-{index}{suffix}').exists():
        index += 1
    return candidate
