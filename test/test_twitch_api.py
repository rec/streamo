from __future__ import annotations

import json

import pytest

from streamo.twitch_api import TwitchApi, TwitchApiError, TwitchRequest


def _api(transport: FakeTransport) -> TwitchApi:
    return TwitchApi(
        client_id="client",
        access_token="token",
        broadcaster_id="broadcaster",
        sender_id="sender",
        moderator_id="moderator",
        api_url="https://example.test/helix",
        transport=transport,
    )


def test_update_stream_info_resolves_category_and_updates_channel() -> None:
    transport = FakeTransport(
        [
            (200, {"data": [{"id": "26936", "name": "Music"}]}),
            (204, {}),
        ]
    )

    result = _api(transport).perform(
        "update_stream_info",
        {"title": "Tonight", "category": "Music", "tags": ["live"]},
    )

    assert result == {}
    assert transport.requests[0].method == "GET"
    assert (
        transport.requests[0].url
        == "https://example.test/helix/search/categories?query=Music&first=20"
    )
    assert transport.requests[1].method == "PATCH"
    assert (
        transport.requests[1].url
        == "https://example.test/helix/channels?broadcaster_id=broadcaster"
    )
    assert json.loads(transport.requests[1].body or b"{}") == {
        "title": "Tonight",
        "tags": ["live"],
        "game_id": "26936",
    }


def test_chat_sends_message() -> None:
    transport = FakeTransport([(200, {"data": [{"message_id": "message-1"}]})])

    result = _api(transport).perform("chat", {"message": "Starting now"})

    assert result == {"data": [{"message_id": "message-1"}]}
    assert transport.requests[0].method == "POST"
    assert transport.requests[0].url == "https://example.test/helix/chat/messages"
    assert json.loads(transport.requests[0].body or b"{}") == {
        "broadcaster_id": "broadcaster",
        "sender_id": "sender",
        "message": "Starting now",
    }


def test_announcement_sends_message() -> None:
    transport = FakeTransport([(204, {})])

    _api(transport).perform("announce", {"message": "Now live", "color": "purple"})

    assert transport.requests[0].method == "POST"
    assert (
        transport.requests[0].url
        == "https://example.test/helix/chat/announcements?broadcaster_id=broadcaster&moderator_id=moderator"
    )
    assert json.loads(transport.requests[0].body or b"{}") == {
        "message": "Now live",
        "color": "purple",
    }


def test_clip_creates_clip() -> None:
    transport = FakeTransport([(202, {"data": [{"id": "clip-1"}]})])

    result = _api(transport).perform("clip", {})

    assert result == {"data": [{"id": "clip-1"}]}
    assert transport.requests[0].method == "POST"
    assert (
        transport.requests[0].url
        == "https://example.test/helix/clips?broadcaster_id=broadcaster"
    )
    assert transport.requests[0].body is None


def test_marker_creates_stream_marker() -> None:
    transport = FakeTransport([(200, {"data": [{"id": "marker-1"}]})])

    result = _api(transport).perform("marker", {"description": "First song"})

    assert result == {"data": [{"id": "marker-1"}]}
    assert transport.requests[0].method == "POST"
    assert transport.requests[0].url == "https://example.test/helix/streams/markers"
    assert json.loads(transport.requests[0].body or b"{}") == {
        "user_id": "broadcaster",
        "description": "First song",
    }


def test_required_fields_are_validated_before_request() -> None:
    transport = FakeTransport([])

    with pytest.raises(TwitchApiError, match="message is required"):
        _api(transport).perform("chat", {})

    assert transport.requests == []


class FakeTransport:
    def __init__(self, responses: list[tuple[int, dict[str, object]]]) -> None:
        self.responses = responses
        self.requests: list[TwitchRequest] = []

    def __call__(self, request: TwitchRequest) -> tuple[int, bytes]:
        self.requests.append(request)
        status, response = self.responses.pop(0)
        return status, json.dumps(response).encode()
