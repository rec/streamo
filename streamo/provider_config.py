import re
import urllib.parse
from datetime import datetime
from math import isfinite
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class AudioEncoding(BaseModel, frozen=True):
    codec: Literal['aac', 'mp3', 'opus', 'vorbis']
    bitrate: str
    sample_rate: int
    channels: Literal[1, 2]

    @field_validator('bitrate')
    @classmethod
    def validate_bitrate(cls, value: str) -> str:
        validate_bitrate(value)
        return value

    @field_validator('sample_rate')
    @classmethod
    def validate_sample_rate(cls, value: int) -> int:
        if value <= 0:
            raise ValueError('sample_rate must be positive')
        return value

    model_config = ConfigDict(hide_input_in_errors=True)


class VideoEncoding(BaseModel, frozen=True):
    codec: Literal['h264', 'hevc', 'av1']
    bitrate: str
    resolution: str
    frame_rate: int
    keyframe_interval: float
    pixel_format: str = 'yuv420p'

    @field_validator('bitrate')
    @classmethod
    def validate_bitrate(cls, value: str) -> str:
        validate_bitrate(value)
        return value

    @field_validator('resolution')
    @classmethod
    def validate_resolution(cls, value: str) -> str:
        match = re.fullmatch(r'([1-9][0-9]*)x([1-9][0-9]*)', value.lower())
        if match is None:
            raise ValueError('resolution must contain positive WIDTHxHEIGHT values')
        return value

    @field_validator('frame_rate')
    @classmethod
    def validate_frame_rate(cls, value: int) -> int:
        if value <= 0:
            raise ValueError('frame_rate must be positive')
        return value

    @field_validator('keyframe_interval')
    @classmethod
    def validate_keyframe_interval(cls, value: float) -> float:
        if not isfinite(value) or value <= 0:
            raise ValueError('keyframe_interval must be positive')
        return value

    model_config = ConfigDict(hide_input_in_errors=True)


class EncodingProfile(BaseModel, frozen=True):
    container: Literal['flv', 'mpegts', 'ogg', 'mp3', 'adts']
    audio: AudioEncoding
    video: VideoEncoding | None = None

    @model_validator(mode='after')
    def validate_codecs(self) -> Self:
        if self.video is not None and self.container in {'mp3', 'adts', 'ogg'}:
            raise ValueError(f'{self.container} is audio-only for the supported codecs')
        audio_codecs = {
            'adts': {'aac'},
            'flv': {'aac', 'mp3'},
            'mp3': {'mp3'},
            'mpegts': {'aac', 'mp3', 'opus'},
            'ogg': {'opus', 'vorbis'},
        }
        if self.audio.codec not in audio_codecs[self.container]:
            raise ValueError(
                f'{self.audio.codec} is not compatible with the '
                f'{self.container} container'
            )
        return self

    model_config = ConfigDict(hide_input_in_errors=True)


class RtmpIngest(BaseModel, frozen=True):
    protocol: Literal['rtmp', 'rtmps']
    server_url: str
    stream_key: SecretStr

    @model_validator(mode='after')
    def validate_ingest(self) -> Self:
        require_url_scheme(self.server_url, {self.protocol})
        require_secret(self.stream_key, 'stream_key')
        return self

    model_config = ConfigDict(hide_input_in_errors=True, extra='forbid')


class SrtIngest(BaseModel, frozen=True):
    protocol: Literal['srt']
    url: str
    passphrase: SecretStr | None = None
    latency_ms: int = 120

    @model_validator(mode='after')
    def validate_ingest(self) -> Self:
        require_url_scheme(self.url, {'srt'})
        if self.passphrase is not None:
            require_secret(self.passphrase, 'passphrase')
        if self.latency_ms <= 0:
            raise ValueError('latency_ms must be positive')
        return self

    model_config = ConfigDict(hide_input_in_errors=True)


