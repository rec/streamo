from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self

from pydantic import ConfigDict, PrivateAttr, field_validator, model_validator
from reccy.protocol import ipc, rpc
from reccy.reccy import Reccy, ReccyStatus
from reccy.services.models import ServiceSpec
from reccy.services.spec import load

from .images import ImageFeed, ImageFeedPoller
from .services import StreamingServiceConfiguration, adapter_for

STREAMO_SERVICE = load(Path(__file__).with_name("service.toml"))

if TYPE_CHECKING:
    from .control import ControlController


class Streamo(Reccy, frozen=True):
    service_spec: ClassVar[ServiceSpec] = STREAMO_SERVICE
    daemon_module: ClassVar[str] = "streamo"
    status_model: ClassVar[type[ReccyStatus]] = ReccyStatus
    rpc_enabled: ClassVar[bool] = True
    rpc_role: ClassVar[str] = "streamo"
    logger_name: ClassVar[str] = "streamo"

    device_name: str
    channel: int
    streaming_service: StreamingServiceConfiguration
    video: Path | None = None
    title_card: Path | None = None

    sample_rate: int = 48_000
    video_resolution: str = "640x360"
    video_frame_rate: int = 10
    title_interval: float = 180.0
    title_duration: float = 8.0
    title_fade: float = 2.0
    image_dir: Path = Path("images")
    image_feed: ImageFeed | None = None
    image_interval: float = 0.0
    image_duration: float = 8.0
    image_fade: float = 2.0
    _controller: "ControlController | None" = PrivateAttr(default=None)

    def run(self, *, preview: bool = False) -> int:
        from . import control, streamer

        service_adapter = adapter_for(self.streaming_service)
        controller = control.ControlController(
            state=control.RuntimeState(),
            image_dir=self.image_dir,
            service=service_adapter,
        )
        controller.state.configure_service(service_adapter)
        object.__setattr__(self, "_controller", controller)
        image_feed_poller = (
            None
            if self.image_feed is None
            else ImageFeedPoller(self.image_feed, self.image_dir)
        )
        self.start()
        try:
            if image_feed_poller is not None:
                image_feed_poller.start()
            returncode = streamer.stream(
                self, controller, service_adapter, preview=preview
            )
            if returncode:
                self.publish_error(f"ffmpeg exited with {returncode}")
            return returncode
        finally:
            if image_feed_poller is not None:
                image_feed_poller.stop()
            self.close()

    def rpc_response(self, request: rpc.Request) -> rpc.Result:
        if self._controller is None:
            return ipc.Error(type="error", message="Streamo is not running")
        return self._controller.handle_request(request)

    @field_validator("channel", "sample_rate", "video_frame_rate")
    @classmethod
    def validate_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be positive")
        return value

    @field_validator(
        "title_interval",
        "title_duration",
        "title_fade",
        "image_interval",
        "image_duration",
        "image_fade",
    )
    @classmethod
    def validate_nonnegative_time(cls, value: float) -> float:
        if value < 0:
            raise ValueError("must not be negative")
        return value

    @model_validator(mode="after")
    def validate_title_card(self) -> Self:
        if self.title_card is None:
            return self
        if not self.title_card.exists():
            raise ValueError(f"{self.title_card} does not exist")
        if self.title_interval <= 0:
            raise ValueError("title_interval must be positive")
        if self.title_duration <= 0:
            raise ValueError("title_duration must be positive")
        if self.title_duration >= self.title_interval:
            raise ValueError("title_duration must be shorter than title_interval")
        if self.title_fade * 2 > self.title_duration:
            raise ValueError("title_fade must fit within title_duration")
        return self

    @model_validator(mode="after")
    def validate_image_overlay(self) -> Self:
        if self.image_interval == 0:
            if self.image_feed is not None:
                raise ValueError("image_interval must be positive with an image feed")
            return self
        if self.image_duration <= 0:
            raise ValueError("image_duration must be positive")
        if self.image_duration >= self.image_interval:
            raise ValueError("image_duration must be shorter than image_interval")
        if self.image_fade * 2 > self.image_duration:
            raise ValueError("image_fade must fit within image_duration")
        return self

    @model_validator(mode="after")
    def validate_video(self) -> Self:
        if self.streaming_service.encoding.video is not None and self.video is None:
            raise ValueError("video is required for video streaming")
        return self

    @property
    def required_channels(self) -> int:
        return self.channel + 1

    model_config = ConfigDict(hide_input_in_errors=True)
