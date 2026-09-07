import json
from collections.abc import Callable

import pytest
from pydantic import TypeAdapter, ValidationError

from streamo.services import (
    AudioEncoding,
    CustomService,
    EncodingProfile,
    FacebookService,
    HlsPushIngest,
    IcecastIngest,
    IcecastService,
    KickService,
    LinkedInService,
    RtmpIngest,
    ServiceCapability,
    SrtIngest,
    StreamingServiceConfiguration,
    StreamMetadata,
    TwitchService,
    TwitchServiceAdapter,
    VimeoService,
    YouTubeService,
    adapter_for,
    ingest_output,
)
from streamo.twitch_api import TwitchRequest


def audio(codec: str = "aac") -> dict[str, object]:
    return {
        "codec": codec,
        "bitrate": "160k",
        "sample_rate": 48_000,
        "channels": 2,
    }


def video() -> dict[str, object]:
    return {
        "codec": "h264",
        "bitrate": "2500k",
        "resolution": "1280x720",
        "frame_rate": 30,
        "keyframe_interval": 2,
    }


def encoding(
    container: str = "flv", *, include_video: bool = True
) -> dict[str, object]:
    result: dict[str, object] = {"container": container, "audio": audio()}
    if include_video:
        result["video"] = video()
    return result


def rtmp(protocol: str = "rtmps") -> dict[str, object]:
    return {
        "protocol": protocol,
        "server_url": f"{protocol}://ingest.example.test/app",
        "stream_key": "secret-key",
    }


@pytest.mark.parametrize(
    ("data", "expected_type"),
    [
        ({"service": "twitch", "ingest": rtmp()}, TwitchService),
        (
            {
                "service": "youtube",
                "ingest": {
                    "protocol": "hls",
                    "upload_url": "https://upload.example.test/{stream_key}/live.m3u8",
                    "stream_key": "secret-key",
                    "segment_duration": 2,
                },
                "encoding": encoding("mpegts"),
            },
            YouTubeService,
        ),
        ({"service": "facebook", "ingest": rtmp()}, FacebookService),
        ({"service": "kick", "ingest": rtmp()}, KickService),
        (
            {
                "service": "vimeo",
                "ingest": {
                    "protocol": "srt",
                    "url": "srt://ingest.example.test:9000",
                },
                "encoding": encoding("mpegts"),
            },
            VimeoService,
        ),
        (
            {"service": "linkedin", "ingest": rtmp("rtmp")},
            LinkedInService,
        ),
        (
            {
                "service": "icecast",
                "ingest": {
                    "protocol": "icecast",
                    "server_url": "icecast://radio.example.test:8000",
                    "mountpoint": "/live",
                    "password": "source-secret",
                },
                "encoding": {
                    "container": "mp3",
                    "audio": audio("mp3"),
                },
            },
            IcecastService,
        ),
        ({"service": "custom", "ingest": rtmp()}, CustomService),
    ],
)
def test_every_service_and_ingest_protocol_parses(
    data: dict[str, object], expected_type: type[object]
) -> None:
    data.setdefault("encoding", encoding())

    service = TypeAdapter(StreamingServiceConfiguration).validate_python(data)

    assert isinstance(service, expected_type)
    assert adapter_for(service).service is service


def test_youtube_api_configuration_requires_existing_resource_ids() -> None:
    with pytest.raises(
        ValidationError,
        match="YouTube credentials require stream_id and broadcast_id",
    ):
        YouTubeService.model_validate(
            {
                "service": "youtube",
                "credentials": "youtube-auth.toml",
                "encoding": encoding(),
            }
        )


def test_youtube_api_configuration_requires_video() -> None:
    with pytest.raises(ValidationError, match="youtube requires video encoding"):
        YouTubeService.model_validate(
            {
                "service": "youtube",
                "credentials": "youtube-auth.toml",
                "stream_id": "stream-1",
                "broadcast_id": "broadcast-1",
                "encoding": encoding(include_video=False),
            }
        )


