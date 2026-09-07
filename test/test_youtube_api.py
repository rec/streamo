import json

from pydantic import SecretStr

from streamo.services import (
    AudioEncoding,
    EncodingProfile,
    RtmpIngest,
    StreamMetadata,
    VideoEncoding,
    YouTubeService,
)
from streamo.youtube_api import YouTubeApi, YouTubeRequest


class FakeTokens:
    def __init__(self) -> None:
        self.requests: list[bool] = []

    def access_token(self, *, force_refresh: bool = False) -> str:
        self.requests.append(force_refresh)
        return "refreshed-token" if force_refresh else "access-token"


class FakeTransport:
    def __init__(self, responses: list[tuple[int, dict[str, object]]]) -> None:
        self.responses = responses
        self.requests: list[YouTubeRequest] = []

    def __call__(self, request: YouTubeRequest) -> tuple[int, bytes]:
        self.requests.append(request)
        status, body = self.responses.pop(0)
        return status, json.dumps(body).encode()


def youtube_service() -> YouTubeService:
    return YouTubeService(
        service="youtube",
        credentials="youtube-auth.toml",
        stream_id="stream-1",
        broadcast_id="broadcast-1",
        encoding=EncodingProfile(
            container="flv",
            audio=AudioEncoding(
                codec="aac", bitrate="160k", sample_rate=48_000, channels=2
            ),
            video=VideoEncoding(
                codec="h264",
                bitrate="2500k",
                resolution="1280x720",
                frame_rate=30,
                keyframe_interval=2,
            ),
        ),
    )


def broadcast() -> dict[str, object]:
    return {
        "snippet": {
            "title": "Old title",
            "description": "Old description",
            "scheduledStartTime": "2026-09-07T20:00:00Z",
            "categoryId": "10",
        },
        "status": {"privacyStatus": "unlisted"},
        "contentDetails": {
            "monitorStream": {
                "enableMonitorStream": True,
                "broadcastStreamDelayMs": 0,
            },
            "enableDvr": True,
            "recordFromStart": True,
        },
    }


def api_with(
    *responses: tuple[int, dict[str, object]],
) -> tuple[YouTubeApi, FakeTokens, FakeTransport]:
    tokens = FakeTokens()
    transport = FakeTransport(list(responses))
    return YouTubeApi(tokens=tokens, transport=transport), tokens, transport


def test_request_refreshes_once_after_unauthorized() -> None:
    api, tokens, transport = api_with(
        (401, {"error": {"message": "expired"}}),
        (200, {"items": []}),
    )

    assert api.request("GET", "liveStreams") == {"items": []}
    assert tokens.requests == [False, True]
    assert transport.requests[0].headers["Authorization"] == "Bearer access-token"
    assert transport.requests[1].headers["Authorization"] == "Bearer refreshed-token"


def test_prepare_updates_binds_and_returns_api_ingest() -> None:
    stream = {
        "cdn": {
            "ingestionType": "rtmp",
            "ingestionInfo": {
                "ingestionAddress": "rtmp://a.rtmp.youtube.com/live2",
                "rtmpsIngestionAddress": "rtmps://a.rtmps.youtube.com/live2",
                "streamName": "secret-stream-name",
            },
        },
        "status": {"streamStatus": "ready"},
    }
    api, _, transport = api_with(
        (200, {"items": [stream]}),
        (200, {"items": [broadcast()]}),
        (200, {}),
        (200, {}),
    )

    ingest, remote_ids = api.prepare(
        youtube_service(), StreamMetadata(title="New title", privacy="private")
    )

    assert isinstance(ingest, RtmpIngest)
    assert ingest.server_url == "rtmps://a.rtmps.youtube.com/live2"
    assert ingest.stream_key == SecretStr("secret-stream-name")
    assert remote_ids == {"stream_id": "stream-1", "broadcast_id": "broadcast-1"}
    update = json.loads(transport.requests[2].body or b"{}")
    assert update["snippet"]["title"] == "New title"
    assert update["status"]["privacyStatus"] == "private"
    assert update["contentDetails"]["enableAutoStart"] is True
    assert update["contentDetails"]["enableAutoStop"] is True
    assert "liveBroadcasts/bind" in transport.requests[3].url
    assert "streamId=stream-1" in transport.requests[3].url


def test_prepare_builds_hls_upload_template_from_api_ingest() -> None:
    service = youtube_service().model_copy(
        update={
            "encoding": youtube_service().encoding.model_copy(
                update={"container": "mpegts"}
            )
        }
    )
    stream = {
        "cdn": {
            "ingestionType": "hls",
            "ingestionInfo": {
                "ingestionAddress": (
                    "https://a.upload.youtube.com/http_upload_hls?"
                    "cid=secret-stream-name&copy=0&file="
                ),
                "streamName": "secret-stream-name",
            },
        },
        "status": {"streamStatus": "ready"},
    }
    api, _, _ = api_with(
        (200, {"items": [stream]}),
        (200, {"items": [broadcast()]}),
        (200, {}),
        (200, {}),
    )

    ingest, _ = api.prepare(service, StreamMetadata())

    assert ingest.protocol == "hls"
    assert ingest.upload_url == (
        "https://a.upload.youtube.com/http_upload_hls?"
        "cid={stream_key}&copy=0&file=stream.m3u8"
    )


def test_health_reports_configuration_issues() -> None:
    api, _, _ = api_with(
        (
            200,
            {
                "items": [
                    {
                        "status": {
                            "streamStatus": "active",
                            "healthStatus": {
                                "status": "bad",
                                "configurationIssues": [
                                    {"type": "audioBitrateHigh"},
                                    {"type": "videoBitrateLow"},
                                ],
                            },
                        }
                    }
                ]
            },
        )
    )

    assert api.health("stream-1") == (
        "active",
        "health=bad; issues=audioBitrateHigh, videoBitrateLow",
    )


def test_metadata_update_does_not_reconfigure_live_broadcast() -> None:
    api, _, transport = api_with(
        (200, {"items": [broadcast()]}),
        (200, {}),
    )

    api.update_metadata(youtube_service(), StreamMetadata(title="Live title"))

    request = transport.requests[1]
    update = json.loads(request.body or b"{}")
    assert "part=snippet%2Cstatus" in request.url
    assert "contentDetails" not in update