class HlsPushIngest(BaseModel, frozen=True):
    protocol: Literal['hls']
    upload_url: str
    stream_key: SecretStr
    segment_duration: float

    @model_validator(mode='after')
    def validate_ingest(self) -> Self:
        require_url_scheme(self.upload_url, {'http', 'https'})
        require_secret(self.stream_key, 'stream_key')
        if '{stream_key}' not in self.upload_url:
            raise ValueError('upload_url must contain {stream_key}')
        if not isfinite(self.segment_duration) or self.segment_duration <= 0:
            raise ValueError('segment_duration must be positive')
        return self

    model_config = ConfigDict(hide_input_in_errors=True)


class IcecastIngest(BaseModel, frozen=True):
    protocol: Literal['icecast']
    server_url: str
    mountpoint: str
    username: str = 'source'
    password: SecretStr
    tls: bool = False

    @model_validator(mode='after')
    def validate_ingest(self) -> Self:
        require_url_scheme(self.server_url, {'icecast'})
        if not self.mountpoint.startswith('/'):
            raise ValueError('mountpoint must begin with /')
        if not self.username:
            raise ValueError('username is required')
        require_secret(self.password, 'password')
        return self

    model_config = ConfigDict(hide_input_in_errors=True)


class StreamMetadata(BaseModel, frozen=True):
    title: str | None = None
    description: str | None = None
    category: str | None = Field(
        default=None,
        description='Twitch category name; YouTube or Kick numeric category ID.',
    )
    tags: list[str] = Field(default_factory=list)
    language: str | None = None
    privacy: Literal['public', 'unlisted', 'private'] | None = None
    scheduled_start: datetime | None = None

    model_config = ConfigDict(hide_input_in_errors=True)


class ServiceContract(BaseModel, frozen=True):
    protocols: list[str]
    video_required: bool


class StreamingService(BaseModel, frozen=True):
    service: str
    name: str
    ingest: Annotated[
        RtmpIngest | SrtIngest | HlsPushIngest | IcecastIngest,
        Field(discriminator='protocol'),
    ]
    encoding: EncodingProfile
    metadata: StreamMetadata = Field(default_factory=StreamMetadata)

    @model_validator(mode='after')
    def validate_media_contract(self) -> Self:
        if isinstance(self.ingest, RtmpIngest) and self.encoding.container != 'flv':
            raise ValueError('RTMP and RTMPS output requires the flv container')
        if isinstance(self.ingest, (SrtIngest, HlsPushIngest)):
            if self.encoding.container != 'mpegts':
                raise ValueError('SRT and HLS output requires the mpegts container')
        if isinstance(self.ingest, IcecastIngest):
            validate_icecast_encoding(self.encoding)
        if (
            self.ingest is not None
            and (contract := SERVICE_CATALOG.get(self.service)) is not None
        ):
            if self.ingest.protocol not in contract.protocols:
                supported = ', '.join(contract.protocols)
                raise ValueError(
                    f'{self.service} requires one of these protocols: {supported}'
                )
            if contract.video_required and self.encoding.video is None:
                raise ValueError(f'{self.service} requires video encoding')
        return self

    model_config = ConfigDict(hide_input_in_errors=True)


class TwitchService(StreamingService, frozen=True):
    service: Literal['twitch']
    name: str = 'Twitch'
    client_id: SecretStr | None = None
    access_token: SecretStr | None = None
    broadcaster_id: str | None = None
    sender_id: str | None = None
    moderator_id: str | None = None
    api_url: str = 'https://api.twitch.tv/helix'
    bandwidth_test: bool = False


class YouTubeService(StreamingService, frozen=True):
    service: Literal['youtube']
    name: str = 'YouTube'
    ingest: (
        Annotated[RtmpIngest | HlsPushIngest, Field(discriminator='protocol')] | None
    ) = None
    credentials: Path | None = None
    stream_id: str | None = None
    broadcast_id: str | None = None
    auto_start: bool = True
    auto_stop: bool = True

    @model_validator(mode='after')
    def validate_youtube(self) -> Self:
        if self.encoding.video is None:
            raise ValueError('youtube requires video encoding')
        if self.credentials is None and self.ingest is None:
            raise ValueError('YouTube requires ingest or credentials')
        if self.credentials is not None and (
            self.stream_id is None or self.broadcast_id is None
        ):
            raise ValueError('YouTube credentials require stream_id and broadcast_id')
        if isinstance(self.ingest, HlsPushIngest):
            if not 1 <= self.ingest.segment_duration <= 4:
                raise ValueError('YouTube HLS segment_duration must be from 1 to 4')
            if self.encoding.audio.codec != 'aac':
                raise ValueError('YouTube HLS requires AAC audio')
            assert self.encoding.video is not None
            if self.encoding.video.codec not in {'h264', 'hevc'}:
                raise ValueError('YouTube HLS requires H.264 or HEVC video')
        return self