def test_kick_api_configuration_requires_channel() -> None:
    with pytest.raises(ValidationError, match="Kick credentials require channel"):
        KickService.model_validate(
            {
                "service": "kick",
                "credentials": "kick-auth.toml",
                "encoding": encoding(),
            }
        )


def test_kick_api_configuration_requires_video() -> None:
    with pytest.raises(ValidationError, match="kick requires video encoding"):
        KickService.model_validate(
            {
                "service": "kick",
                "credentials": "kick-auth.toml",
                "channel": "channel-name",
                "encoding": encoding(include_video=False),
            }
        )


def test_rtmp_requires_flv_and_matching_backup_fields() -> None:
    with pytest.raises(ValidationError, match="flv container"):
        CustomService(
            service="custom",
            ingest=RtmpIngest(
                protocol="rtmps",
                server_url="rtmps://ingest.example.test/app",
                stream_key="secret",
            ),
            encoding=EncodingProfile.model_validate(encoding("mpegts")),
        )

    with pytest.raises(ValidationError, match="must appear together"):
        RtmpIngest(
            protocol="rtmps",
            server_url="rtmps://ingest.example.test/app",
            stream_key="secret",
            backup_server_url="rtmps://backup.example.test/app",
        )


def test_named_service_rejects_unsupported_protocol() -> None:
    with pytest.raises(ValidationError, match="kick requires"):
        KickService(
            service="kick",
            ingest=RtmpIngest(
                protocol="rtmp",
                server_url="rtmp://ingest.example.test/app",
                stream_key="secret",
            ),
            encoding=EncodingProfile.model_validate(encoding()),
        )


def test_icecast_rejects_video_and_invalid_mountpoint() -> None:
    with pytest.raises(ValidationError, match="mountpoint must begin"):
        IcecastIngest(
            protocol="icecast",
            server_url="icecast://radio.example.test:8000",
            mountpoint="live",
            password="secret",
        )

    with pytest.raises(ValidationError, match="audio-only"):
        icecast_encoding = encoding("mp3")
        icecast_encoding["audio"] = audio("mp3")
        IcecastService(
            service="icecast",
            ingest=IcecastIngest(
                protocol="icecast",
                server_url="icecast://radio.example.test:8000",
                mountpoint="/live",
                password="secret",
            ),
            encoding=EncodingProfile.model_validate(icecast_encoding),
        )


def test_secrets_are_hidden_in_serialization_and_validation_errors() -> None:
    service = TwitchService(
        service="twitch",
        ingest=RtmpIngest(
            protocol="rtmps",
            server_url="rtmps://ingest.example.test/app",
            stream_key="super-secret-key",
        ),
        encoding=EncodingProfile.model_validate(encoding()),
        access_token="super-secret-token",
    )

    serialized = service.model_dump_json()
    assert "super-secret-key" not in serialized
    assert "super-secret-token" not in serialized

    with pytest.raises(ValidationError) as raised:
        RtmpIngest(
            protocol="rtmps",
            server_url="not-a-url",
            stream_key="validation-secret",
        )
    assert "validation-secret" not in str(raised.value)


def test_rtmp_output_and_diagnostics_redact_stream_key() -> None:
    service = CustomService(
        service="custom",
        ingest=RtmpIngest(
            protocol="rtmps",
            server_url="rtmps://ingest.example.test/app",
            stream_key="secret/key",
        ),
        encoding=EncodingProfile.model_validate(encoding()),
    )

    output = ingest_output(service)

    assert output.arguments == [
        "-f",
        "flv",
        "rtmps://ingest.example.test/app/secret%2Fkey",
    ]
    assert output.redacted_arguments() == ["-f", "flv", "[REDACTED]"]


def test_srt_output_includes_latency_and_redacts_passphrase() -> None:
    service = CustomService(
        service="custom",
        ingest=SrtIngest(
            protocol="srt",
            url="srt://ingest.example.test:9000?mode=caller",
            passphrase="secret phrase",
            latency_ms=250,
        ),
        encoding=EncodingProfile.model_validate(encoding("mpegts")),
    )

    output = ingest_output(service)

    assert output.arguments == [
        "-f",
        "mpegts",
        "srt://ingest.example.test:9000?mode=caller&latency=250000&passphrase=secret+phrase",
    ]
    assert output.redacted_arguments()[-1] == "[REDACTED]"


