import re
import urllib.parse
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum, auto
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class AudioEncoding(BaseModel, frozen=True):
    codec: Literal["aac", "mp3", "opus", "vorbis"]
    bitrate: str
    sample_rate: int
    channels: Literal[1, 2]

    @field_validator("bitrate")
    @classmethod
    def validate_bitrate(cls, value: str) -> str:
        validate_bitrate(value)
        return value

    @field_validator("sample_rate")
    @classmethod
    def validate_sample_rate(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("sample_rate must be positive")
        return value

    model_config = ConfigDict(hide_input_in_errors=True)


class VideoEncoding(BaseModel, frozen=True):
    codec: Literal["h264", "hevc", "av1"]
    bitrate: str
    resolution: str
    frame_rate: int
    keyframe_interval: float
    pixel_format: str = "yuv420p"

    @field_validator("bitrate")
    @classmethod
    def validate_bitrate(cls, value: str) -> str:
        validate_bitrate(value)
        return value

    @field_validator("resolution")
    @classmethod
    def validate_resolution(cls, value: str) -> str:
        match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", value.lower())
        if match is None:
            raise ValueError("resolution must contain positive WIDTHxHEIGHT values")
        return value

    @field_validator("frame_rate")
    @classmethod
    def validate_frame_rate(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("frame_rate must be positive")
        return value

    @field_validator("keyframe_interval")
    @classmethod
    def validate_keyframe_interval(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("keyframe_interval must be positive")
        return value

    model_config = ConfigDict(hide_input_in_errors=True)


class EncodingProfile(BaseModel, frozen=True):
    container: Literal["flv", "mpegts", "ogg", "mp3", "adts"]
    audio: AudioEncoding
    video: VideoEncoding | None = None

    @model_validator(mode="after")
    def validate_codecs(self) -> Self:
        audio_codecs = {
            "adts": {"aac"},
            "flv": {"aac", "mp3"},
            "mp3": {"mp3"},
            "mpegts": {"aac", "mp3", "opus"},
            "ogg": {"opus", "vorbis"},
        }
        if self.audio.codec not in audio_codecs[self.container]:
            raise ValueError(
                f"{self.audio.codec} is not compatible with the "
                f"{self.container} container"
            )
        return self

    model_config = ConfigDict(hide_input_in_errors=True)


class RtmpIngest(BaseModel, frozen=True):
    protocol: Literal["rtmp", "rtmps"]
    server_url: str
    stream_key: SecretStr
    backup_server_url: str | None = None
    backup_stream_key: SecretStr | None = None

    @model_validator(mode="after")
    def validate_ingest(self) -> Self:
        require_url_scheme(self.server_url, {self.protocol})
        require_secret(self.stream_key, "stream_key")
        if (self.backup_server_url is None) != (self.backup_stream_key is None):
            raise ValueError(
                "backup_server_url and backup_stream_key must appear together"
            )
        if self.backup_server_url is not None:
            require_url_scheme(self.backup_server_url, {self.protocol})
        if self.backup_stream_key is not None:
            require_secret(self.backup_stream_key, "backup_stream_key")
        return self

    model_config = ConfigDict(hide_input_in_errors=True)


class SrtIngest(BaseModel, frozen=True):
    protocol: Literal["srt"]
    url: str
    passphrase: SecretStr | None = None
    latency_ms: int = 120

    @model_validator(mode="after")
    def validate_ingest(self) -> Self:
        require_url_scheme(self.url, {"srt"})
        if self.passphrase is not None:
            require_secret(self.passphrase, "passphrase")
        if self.latency_ms <= 0:
            raise ValueError("latency_ms must be positive")
        return self

    model_config = ConfigDict(hide_input_in_errors=True)


class HlsPushIngest(BaseModel, frozen=True):
    protocol: Literal["hls"]
    upload_url: str
    stream_key: SecretStr
    segment_duration: float

    @model_validator(mode="after")
    def validate_ingest(self) -> Self:
        require_url_scheme(self.upload_url, {"http", "https"})
        require_secret(self.stream_key, "stream_key")
        if "{stream_key}" not in self.upload_url:
            raise ValueError("upload_url must contain {stream_key}")
        if self.segment_duration <= 0:
            raise ValueError("segment_duration must be positive")
        return self

    model_config = ConfigDict(hide_input_in_errors=True)


class IcecastIngest(BaseModel, frozen=True):
    protocol: Literal["icecast"]
    server_url: str
    mountpoint: str
    username: str = "source"
    password: SecretStr
    tls: bool = False

    @model_validator(mode="after")
    def validate_ingest(self) -> Self:
        require_url_scheme(self.server_url, {"icecast"})
        if not self.mountpoint.startswith("/"):
            raise ValueError("mountpoint must begin with /")
        if not self.username:
            raise ValueError("username is required")
        require_secret(self.password, "password")
        return self

    model_config = ConfigDict(hide_input_in_errors=True)


class StreamMetadata(BaseModel, frozen=True):
    title: str | None = None
    description: str | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    language: str | None = None
    privacy: Literal["public", "unlisted", "private"] | None = None
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
        Field(discriminator="protocol"),
    ]
    encoding: EncodingProfile
    metadata: StreamMetadata = Field(default_factory=StreamMetadata)

    @model_validator(mode="after")
    def validate_media_contract(self) -> Self:
        if isinstance(self.ingest, RtmpIngest) and self.encoding.container != "flv":
            raise ValueError("RTMP and RTMPS output requires the flv container")
        if isinstance(self.ingest, (SrtIngest, HlsPushIngest)):
            if self.encoding.container != "mpegts":
                raise ValueError("SRT and HLS output requires the mpegts container")
        if isinstance(self.ingest, IcecastIngest):
            validate_icecast_encoding(self.encoding)
        if (
            self.ingest is not None
            and (contract := SERVICE_CATALOG.get(self.service)) is not None
        ):
            if self.ingest.protocol not in contract.protocols:
                supported = ", ".join(contract.protocols)
                raise ValueError(
                    f"{self.service} requires one of these protocols: {supported}"
                )
            if contract.video_required and self.encoding.video is None:
                raise ValueError(f"{self.service} requires video encoding")
        return self

    model_config = ConfigDict(hide_input_in_errors=True)


class TwitchService(StreamingService, frozen=True):
    service: Literal["twitch"]
    name: str = "Twitch"
    client_id: SecretStr | None = None
    access_token: SecretStr | None = None
    broadcaster_id: str | None = None
    sender_id: str | None = None
    moderator_id: str | None = None
    api_url: str = "https://api.twitch.tv/helix"
    bandwidth_test: bool = False


class YouTubeService(StreamingService, frozen=True):
    service: Literal["youtube"]
    name: str = "YouTube"
    ingest: (
        Annotated[RtmpIngest | HlsPushIngest, Field(discriminator="protocol")] | None
    ) = None
    credentials: Path | None = None
    stream_id: str | None = None
    broadcast_id: str | None = None
    auto_start: bool = True
    auto_stop: bool = True

    @model_validator(mode="after")
    def validate_youtube(self) -> Self:
        if self.encoding.video is None:
            raise ValueError("youtube requires video encoding")
        if self.credentials is None and self.ingest is None:
            raise ValueError("YouTube requires ingest or credentials")
        if self.credentials is not None and (
            self.stream_id is None or self.broadcast_id is None
        ):
            raise ValueError("YouTube credentials require stream_id and broadcast_id")
        if isinstance(self.ingest, HlsPushIngest):
            if not 1 <= self.ingest.segment_duration <= 4:
                raise ValueError("YouTube HLS segment_duration must be from 1 to 4")
            if self.encoding.audio.codec != "aac":
                raise ValueError("YouTube HLS requires AAC audio")
            assert self.encoding.video is not None
            if self.encoding.video.codec not in {"h264", "hevc"}:
                raise ValueError("YouTube HLS requires H.264 or HEVC video")
        return self


class FacebookService(StreamingService, frozen=True):
    service: Literal["facebook"]
    name: str = "Facebook"
    access_token: SecretStr | None = None
    destination_id: str | None = None
    live_video_id: str | None = None


class KickService(StreamingService, frozen=True):
    service: Literal["kick"]
    name: str = "Kick"
    ingest: RtmpIngest | None = None
    credentials: Path | None = None
    channel: str | None = None
    api_url: str = "https://api.kick.com/public/v1"

    @model_validator(mode="after")
    def validate_kick(self) -> Self:
        if self.encoding.video is None:
            raise ValueError("kick requires video encoding")
        if self.credentials is None and self.ingest is None:
            raise ValueError("Kick requires ingest or credentials")
        if self.credentials is not None and self.channel is None:
            raise ValueError("Kick credentials require channel")
        return self


class VimeoService(StreamingService, frozen=True):
    service: Literal["vimeo"]
    name: str = "Vimeo"
    access_token: SecretStr | None = None
    event_id: str | None = None


class LinkedInService(StreamingService, frozen=True):
    service: Literal["linkedin"]
    name: str = "LinkedIn"
    event_id: str | None = None
    operator_go_live: bool = True


class IcecastService(StreamingService, frozen=True):
    service: Literal["icecast"]
    name: str = "Icecast"
    admin_url: str | None = None
    admin_username: str | None = None
    admin_password: SecretStr | None = None


class CustomService(StreamingService, frozen=True):
    service: Literal["custom"]
    name: str = "Custom"


type StreamingServiceConfiguration = Annotated[
    TwitchService
    | YouTubeService
    | FacebookService
    | KickService
    | VimeoService
    | LinkedInService
    | IcecastService
    | CustomService,
    Field(discriminator="service"),
]


class ServiceCapability(StrEnum):
    PREPARE = auto()
    PUBLISH = auto()
    FINISH = auto()
    STOP = auto()
    METADATA = auto()
    HEALTH = auto()
    BACKUP_INGEST = auto()
    CHAT = auto()
    ANNOUNCE = auto()
    CLIP = auto()
    MARKER = auto()
    CUE_POINT = auto()
    SCHEDULE = auto()
    ARCHIVE = auto()


class PreparedStream(BaseModel, frozen=True):
    service: StreamingServiceConfiguration
    remote_ids: dict[str, str] = Field(default_factory=dict)


class FfmpegDestination(BaseModel, frozen=True):
    muxer: str
    url: str
    options: dict[str, str] = Field(default_factory=dict)
    secret_url: bool = True

    def arguments(self) -> list[str]:
        arguments = [
            argument
            for name, value in self.options.items()
            for argument in (f"-{name}", value)
        ]
        return [*arguments, "-f", self.muxer, self.url]

    def tee_specification(self, *, redact: bool = False) -> str:
        options = [f"f={tee_escape(self.muxer)}"]
        options.extend(
            f"{name}={tee_escape(value)}" for name, value in self.options.items()
        )
        if redact and self.secret_url:
            return f"[{':'.join(options)}][REDACTED]"
        return f"[{':'.join(options)}]{tee_escape(self.url)}"


class FfmpegOutput(BaseModel, frozen=True):
    destinations: list[FfmpegDestination]

    @property
    def arguments(self) -> list[str]:
        if len(self.destinations) == 1:
            return self.destinations[0].arguments()
        return [
            "-f",
            "tee",
            "|".join(
                destination.tee_specification() for destination in self.destinations
            ),
        ]

    def redacted_arguments(self) -> list[str]:
        if len(self.destinations) == 1:
            destination = self.destinations[0]
            arguments = destination.arguments()
            if destination.secret_url:
                arguments[-1] = "[REDACTED]"
            return arguments
        return [
            "-f",
            "tee",
            "|".join(
                destination.tee_specification(redact=True)
                for destination in self.destinations
            ),
        ]


class RemoteStreamStatus(BaseModel, frozen=True):
    state: str
    detail: str | None = None


class StreamingServiceAdapter(Protocol):
    service: StreamingServiceConfiguration
    capabilities: list[ServiceCapability]

    def prepare(self, metadata: StreamMetadata) -> PreparedStream: ...

    def output(self, prepared: PreparedStream) -> FfmpegOutput: ...

    def publish(self, prepared: PreparedStream) -> None: ...

    def update_metadata(self, metadata: StreamMetadata) -> None: ...

    def health(self) -> RemoteStreamStatus | None: ...

    def perform(
        self, command: str, payload: Mapping[str, object]
    ) -> dict[str, object]: ...

    def finish(self) -> None: ...


class UnsupportedServiceOperation(ValueError):
    pass


class GenericServiceAdapter:
    def __init__(self, service: StreamingServiceConfiguration) -> None:
        self.service = service
        self.capabilities = [ServiceCapability.PUBLISH, ServiceCapability.STOP]
        if isinstance(service.ingest, RtmpIngest):
            if service.ingest.backup_server_url is not None:
                self.capabilities.append(ServiceCapability.BACKUP_INGEST)

    def prepare(self, metadata: StreamMetadata) -> PreparedStream:
        return PreparedStream(
            service=self.service.model_copy(update={"metadata": metadata})
        )

    def output(self, prepared: PreparedStream) -> FfmpegOutput:
        return ingest_output(prepared.service)

    def publish(self, prepared: PreparedStream) -> None:
        pass

    def update_metadata(self, metadata: StreamMetadata) -> None:
        raise UnsupportedServiceOperation(
            f"{self.service.name} does not support metadata"
        )

    def health(self) -> RemoteStreamStatus | None:
        return None

    def perform(self, command: str, payload: Mapping[str, object]) -> dict[str, object]:
        capability = command_capability(command)
        raise UnsupportedServiceOperation(
            f"{self.service.name} does not support {capability.value}"
        )

    def finish(self) -> None:
        pass


class TwitchServiceAdapter(GenericServiceAdapter):
    def __init__(self, service: StreamingServiceConfiguration) -> None:
        from .twitch_api import TwitchApi

        assert isinstance(service, TwitchService)
        super().__init__(service)
        self.twitch = TwitchApi.from_service(service)
        if self.twitch is not None:
            self.capabilities.extend(
                [
                    ServiceCapability.METADATA,
                    ServiceCapability.CHAT,
                    ServiceCapability.ANNOUNCE,
                    ServiceCapability.CLIP,
                    ServiceCapability.MARKER,
                ]
            )

    def prepare(self, metadata: StreamMetadata) -> PreparedStream:
        if self.twitch is not None:
            payload = twitch_metadata_payload(metadata)
            if payload:
                self.twitch.perform("update_stream_info", payload)
        return super().prepare(metadata)

    def perform(self, command: str, payload: Mapping[str, object]) -> dict[str, object]:
        capability = command_capability(command)
        if capability not in self.capabilities or self.twitch is None:
            raise UnsupportedServiceOperation(
                f"{self.service.name} does not support {capability.value} "
                "with its current configuration"
            )
        return self.twitch.perform(command, payload)

    def update_metadata(self, metadata: StreamMetadata) -> None:
        payload = twitch_metadata_payload(metadata)
        if payload:
            self.perform("update_stream_info", payload)


class YouTubeServiceAdapter(GenericServiceAdapter):
    def __init__(self, service: StreamingServiceConfiguration) -> None:
        from .youtube_api import YouTubeApi

        assert isinstance(service, YouTubeService)
        super().__init__(service)
        self.youtube = YouTubeApi.from_service(service)
        if self.youtube is not None:
            self.capabilities.extend(
                [
                    ServiceCapability.PREPARE,
                    ServiceCapability.FINISH,
                    ServiceCapability.METADATA,
                    ServiceCapability.HEALTH,
                    ServiceCapability.SCHEDULE,
                ]
            )

    def prepare(self, metadata: StreamMetadata) -> PreparedStream:
        assert isinstance(self.service, YouTubeService)
        if self.youtube is None:
            return super().prepare(metadata)
        ingest, remote_ids = self.youtube.prepare(self.service, metadata)
        service = YouTubeService.model_validate(
            {**self.service.model_dump(), "ingest": ingest, "metadata": metadata}
        )
        self.service = service
        return PreparedStream(service=service, remote_ids=remote_ids)

    def perform(self, command: str, payload: Mapping[str, object]) -> dict[str, object]:
        assert isinstance(self.service, YouTubeService)
        if command != "update_stream_info" or self.youtube is None:
            return super().perform(command, payload)
        metadata = StreamMetadata.model_validate(
            {**self.service.metadata.model_dump(), **payload}
        )
        self.youtube.update_metadata(self.service, metadata)
        self.service = self.service.model_copy(update={"metadata": metadata})
        return {}

    def update_metadata(self, metadata: StreamMetadata) -> None:
        assert isinstance(self.service, YouTubeService)
        if self.youtube is None:
            return super().update_metadata(metadata)
        self.youtube.update_metadata(self.service, metadata)
        self.service = self.service.model_copy(update={"metadata": metadata})

    def health(self) -> RemoteStreamStatus | None:
        assert isinstance(self.service, YouTubeService)
        if self.youtube is None or self.service.stream_id is None:
            return None
        state, detail = self.youtube.health(self.service.stream_id)
        return RemoteStreamStatus(state=state, detail=detail)


class FacebookServiceAdapter(GenericServiceAdapter):
    pass


class KickServiceAdapter(GenericServiceAdapter):
    def __init__(self, service: StreamingServiceConfiguration) -> None:
        from .kick_api import KickApi

        assert isinstance(service, KickService)
        super().__init__(service)
        self.kick = KickApi.from_service(service)
        if self.kick is not None:
            self.capabilities.extend(
                [
                    ServiceCapability.PREPARE,
                    ServiceCapability.METADATA,
                    ServiceCapability.HEALTH,
                    ServiceCapability.CHAT,
                ]
            )

    def prepare(self, metadata: StreamMetadata) -> PreparedStream:
        assert isinstance(self.service, KickService)
        if self.kick is None:
            return super().prepare(metadata)
        ingest, remote_ids = self.kick.prepare(metadata)
        service = KickService.model_validate(
            {**self.service.model_dump(), "ingest": ingest, "metadata": metadata}
        )
        self.service = service
        return PreparedStream(service=service, remote_ids=remote_ids)

    def perform(self, command: str, payload: Mapping[str, object]) -> dict[str, object]:
        capability = command_capability(command)
        if capability not in self.capabilities or self.kick is None:
            raise UnsupportedServiceOperation(
                f"{self.service.name} does not support {capability.value} "
                "with its current configuration"
            )
        return self.kick.perform(command, payload)

    def update_metadata(self, metadata: StreamMetadata) -> None:
        if self.kick is None:
            return super().update_metadata(metadata)
        self.kick.update_metadata(metadata)
        self.service = self.service.model_copy(update={"metadata": metadata})

    def health(self) -> RemoteStreamStatus | None:
        if self.kick is None:
            return None
        state, detail = self.kick.health()
        return RemoteStreamStatus(state=state, detail=detail)


class VimeoServiceAdapter(GenericServiceAdapter):
    pass


class LinkedInServiceAdapter(GenericServiceAdapter):
    pass


class IcecastServiceAdapter(GenericServiceAdapter):
    pass


def adapter_for(service: StreamingServiceConfiguration) -> StreamingServiceAdapter:
    adapter = SERVICE_ADAPTERS[service.service]
    return adapter(service)


def command_capability(command: str) -> ServiceCapability:
    try:
        return COMMAND_CAPABILITIES[command]
    except KeyError as error:
        raise UnsupportedServiceOperation(
            f"unknown service command {command}"
        ) from error


def endpoint_host(service: StreamingServiceConfiguration) -> str:
    ingest = service.ingest
    if ingest is None:
        return ""
    url = (
        ingest.server_url
        if isinstance(ingest, (RtmpIngest, IcecastIngest))
        else ingest.url
        if isinstance(ingest, SrtIngest)
        else ingest.upload_url
    )
    return urllib.parse.urlsplit(url).hostname or ""


def ingest_output(service: StreamingServiceConfiguration) -> FfmpegOutput:
    ingest = service.ingest
    if ingest is None:
        raise ValueError(f"{service.name} ingest has not been prepared")
    if isinstance(ingest, RtmpIngest):
        key = urllib.parse.quote(ingest.stream_key.get_secret_value(), safe="")
        url = f"{ingest.server_url.rstrip('/')}/{key}"
        if isinstance(service, TwitchService) and service.bandwidth_test:
            url = f"{url}?bandwidthtest=true"
        return FfmpegOutput(
            destinations=[FfmpegDestination(muxer=service.encoding.container, url=url)]
        )
    if isinstance(ingest, SrtIngest):
        url = add_srt_options(ingest)
        return FfmpegOutput(
            destinations=[FfmpegDestination(muxer=service.encoding.container, url=url)]
        )
    if isinstance(ingest, HlsPushIngest):
        key = urllib.parse.quote(ingest.stream_key.get_secret_value(), safe="")
        url = ingest.upload_url.replace("{stream_key}", key)
        return FfmpegOutput(
            destinations=[
                FfmpegDestination(
                    muxer="hls",
                    url=url,
                    options={
                        "hls_time": format_number(ingest.segment_duration),
                        "hls_list_size": "5",
                        "method": "PUT",
                    },
                )
            ]
        )
    return icecast_output(service, ingest)


def add_srt_options(ingest: SrtIngest) -> str:
    parsed = urllib.parse.urlsplit(ingest.url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("latency", str(ingest.latency_ms * 1000)))
    if ingest.passphrase is not None:
        query.append(("passphrase", ingest.passphrase.get_secret_value()))
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def icecast_output(
    service: StreamingServiceConfiguration, ingest: IcecastIngest
) -> FfmpegOutput:
    parsed = urllib.parse.urlsplit(ingest.server_url)
    username = urllib.parse.quote(ingest.username, safe="")
    password = urllib.parse.quote(ingest.password.get_secret_value(), safe="")
    url = f"icecast://{username}:{password}@{parsed.netloc}{ingest.mountpoint}"
    content_type = ICECAST_CONTENT_TYPES[service.encoding.audio.codec]
    options = {"content_type": content_type}
    if ingest.tls:
        options["tls"] = "1"
    if service.metadata.title is not None:
        options["ice_name"] = service.metadata.title
    if service.metadata.description is not None:
        options["ice_description"] = service.metadata.description
    return FfmpegOutput(
        destinations=[
            FfmpegDestination(
                muxer=service.encoding.container,
                url=url,
                options=options,
            )
        ]
    )


def tee_escape(value: str) -> str:
    for character in "\\'[]|:":
        value = value.replace(character, f"\\{character}")
    return value


def validate_icecast_encoding(encoding: EncodingProfile) -> None:
    if encoding.video is not None:
        raise ValueError("Icecast output must be audio-only")
    containers = {
        "aac": {"adts"},
        "mp3": {"mp3"},
        "opus": {"ogg"},
        "vorbis": {"ogg"},
    }
    if encoding.container not in containers[encoding.audio.codec]:
        raise ValueError(
            f"{encoding.audio.codec} is not compatible with the "
            f"{encoding.container} container"
        )


def validate_bitrate(value: str) -> None:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kKmM]?)", value)
    if match is None or float(match.group(1)) <= 0:
        raise ValueError("bitrate must be a positive FFmpeg rate")