class FacebookService(StreamingService, frozen=True):
    service: Literal['facebook']
    name: str = 'Facebook'
    access_token: SecretStr | None = None
    destination_id: str | None = None
    live_video_id: str | None = None


class KickService(StreamingService, frozen=True):
    service: Literal['kick']
    name: str = 'Kick'
    ingest: RtmpIngest | None = None
    credentials: Path | None = None
    channel: str | None = Field(
        default=None, description='Kick channel slug, not an audio channel number.'
    )
    api_url: str = 'https://api.kick.com/public/v1'

    @model_validator(mode='after')
    def validate_kick(self) -> Self:
        if self.encoding.video is None:
            raise ValueError('kick requires video encoding')
        if self.credentials is None and self.ingest is None:
            raise ValueError('Kick requires ingest or credentials')
        if self.credentials is not None and self.channel is None:
            raise ValueError('Kick credentials require channel')
        return self


class VimeoService(StreamingService, frozen=True):
    service: Literal['vimeo']
    name: str = 'Vimeo'
    access_token: SecretStr | None = None
    event_id: str | None = None


class LinkedInService(StreamingService, frozen=True):
    service: Literal['linkedin']
    name: str = 'LinkedIn'
    event_id: str | None = None
    operator_go_live: bool = True


class IcecastService(StreamingService, frozen=True):
    service: Literal['icecast']
    name: str = 'Icecast'
    admin_url: str | None = None
    admin_username: str | None = None
    admin_password: SecretStr | None = None


class CustomService(StreamingService, frozen=True):
    service: Literal['custom']
    name: str = 'Custom'


type StreamingServiceConfiguration = Annotated[
    TwitchService
    | YouTubeService
    | FacebookService
    | KickService
    | VimeoService
    | LinkedInService
    | IcecastService
    | CustomService,
    Field(discriminator='service'),
]


def validate_icecast_encoding(encoding: EncodingProfile) -> None:
    if encoding.video is not None:
        raise ValueError('Icecast output must be audio-only')
    containers = {
        'aac': {'adts'},
        'mp3': {'mp3'},
        'opus': {'ogg'},
        'vorbis': {'ogg'},
    }
    if encoding.container not in containers[encoding.audio.codec]:
        raise ValueError(
            f'{encoding.audio.codec} is not compatible with the '
            f'{encoding.container} container'
        )


def validate_bitrate(value: str) -> None:
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([kKmM]?)', value)
    if match is None or float(match.group(1)) <= 0:
        raise ValueError('bitrate must be a positive FFmpeg rate')


def require_secret(value: SecretStr, name: str) -> None:
    if not value.get_secret_value():
        raise ValueError(f'{name} is required')


def require_url_scheme(url: str, schemes: set[str]) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in schemes or not parsed.hostname:
        expected = ' or '.join(sorted(schemes))
        raise ValueError(f'URL must use {expected} and include a host')


SERVICE_CATALOG = {
    'twitch': ServiceContract(protocols=['rtmp', 'rtmps'], video_required=True),
    'youtube': ServiceContract(protocols=['rtmp', 'rtmps', 'hls'], video_required=True),
    'facebook': ServiceContract(protocols=['rtmp', 'rtmps'], video_required=True),
    'kick': ServiceContract(protocols=['rtmps'], video_required=True),
    'vimeo': ServiceContract(protocols=['rtmp', 'rtmps', 'srt'], video_required=True),
    'linkedin': ServiceContract(protocols=['rtmp', 'rtmps'], video_required=True),
    'icecast': ServiceContract(protocols=['icecast'], video_required=False),
}