def test_hls_output_uses_transport_stream_segments() -> None:
    service = CustomService(
        service="custom",
        ingest=HlsPushIngest(
            protocol="hls",
            upload_url="https://upload.example.test/{stream_key}/live.m3u8",
            stream_key="secret-key",
            segment_duration=2,
        ),
        encoding=EncodingProfile.model_validate(encoding("mpegts")),
    )

    output = ingest_output(service)

    assert output.arguments == [
        "-hls_time",
        "2",
        "-hls_list_size",
        "5",
        "-method",
        "PUT",
        "-f",
        "hls",
        "https://upload.example.test/secret-key/live.m3u8",
    ]
    assert output.redacted_arguments()[-1] == "[REDACTED]"


def test_icecast_output_is_audio_only_and_redacts_password() -> None:
    service = IcecastService(
        service="icecast",
        ingest=IcecastIngest(
            protocol="icecast",
            server_url="icecast://radio.example.test:8000",
            mountpoint="/live",
            username="source",
            password="source secret",
            tls=True,
        ),
        encoding=EncodingProfile(
            container="mp3",
            audio=AudioEncoding(
                codec="mp3", bitrate="192k", sample_rate=48_000, channels=2
            ),
        ),
        metadata={"title": "Live show"},
    )

    output = ingest_output(service)

    assert output.arguments == [
        "-content_type",
        "audio/mpeg",
        "-tls",
        "1",
        "-ice_name",
        "Live show",
        "-f",
        "mp3",
        "icecast://source:source%20secret@radio.example.test:8000/live",
    ]
    assert output.redacted_arguments()[-1] == "[REDACTED]"


def test_named_adapters_expose_only_configured_capabilities() -> None:
    without_api = TwitchService(
        service="twitch",
        ingest=RtmpIngest(
            protocol="rtmps",
            server_url="rtmps://ingest.example.test/app",
            stream_key="secret",
        ),
        encoding=EncodingProfile.model_validate(encoding()),
    )
    with_api = TwitchService(
        service="twitch",
        ingest=without_api.ingest,
        encoding=without_api.encoding,
        client_id="client",
        access_token="token",
        broadcaster_id="broadcaster",
    )

    assert adapter_for(without_api).capabilities == [
        ServiceCapability.PUBLISH,
        ServiceCapability.STOP,
    ]
    assert ServiceCapability.CHAT in adapter_for(with_api).capabilities


def test_twitch_adapter_prepares_configured_metadata() -> None:
    service = TwitchService(
        service="twitch",
        ingest=RtmpIngest(
            protocol="rtmps",
            server_url="rtmps://ingest.example.test/app",
            stream_key="secret",
        ),
        encoding=EncodingProfile.model_validate(encoding()),
        client_id="client",
        access_token="token",
        broadcaster_id="broadcaster",
    )
    adapter = adapter_for(service)
    assert isinstance(adapter, TwitchServiceAdapter)
    assert adapter.twitch is not None
    requests: list[TwitchRequest] = []

    def transport(request: TwitchRequest) -> tuple[int, bytes]:
        requests.append(request)
        return 204, b""

    adapter.twitch.transport = transport

    adapter.prepare(StreamMetadata(title="Tonight", language="en"))

    assert json.loads(requests[0].body) == {
        "title": "Tonight",
        "broadcaster_language": "en",
    }


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["encoding"]["video"].update({"frame_rate": 0}),
        lambda d: d["encoding"]["video"].update({"resolution": "0x720"}),
        lambda d: d["encoding"]["audio"].update({"bitrate": "0k"}),
    ],
)
def test_encoding_values_must_be_positive(
    change: Callable[[dict[str, object]], object],
) -> None:
    data = {
        "service": "custom",
        "ingest": rtmp(),
        "encoding": encoding(),
    }
    change(data)

    with pytest.raises(ValidationError):
        TypeAdapter(StreamingServiceConfiguration).validate_python(data)
