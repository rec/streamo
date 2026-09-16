import io
import queue
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import URLError
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname, urlopen

from PIL import Image
from reccy.protocol import ipc, rpc

from .images import IMAGE_SUFFIXES, MAX_IMAGE_BYTES, MAX_IMAGE_SIDE, publish_file
from .kick_api import KickApiError
from .providers import (
    COMMAND_CAPABILITIES,
    StreamingServiceAdapter,
    UnsupportedServiceOperation,
    endpoint_host,
)
from .twitch_api import TwitchApiError
from .youtube_api import YouTubeApiError


class RuntimeState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = 'starting'
        self.muted = False
        self.ffmpeg_alive = False
        self.ffmpeg_returncode: int | None = None
        self.audio_frames = 0
        self.audio_seconds = 0.0
        self.last_audio_at: float | None = None
        self.left_level_db: float | None = None
        self.right_level_db: float | None = None
        self.clipping = False
        self.output_bitrate_kbps: float | None = None
        self.last_error: str | None = None
        self.service: str | None = None
        self.endpoint_host: str | None = None
        self.capabilities: list[str] = []
        self.remote_health: dict[str, object] | None = None
        self.remote_health_error: str | None = None
        self.remote_health_updated_at: float | None = None
        self.audio_error: str | None = None
        self.audio_dropped_frames = 0
        self.audio_error_count = 0
        self.audio_last_error: str | None = None
        self.publish_requested = False
        self.encoder_attempts = 0
        self.next_retry_at: float | None = None
        self.publish_error: str | None = None
        self.last_publish_error: str | None = None
        self.last_output_progress_at: float | None = None
        self.progress_started: float | None = None
        self.progress_updated: float | None = None
        self.output_time_us = 0

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                'state': self.state,
                'muted': self.muted,
                'ffmpeg_alive': self.ffmpeg_alive,
                'ffmpeg_returncode': self.ffmpeg_returncode,
                'audio_frames': self.audio_frames,
                'audio_seconds': self.audio_seconds,
                'last_audio_at': self.last_audio_at,
                'left_level_db': self.left_level_db,
                'right_level_db': self.right_level_db,
                'clipping': self.clipping,
                'output_bitrate_kbps': self.output_bitrate_kbps,
                'last_error': self.last_error,
                'publish_requested': self.publish_requested,
                'encoder_attempts': self.encoder_attempts,
                'next_retry_at': self.next_retry_at,
                'publish_error': self.publish_error,
                'last_publish_error': self.last_publish_error,
                'last_output_progress_at': self.last_output_progress_at,
                'audio_error': self.audio_error,
                'audio_last_error': self.audio_last_error,
                'audio_error_count': self.audio_error_count,
                'audio_dropped_frames': self.audio_dropped_frames,
                'service': self.service,
                'endpoint_host': self.endpoint_host,
                'capabilities': list(self.capabilities),
                'remote_health_error': self.remote_health_error,
                'remote_health_updated_at': self.remote_health_updated_at,
                'remote_health': (
                    None if self.remote_health is None else dict(self.remote_health)
                ),
            }

    def configure_service(self, adapter: StreamingServiceAdapter) -> None:
        with self._lock:
            self.service = adapter.service.service
            self.endpoint_host = endpoint_host(adapter.service)
            self.capabilities = [c.value for c in adapter.capabilities]

    def set_audio_health(self, error: str | None, dropped_frames: int) -> None:
        with self._lock:
            if error and error != self.audio_error:
                self.audio_error_count += 1
                self.audio_last_error = error
            self.audio_error = error
            self.audio_dropped_frames = dropped_frames

    def set_remote_health(
        self, health: dict[str, object] | None, error: str | None = None
    ) -> None:
        with self._lock:
            self.remote_health = health
            self.remote_health_error = error
            self.remote_health_updated_at = time.time()

    def set_state(self, state: str) -> None:
        with self._lock:
            self.state = state

    def set_error(self, message: str) -> None:
        with self._lock:
            self.state = 'failed'
            self.last_error = message

    def set_ffmpeg(self, *, alive: bool, returncode: int | None = None) -> None:
        with self._lock:
            self.ffmpeg_alive = alive
            self.ffmpeg_returncode = returncode

    def begin_encoder_attempt(self) -> None:
        with self._lock:
            self.encoder_attempts += 1
            self.next_retry_at = None
            self.output_bitrate_kbps = None
            self.last_output_progress_at = None
            self.progress_started = self.progress_updated = None
            self.output_time_us = 0
            self.state = 'starting'

    def set_publish_requested(self, requested: bool) -> None:
        with self._lock:
            self.publish_requested = requested
            if not requested:
                self.next_retry_at = None

    def publish_failed(self, message: str, retry_delay: float | None) -> None:
        with self._lock:
            self.publish_error = self.last_publish_error = self.last_error = message
            self.next_retry_at = (
                None if retry_delay is None else time.time() + retry_delay
            )
            self.state = 'failed' if retry_delay is None else 'recovering'
            self.output_bitrate_kbps = None

    def record_output_progress(self, output_time_us: int) -> None:
        with self._lock:
            if output_time_us <= self.output_time_us:
                return
            now = time.monotonic()
            if self.progress_updated is None or now - self.progress_updated > 10:
                self.progress_started = now
            self.output_time_us = output_time_us
            self.progress_updated = now
            self.last_output_progress_at = time.time()
            self.publish_error = None
            self.state = 'muted' if self.muted else 'streaming'

    def output_is_stable(self) -> bool:
        with self._lock:
            return (
                self.progress_started is not None
                and self.progress_updated is not None
                and self.progress_updated - self.progress_started >= 60
                and time.monotonic() - self.progress_updated <= 10
            )

    def set_muted(self, muted: bool) -> None:
        with self._lock:
            self.muted = muted
            if self.state in {'streaming', 'muted'}:
                self.state = 'muted' if muted else 'streaming'

    def is_muted(self) -> bool:
        with self._lock:
            return self.muted

    def record_audio(
        self,
        *,
        frames: int,
        sample_rate: int,
        left_level_db: float,
        right_level_db: float,
        clipping: bool,
    ) -> None:
        with self._lock:
            self.audio_frames += frames
            self.audio_seconds = self.audio_frames / sample_rate
            self.last_audio_at = time.time()
            self.left_level_db = left_level_db
            self.right_level_db = right_level_db
            self.clipping = clipping

    def set_output_bitrate(self, bitrate_kbps: float | None) -> None:
        with self._lock:
            self.output_bitrate_kbps = bitrate_kbps


@dataclass
class ControlCommand:
    name: str
    payload: dict[str, object] = field(default_factory=dict)


@dataclass
class ControlController:
    state: RuntimeState
    image_dir: Path = Path('images')
    service: StreamingServiceAdapter | None = None
    commands: queue.Queue[ControlCommand] = field(default_factory=queue.Queue)

    def handle_request(self, request: rpc.Request) -> rpc.Result:
        command = request.command
        if command == 'ping':
            return 'pong'
        if command == 'status':
            return self.state.snapshot()
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
    publish_file(target, contents)


def remove_last_image(image_dir: Path) -> Path | None:
    if not image_dir.exists():
        return None
    images = [
        p
        for p in image_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    ]
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
