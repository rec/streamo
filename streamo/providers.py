import urllib.parse
from collections.abc import Mapping
from enum import StrEnum, auto
from typing import Protocol

from pydantic import (
    BaseModel,
    Field,
)

from .provider_config import (
    HlsPushIngest,
    IcecastIngest,
    KickService,
    RtmpIngest,
    SrtIngest,
    StreamingServiceConfiguration,
    StreamMetadata,
    TwitchService,
    YouTubeService,
)


class ServiceCapability(StrEnum):
    METADATA = auto()
    HEALTH = auto()
    CHAT = auto()
    ANNOUNCE = auto()
    CLIP = auto()
    MARKER = auto()


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
            for argument in (f'-{name}', value)
        ]
        return [*arguments, '-f', self.muxer, self.url]

    def tee_specification(self, *, redact: bool = False) -> str:
        options = [f'f={tee_escape(self.muxer)}']
        options.extend(
            f'{name}={tee_escape(value)}' for name, value in self.options.items()
        )
        if redact and self.secret_url:
            return f'[{":".join(options)}][REDACTED]'
        return f'[{":".join(options)}]{tee_escape(self.url)}'


class FfmpegOutput(BaseModel, frozen=True):
    destinations: list[FfmpegDestination]

    @property
    def arguments(self) -> list[str]:
        if len(self.destinations) == 1:
            return self.destinations[0].arguments()
        return [
            '-f',
            'tee',
            '|'.join(
                destination.tee_specification() for destination in self.destinations
            ),
        ]

    def redacted_arguments(self) -> list[str]:
        if len(self.destinations) == 1:
            destination = self.destinations[0]
            arguments = destination.arguments()
            if destination.secret_url:
                arguments[-1] = '[REDACTED]'
            return arguments
        return [
            '-f',
            'tee',
            '|'.join(
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
        self.capabilities: list[ServiceCapability] = []

    def prepare(self, metadata: StreamMetadata) -> PreparedStream:
        return PreparedStream(
            service=self.service.model_copy(update={'metadata': metadata})
        )

    def output(self, prepared: PreparedStream) -> FfmpegOutput:
        return ingest_output(prepared.service)

    def publish(self, prepared: PreparedStream) -> None:
        pass

    def update_metadata(self, metadata: StreamMetadata) -> None:
        raise UnsupportedServiceOperation(
            f'{self.service.name} does not support metadata'
        )

    def health(self) -> RemoteStreamStatus | None:
        return None

    def perform(self, command: str, payload: Mapping[str, object]) -> dict[str, object]:
        capability = command_capability(command)
        raise UnsupportedServiceOperation(
            f'{self.service.name} does not support {capability.value}'
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
                self.twitch.perform('update_stream_info', payload)
        return super().prepare(metadata)

    def perform(self, command: str, payload: Mapping[str, object]) -> dict[str, object]:
        capability = command_capability(command)
        if capability not in self.capabilities or self.twitch is None:
            raise UnsupportedServiceOperation(
                f'{self.service.name} does not support {capability.value} '
                'with its current configuration'
            )
        return self.twitch.perform(command, payload)

    def update_metadata(self, metadata: StreamMetadata) -> None:
        payload = twitch_metadata_payload(metadata)
        if payload:
            self.perform('update_stream_info', payload)


class YouTubeServiceAdapter(GenericServiceAdapter):
    def __init__(self, service: StreamingServiceConfiguration) -> None:
        from .youtube_api import YouTubeApi

        assert isinstance(service, YouTubeService)
        super().__init__(service)
        self.youtube = YouTubeApi.from_service(service)
        if self.youtube is not None:
            self.capabilities.extend(
                [
                    ServiceCapability.METADATA,
                    ServiceCapability.HEALTH,
                ]
            )

    def prepare(self, metadata: StreamMetadata) -> PreparedStream:
        assert isinstance(self.service, YouTubeService)
        if self.youtube is None:
            return super().prepare(metadata)
        ingest, remote_ids = self.youtube.prepare(self.service, metadata)
        service = YouTubeService.model_validate(
            {**self.service.model_dump(), 'ingest': ingest, 'metadata': metadata}
        )
        self.service = service
        return PreparedStream(service=service, remote_ids=remote_ids)

    def perform(self, command: str, payload: Mapping[str, object]) -> dict[str, object]:
        assert isinstance(self.service, YouTubeService)
        if command != 'update_stream_info' or self.youtube is None:
            return super().perform(command, payload)
        metadata = StreamMetadata.model_validate(
            {**self.service.metadata.model_dump(), **payload}
        )
        self.youtube.update_metadata(self.service, metadata)
        self.service = self.service.model_copy(update={'metadata': metadata})
        return {}

    def update_metadata(self, metadata: StreamMetadata) -> None:
        assert isinstance(self.service, YouTubeService)
        if self.youtube is None:
            return super().update_metadata(metadata)
        self.youtube.update_metadata(self.service, metadata)
        self.service = self.service.model_copy(update={'metadata': metadata})

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
            {**self.service.model_dump(), 'ingest': ingest, 'metadata': metadata}
        )
        self.service = service
        return PreparedStream(service=service, remote_ids=remote_ids)

    def perform(self, command: str, payload: Mapping[str, object]) -> dict[str, object]:
        capability = command_capability(command)
        if capability not in self.capabilities or self.kick is None:
            raise UnsupportedServiceOperation(
                f'{self.service.name} does not support {capability.value} '
                'with its current configuration'
            )
        return self.kick.perform(command, payload)

    def update_metadata(self, metadata: StreamMetadata) -> None:
        if self.kick is None:
            return super().update_metadata(metadata)
        self.kick.update_metadata(metadata)
        self.service = self.service.model_copy(update={'metadata': metadata})

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
            f'unknown service command {command}'
        ) from error


def endpoint_host(service: StreamingServiceConfiguration) -> str:
    ingest = service.ingest
    if ingest is None:
        return ''
    url = (
        ingest.server_url
        if isinstance(ingest, (RtmpIngest, IcecastIngest))
        else ingest.url
        if isinstance(ingest, SrtIngest)
        else ingest.upload_url
    )
    return urllib.parse.urlsplit(url).hostname or ''


def ingest_output(service: StreamingServiceConfiguration) -> FfmpegOutput:
    ingest = service.ingest
    if ingest is None:
        raise ValueError(f'{service.name} ingest has not been prepared')
    if isinstance(ingest, RtmpIngest):
        key = urllib.parse.quote(ingest.stream_key.get_secret_value(), safe='')
        url = f'{ingest.server_url.rstrip("/")}/{key}'
        if isinstance(service, TwitchService) and service.bandwidth_test:
            url = f'{url}?bandwidthtest=true'
        return FfmpegOutput(
            destinations=[FfmpegDestination(muxer=service.encoding.container, url=url)]
        )
    if isinstance(ingest, SrtIngest):
        url = add_srt_options(ingest)
        return FfmpegOutput(
            destinations=[FfmpegDestination(muxer=service.encoding.container, url=url)]
        )
    if isinstance(ingest, HlsPushIngest):
        key = urllib.parse.quote(ingest.stream_key.get_secret_value(), safe='')
        url = ingest.upload_url.replace('{stream_key}', key)
        return FfmpegOutput(
            destinations=[
                FfmpegDestination(
                    muxer='hls',
                    url=url,
                    options={
                        'hls_time': format_number(ingest.segment_duration),
                        'hls_list_size': '5',
                        'method': 'PUT',
                    },
                )
            ]
        )
    return icecast_output(service, ingest)


def add_srt_options(ingest: SrtIngest) -> str:
    parsed = urllib.parse.urlsplit(ingest.url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append(('latency', str(ingest.latency_ms * 1000)))
    if ingest.passphrase is not None:
        query.append(('passphrase', ingest.passphrase.get_secret_value()))
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def icecast_output(
    service: StreamingServiceConfiguration, ingest: IcecastIngest
) -> FfmpegOutput:
    parsed = urllib.parse.urlsplit(ingest.server_url)
    username = urllib.parse.quote(ingest.username, safe='')
    password = urllib.parse.quote(ingest.password.get_secret_value(), safe='')
    url = f'icecast://{username}:{password}@{parsed.netloc}{ingest.mountpoint}'
    content_type = ICECAST_CONTENT_TYPES[service.encoding.audio.codec]
    options = {'content_type': content_type}
    if ingest.tls:
        options['tls'] = '1'
    if service.metadata.title is not None:
        options['ice_name'] = service.metadata.title
    if service.metadata.description is not None:
        options['ice_description'] = service.metadata.description
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
        value = value.replace(character, f'\\{character}')
    return value


def format_number(value: float) -> str:
    return f'{value:g}'


def twitch_metadata_payload(metadata: StreamMetadata) -> dict[str, object]:
    payload: dict[str, object] = {}
    if metadata.title is not None:
        payload['title'] = metadata.title
    if metadata.category is not None:
        payload['category'] = metadata.category
    if metadata.tags:
        payload['tags'] = metadata.tags
    if metadata.language is not None:
        payload['broadcaster_language'] = metadata.language
    return payload


COMMAND_CAPABILITIES = {
    'update_stream_info': ServiceCapability.METADATA,
    'chat': ServiceCapability.CHAT,
    'announce': ServiceCapability.ANNOUNCE,
    'clip': ServiceCapability.CLIP,
    'marker': ServiceCapability.MARKER,
}

SERVICE_ADAPTERS: dict[str, type[GenericServiceAdapter]] = {
    'twitch': TwitchServiceAdapter,
    'youtube': YouTubeServiceAdapter,
    'facebook': FacebookServiceAdapter,
    'kick': KickServiceAdapter,
    'vimeo': VimeoServiceAdapter,
    'linkedin': LinkedInServiceAdapter,
    'icecast': IcecastServiceAdapter,
    'custom': GenericServiceAdapter,
}

ICECAST_CONTENT_TYPES = {
    'aac': 'audio/aac',
    'mp3': 'audio/mpeg',
    'opus': 'application/ogg',
    'vorbis': 'application/ogg',
}