def require_secret(value: SecretStr, name: str) -> None:
    if not value.get_secret_value():
        raise ValueError(f"{name} is required")


def require_url_scheme(url: str, schemes: set[str]) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in schemes or not parsed.hostname:
        expected = " or ".join(sorted(schemes))
        raise ValueError(f"URL must use {expected} and include a host")


def format_number(value: float) -> str:
    return f"{value:g}"


def twitch_metadata_payload(metadata: StreamMetadata) -> dict[str, object]:
    payload: dict[str, object] = {}
    if metadata.title is not None:
        payload["title"] = metadata.title
    if metadata.category is not None:
        payload["category"] = metadata.category
    if metadata.tags:
        payload["tags"] = metadata.tags
    if metadata.language is not None:
        payload["broadcaster_language"] = metadata.language
    return payload


COMMAND_CAPABILITIES = {
    "update_stream_info": ServiceCapability.METADATA,
    "chat": ServiceCapability.CHAT,
    "announce": ServiceCapability.ANNOUNCE,
    "clip": ServiceCapability.CLIP,
    "marker": ServiceCapability.MARKER,
}

SERVICE_ADAPTERS: dict[str, type[GenericServiceAdapter]] = {
    "twitch": TwitchServiceAdapter,
    "youtube": YouTubeServiceAdapter,
    "facebook": FacebookServiceAdapter,
    "kick": KickServiceAdapter,
    "vimeo": VimeoServiceAdapter,
    "linkedin": LinkedInServiceAdapter,
    "icecast": IcecastServiceAdapter,
    "custom": GenericServiceAdapter,
}

ICECAST_CONTENT_TYPES = {
    "aac": "audio/aac",
    "mp3": "audio/mpeg",
    "opus": "application/ogg",
    "vorbis": "application/ogg",
}

SERVICE_CATALOG = {
    "twitch": ServiceContract(protocols=["rtmp", "rtmps"], video_required=True),
    "youtube": ServiceContract(protocols=["rtmp", "rtmps", "hls"], video_required=True),
    "facebook": ServiceContract(protocols=["rtmp", "rtmps"], video_required=True),
    "kick": ServiceContract(protocols=["rtmps"], video_required=True),
    "vimeo": ServiceContract(protocols=["rtmp", "rtmps", "srt"], video_required=True),
    "linkedin": ServiceContract(protocols=["rtmp", "rtmps"], video_required=True),
    "icecast": ServiceContract(protocols=["icecast"], video_required=False),
}
