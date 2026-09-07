import queue
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import URLError
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname, urlopen

from reccy.protocol import ipc, rpc

from .services import (
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
        self.state = "starting"
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

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "state": self.state,
                "muted": self.muted,
                "ffmpeg_alive": self.ffmpeg_alive,
                "ffmpeg_returncode": self.ffmpeg_returncode,
                "audio_frames": self.audio_frames,
                "audio_seconds": self.audio_seconds,
                "last_audio_at": self.last_audio_at,
                "left_level_db": self.left_level_db,
                "right_level_db": self.right_level_db,
                "clipping": self.clipping,
                "output_bitrate_kbps": self.output_bitrate_kbps,
                "last_error": self.last_error,
                "service": self.service,
                "endpoint_host": self.endpoint_host,
                "capabilities": list(self.capabilities),
                "remote_health": (
                    None if self.remote_health is None else dict(self.remote_health)
                ),
            }

    def configure_service(self, adapter: StreamingServiceAdapter) -> None:
        with self._lock:
            self.service = adapter.service.service
            self.endpoint_host = endpoint_host(adapter.service)
            self.capabilities = [c.value for c in adapter.capabilities]

    def set_remote_health(self, health: dict[str, object] | None) -> None:
        with self._lock:
            self.remote_health = health

    def set_state(self, state: str) -> None:
        with self._lock:
            self.state = state

    def set_error(self, message: str) -> None:
        with self._lock:
            self.state = "failed"
            self.last_error = message

    def set_ffmpeg(self, *, alive: bool, returncode: int | None = None) -> None:
        with self._lock:
            self.ffmpeg_alive = alive
            self.ffmpeg_returncode = returncode

    def set_muted(self, muted: bool) -> None:
        with self._lock:
            self.muted = muted
            if self.state in {"streaming", "muted"}:
                self.state = "muted" if muted else "streaming"

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
    image_dir: Path = Path("images")
    service: StreamingServiceAdapter | None = None
    commands: queue.Queue[ControlCommand] = field(default_factory=queue.Queue)

    def handle_request(self, request: rpc.Request) -> rpc.Result:
        command = request.command
        if command == "ping":
            return "pong"
        if command == "status":
            if self.service is not None:
                health = self.service.health()
                self.state.set_remote_health(
                    None if health is None else health.model_dump()
                )
            return self.state.snapshot()
        if command == "mute":
            self.state.set_muted(True)
            return "ok"
        if command == "unmute":
            self.state.set_muted(False)
            return "ok"
        if command == "stop":
            self.commands.put(ControlCommand(name=command, payload=request.params))
            return "ok"
        if command == "image":
            return self.handle_image_command(request.params)
        if command in SERVICE_COMMANDS:
            return self.handle_service_command(command, request.params)
        return ipc.Error(type="error", message=f"unknown command {command}")

    def handle_image_command(self, payload: dict[str, object]) -> rpc.Result:
        urls = payload.get("urls")
        if not isinstance(urls, list) or not urls:
            return ipc.Error(type="error", message="image requires one or more urls")
        validated_urls: list[str] = []
        for url in urls:
            if not isinstance(url, str):
                return ipc.Error(type="error", message="image urls must be strings")
            validated_urls.append(url)
        try:
            paths = store_images(self.image_dir, validated_urls)
        except ImageStoreError as error:
            return ipc.Error(type="error", message=str(error))
        return {"images": [p.as_posix() for p in paths]}

    def handle_service_command(
        self, command: str, payload: dict[str, object]
    ) -> rpc.Result:
        if self.service is None:
            return ipc.Error(
                type="error", message="streaming service is not configured"
            )
        try:
            return self.service.perform(command, payload)
        except (
            TwitchApiError,
            YouTubeApiError,
            UnsupportedServiceOperation,
        ) as error:
            return ipc.Error(type="error", message=str(error))


SERVICE_COMMANDS = set(COMMAND_CAPABILITIES)


class ImageStoreError(ValueError):
    pass


def store_images(image_dir: Path, urls: list[str]) -> list[Path]:
    image_dir.mkdir(parents=True, exist_ok=True)
    return [store_image(image_dir, u) for u in urls]


def store_image(image_dir: Path, url: str) -> Path:
    parsed = urlparse(url)
    if parsed.scheme == "file":
        source = Path(url2pathname(parsed.path))
        target = unique_image_path(image_dir, image_name(parsed.path))
        try:
            publish_image(target, source.read_bytes())
        except OSError as error:
            raise ImageStoreError(f"could not copy {url}: {error}") from error
        return target
    if parsed.scheme in {"http", "https"}:
        target = unique_image_path(image_dir, image_name(parsed.path))
        try:
            with urlopen(url, timeout=10) as response:
                publish_image(target, response.read())
        except (OSError, URLError) as error:
            raise ImageStoreError(f"could not download {url}: {error}") from error
        return target
    raise ImageStoreError(f"unsupported image URL {url}")


def publish_image(target: Path, contents: bytes) -> None:
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


def image_name(path: str) -> str:
    name = Path(unquote(path)).name
    if name in {"", ".", ".."}:
        name = "image.png"
    if Path(name).suffix:
        return name
    return f"{name}.png"


def unique_image_path(image_dir: Path, name: str) -> Path:
    target = image_dir / name
    if not target.exists():
        return target
    suffix = target.suffix or ".png"
    stem = target.stem if target.suffix else target.name
    index = 2
    while (candidate := image_dir / f"{stem}-{index}{suffix}").exists():
        index += 1
    return candidate
