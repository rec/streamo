import re
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Self

from PIL import Image
from pydantic import ConfigDict, Field, PrivateAttr, field_validator, model_validator
from reccy.protocol import ipc, rpc
from reccy.reccy import Reccy, ReccyStatus
from reccy.services.spec import load

from .images import ImageFeed, ImageFeedPoller, image_paths
from .provider_config import StreamingServiceConfiguration
from .providers import GenericServiceAdapter, adapter_for
from .runtime import HealthWarnings, RuntimeState

STREAMO_SERVICE = load(Path(__file__).with_name('service.toml'))

if TYPE_CHECKING:
    from .control import ControlController


class Streamo(Reccy, frozen=True):
    name = 'streamo'
    service_spec = STREAMO_SERVICE
    status_model = ReccyStatus
    rpc_enabled = True

    device_name: str
    channel: int = Field(
        description='First channel of the stereo input pair, numbered from 1.'
    )
    streaming_service: StreamingServiceConfiguration
    video: Path | None = None
    title_card: Path | None = None

    sample_rate: int = 48_000
    video_resolution: str = '640x360'
    video_frame_rate: int = 10
    title_interval: float = 180.0
    title_duration: float = 8.0
    title_fade: float = 2.0
    local_display: bool = True
    live_overlays: bool = Field(
        default=True,
        description='Enable titles, photos and slate. Requires a restart to change.',
    )
    recover_publish: bool = False
    health_warnings: HealthWarnings = Field(default_factory=HealthWarnings)
    image_dir: list[Path] = Field(default_factory=lambda: [Path('images')])
    image_dir_weights: str | list[int] | None = None
    image_approval_required: bool = False
    image_feed: ImageFeed | None = None
    current_session_image_weight: int = 3
    image_interval: float = 0.0
    image_duration: float = 8.0
    image_fade: float = 2.0
    _controller: 'ControlController | None' = PrivateAttr(default=None)

    def run(self, *, preview: bool = False) -> int:
        from . import control, streamer

        self.validate_media()
        service_adapter = (
            GenericServiceAdapter(self.streaming_service)
            if preview
            else adapter_for(self.streaming_service)
        )
        controller = control.ControlController(
            state=RuntimeState(self.health_warnings),
            image_dir=self.primary_image_dir,
            service=None if preview else service_adapter,
        )
        controller.state.configure_service(service_adapter)
        if preview:
            controller.state.capabilities = []
        object.__setattr__(self, '_controller', controller)
        image_feed_poller = (
            None
            if self.image_feed is None
            or not self.live_overlays
            or self.streaming_service.encoding.video is None
            else ImageFeedPoller(self.image_feed, self.primary_image_dir)
        )
        initial_image_paths = {p for d in self.image_dir for p in image_paths(d)}
        self.start()
        try:
            if image_feed_poller is not None:
                image_feed_poller.start()
            returncode = streamer.stream(
                self,
                controller,
                service_adapter,
                initial_image_paths=initial_image_paths,
                preview=preview,
            )
            if returncode:
                self.publish_error(f'ffmpeg exited with {returncode}')
            return returncode
        finally:
            if image_feed_poller is not None:
                image_feed_poller.stop()
            self.close()

    def validate_media(self) -> None:
        if self.streaming_service.encoding.video is not None:
            if self.video is None or not self.video.is_file():
                raise ValueError(f'Video file is unavailable: {self.video}')
            with self.video.open('rb'):
                pass
        if self.live_overlays and self.title_card is not None:
            with Image.open(self.title_card) as image:
                image.load()

    @field_validator('video_resolution')
    @classmethod
    def validate_resolution(cls, value: str) -> str:
        if re.fullmatch(r'[1-9][0-9]*x[1-9][0-9]*', value.lower()) is None:
            raise ValueError(
                'video_resolution must contain positive WIDTHxHEIGHT values'
            )
        return value.lower()

    def rpc_response(self, request: rpc.Request) -> rpc.Result:
        if request.command == 'preflight':
            from .preflight import check

            if request.params:
                return ipc.Error(
                    type='error',
                    message=(
                        'RPC preflight accepts no parameters; '
                        'use the CLI for explicit probes'
                    ),
                )
            return check(self).model_dump(mode='json')
        if self._controller is None:
            return ipc.Error(type='error', message='streamO is not running')
        return self._controller.handle_request(request)

    @field_validator('channel', 'sample_rate', 'video_frame_rate')
    @classmethod
    def validate_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError('must be positive')
        return value

    @field_validator('current_session_image_weight')
    @classmethod
    def validate_nonnegative_weight(cls, value: int) -> int:
        if not isfinite(value) or value < 0:
            raise ValueError('must not be negative')
        return value

    @model_validator(mode='after')
    def validate_image_dirs(self) -> Self:
        if not self.image_dir:
            raise ValueError('image_dir must contain at least one directory')
        if len(set(self.image_dir)) != len(self.image_dir):
            raise ValueError('image_dir entries must be distinct')
        if self.image_dir_weights is not None:
            weights = parse_image_dir_weights(self.image_dir_weights)
            if not weights:
                raise ValueError('image_dir_weights must contain at least one weight')
            if len(weights) > len(self.image_dir):
                raise ValueError('image_dir_weights must not exceed image_dir')
            if any(w <= 0 for w in weights):
                raise ValueError('image_dir_weights must be positive')
        return self

    @property
    def primary_image_dir(self) -> Path:
        return self.image_dir[0]

    @property
    def resolved_image_dir_weights(self) -> list[int]:
        if self.image_dir_weights is None:
            return list(range(len(self.image_dir), 0, -1))
        weights = parse_image_dir_weights(self.image_dir_weights)
        return weights + [weights[-1]] * (len(self.image_dir) - len(weights))

    @field_validator(
        'title_interval',
        'title_duration',
        'title_fade',
        'image_interval',
        'image_duration',
        'image_fade',
    )
    @classmethod
    def validate_nonnegative_time(cls, value: float) -> float:
        if not isfinite(value) or value < 0:
            raise ValueError('must be finite and nonnegative')
        return value

    @model_validator(mode='after')
    def validate_title_card(self) -> Self:
        if not self.live_overlays:
            return self
        if self.title_card is not None and not self.title_card.is_file():
            raise ValueError(f'{self.title_card} does not exist')
        if self.title_interval <= 0:
            raise ValueError('title_interval must be positive')
        if self.title_duration <= 0:
            raise ValueError('title_duration must be positive')
        if self.title_duration >= self.title_interval:
            raise ValueError('title_duration must be shorter than title_interval')
        if self.title_fade * 2 > self.title_duration:
            raise ValueError('title_fade must fit within title_duration')
        return self

    @model_validator(mode='after')
    def validate_image_overlay(self) -> Self:
        if self.image_interval == 0:
            if self.image_feed is not None:
                raise ValueError('image_interval must be positive with an image feed')
            return self
        if self.image_duration <= 0:
            raise ValueError('image_duration must be positive')
        if self.image_duration >= self.image_interval:
            raise ValueError('image_duration must be shorter than image_interval')
        if self.image_fade * 2 > self.image_duration:
            raise ValueError('image_fade must fit within image_duration')
        return self

    @model_validator(mode='after')
    def validate_video(self) -> Self:
        if self.streaming_service.encoding.video is not None and self.video is None:
            raise ValueError('video is required for video streaming')
        return self

    @property
    def required_channels(self) -> int:
        return self.channel + 1

    model_config = ConfigDict(hide_input_in_errors=True)


def parse_image_dir_weights(value: str | list[int]) -> list[int]:
    if isinstance(value, list):
        return value
    try:
        weights = [int(part.strip()) for part in value.split(',')]
    except ValueError as error:
        raise ValueError(
            'image_dir_weights must be comma-separated integers'
        ) from error
    if not weights or any(not part.strip() for part in value.split(',')):
        raise ValueError('image_dir_weights must be comma-separated integers')
    return weights
