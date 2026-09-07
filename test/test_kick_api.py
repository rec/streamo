import json
import stat
import tomllib
import urllib.parse
from pathlib import Path

from pydantic import SecretStr

from streamo.kick_api import (
    KickAccessTokenProvider,
    KickApi,
    KickRequest,
    StoredKickCredentials,
)
from streamo.services import StreamMetadata


class FakeTokens:
    def __init__(self) -> None:
        self.requests: list[bool] = []

    def access_token(self, *, force_refresh: bool = False) -> str:
        self.requests.append(force_refresh)
        return "refreshed-token" if force_refresh else "access-token"


class FakeTransport:
    def __init__(self, responses: list[tuple[int, dict[str, object] | None]]) -> None:
        self.responses = responses
        self.requests: list[KickRequest] = []

    def __call__(self, request: KickRequest) -> tuple[int, bytes]:
        self.requests.append(request)
        status, body = self.responses.pop(0)
        return status, b"" if body is None else json.dumps(body).encode()


def channel(*, live: bool = False) -> dict[str, object]:
    return {
        "broadcaster_user_id": 1234,
        "slug": "channel-name",
        "stream_title": "Old title",
        "stream": {
            "url": "rtmps://fa723fc1b171.global-contribute.live-video.net/app",
            "key": "secret-stream-key",
            "is_live": live,
            "viewer_count": 42 if live else 0,
        },
    }


def api_with(
    *responses: tuple[int, dict[str, object] | None],
) -> tuple[KickApi, FakeTokens, FakeTransport]:
    tokens = FakeTokens()
    transport = FakeTransport(list(responses))
    return (
        KickApi(
            tokens=tokens,
            channel_slug="channel-name",
            transport=transport,
        ),
        tokens,
        transport,
    )


def test_request_refreshes_once_after_unauthorized() -> None:
    api, tokens, transport = api_with(
        (401, {"message": "expired"}),
        (200, {"data": []}),
    )

    assert api.request("GET", "channels") == {"data": []}
    assert tokens.requests == [False, True]
    assert transport.requests[0].headers["Authorization"] == "Bearer access-token"
    assert transport.requests[1].headers["Authorization"] == "Bearer refreshed-token"


def test_prepare_updates_metadata_and_returns_api_ingest() -> None:
    api, _, transport = api_with(
        (200, {"data": [channel()]}),
        (204, None),
    )

    ingest, remote_ids = api.prepare(
        StreamMetadata(title="New title", category="15", tags=["music", "live"])
    )

    assert ingest.server_url == (
        "rtmps://fa723fc1b171.global-contribute.live-video.net/app"
    )
    assert ingest.stream_key == SecretStr("secret-stream-key")
    assert remote_ids == {"broadcaster_id": "1234"}
    assert urllib.parse.parse_qs(
        urllib.parse.urlsplit(transport.requests[0].url).query
    ) == {"slug": ["channel-name"]}
    assert json.loads(transport.requests[1].body or b"{}") == {
        "stream_title": "New title",
        "category_id": 15,
        "custom_tags": ["music", "live"],
    }


def test_prepare_without_metadata_does_not_update_channel() -> None:
    api, _, transport = api_with((200, {"data": [channel()]}))

    ingest, _ = api.prepare(StreamMetadata())

    assert ingest.stream_key == SecretStr("secret-stream-key")
    assert len(transport.requests) == 1


def test_chat_uses_prepared_broadcaster_id() -> None:
    api, _, transport = api_with(
        (200, {"data": {"message_id": "message-1", "is_sent": True}}),
    )
    api.broadcaster_id = 1234

    response = api.perform("chat", {"message": "Hello"})

    assert response["data"] == {"message_id": "message-1", "is_sent": True}
    assert json.loads(transport.requests[0].body or b"{}") == {
        "broadcaster_user_id": 1234,
        "content": "Hello",
        "type": "user",
    }


def test_health_reports_live_state_and_viewers() -> None:
    api, _, _ = api_with((200, {"data": [channel(live=True)]}))

    assert api.health() == ("live", "viewers=42")


def test_health_reports_offline_without_stream() -> None:
    offline_channel = channel()
    offline_channel["stream"] = None
    api, _, _ = api_with((200, {"data": [offline_channel]}))

    assert api.health() == ("offline", None)


def test_refresh_persists_rotated_refresh_token(tmp_path: Path) -> None:
    path = tmp_path / "kick-auth.toml"
    path.write_text(
        'client_id = "client-id"\n'
        'client_secret = "client-secret"\n'
        'refresh_token = "old-refresh-token"\n'
    )
    transport = FakeTransport(
        [
            (
                200,
                {
                    "access_token": "new-access-token",
                    "refresh_token": "new-refresh-token",
                    "expires_in": 3600,
                },
            )
        ]
    )
    provider = KickAccessTokenProvider(
        StoredKickCredentials(
            client_id="client-id",
            client_secret=SecretStr("client-secret"),
            refresh_token=SecretStr("old-refresh-token"),
        ),
        path,
        transport,
    )

    assert provider.access_token() == "new-access-token"
    assert tomllib.loads(path.read_text())["refresh_token"] == "new-refresh-token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    form = urllib.parse.parse_qs((transport.requests[0].body or b"").decode())
    assert form == {
        "grant_type": ["refresh_token"],
        "client_id": ["client-id"],
        "client_secret": ["client-secret"],
        "refresh_token": ["old-refresh-token"],
    }
