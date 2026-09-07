import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from .services import TwitchService


@dataclass
class TwitchRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


class TwitchApiError(Exception):
    pass


class TwitchApiClient(Protocol):
    def perform(self, command: str, payload: Mapping[str, object]) -> dict[str, object]:
        pass


def urllib_transport(request: TwitchRequest) -> tuple[int, bytes]:
    urllib_request = urllib.request.Request(
        request.url,
        data=request.body,
        headers=request.headers,
        method=request.method,
    )
    try:
        with urllib.request.urlopen(urllib_request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        raise TwitchApiError(f"Twitch API {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise TwitchApiError(f"Twitch API request failed: {error.reason}") from error


@dataclass
class TwitchApi:
    client_id: str
    access_token: str
    broadcaster_id: str
    sender_id: str
    moderator_id: str
    api_url: str = "https://api.twitch.tv/helix"
    transport: Callable[[TwitchRequest], tuple[int, bytes]] = urllib_transport

    @classmethod
    def from_service(cls, service: TwitchService) -> "TwitchApi | None":
        if (
            service.client_id is None
            or service.access_token is None
            or service.broadcaster_id is None
        ):
            return None
        return cls(
            client_id=service.client_id.get_secret_value(),
            access_token=service.access_token.get_secret_value(),
            broadcaster_id=service.broadcaster_id,
            sender_id=service.sender_id or service.broadcaster_id,
            moderator_id=service.moderator_id or service.broadcaster_id,
            api_url=service.api_url,
        )

    def perform(self, command: str, payload: Mapping[str, object]) -> dict[str, object]:
        if command == "update_stream_info":
            return self.update_stream_info(payload)
        if command == "chat":
            return self.send_chat_message(payload)
        if command == "announce":
            return self.send_announcement(payload)
        if command == "clip":
            return self.create_clip(payload)
        if command == "marker":
            return self.create_marker(payload)
        raise TwitchApiError(f"unsupported Twitch API command {command}")

    def update_stream_info(self, payload: Mapping[str, object]) -> dict[str, object]:
        body = self.channel_update_body(payload)
        if not body:
            raise TwitchApiError("update_stream_info requires at least one field")
        self.request(
            "PATCH",
            "channels",
            query={"broadcaster_id": self.broadcaster_id},
            body=body,
        )
        return {}

    def send_chat_message(self, payload: Mapping[str, object]) -> dict[str, object]:
        message = required_string(payload, "message")
        body: dict[str, object] = {
            "broadcaster_id": self.broadcaster_id,
            "sender_id": self.sender_id,
            "message": message,
        }
        copy_optional(
            payload,
            body,
            "reply_parent_message_id",
            "for_source_only",
            "pin",
        )
        return self.request("POST", "chat/messages", body=body)

    def send_announcement(self, payload: Mapping[str, object]) -> dict[str, object]:
        body: dict[str, object] = {"message": required_string(payload, "message")}
        copy_optional(payload, body, "color", "for_source_only")
        self.request(
            "POST",
            "chat/announcements",
            query={
                "broadcaster_id": self.broadcaster_id,
                "moderator_id": self.moderator_id,
            },
            body=body,
        )
        return {}

    def create_clip(self, payload: Mapping[str, object]) -> dict[str, object]:
        query: dict[str, object] = {"broadcaster_id": self.broadcaster_id}
        copy_optional(payload, query, "has_delay")
        return self.request("POST", "clips", query=query)

    def create_marker(self, payload: Mapping[str, object]) -> dict[str, object]:
        body: dict[str, object] = {"user_id": self.broadcaster_id}
        copy_optional(payload, body, "description")
        return self.request("POST", "streams/markers", body=body)

    def channel_update_body(self, payload: Mapping[str, object]) -> dict[str, object]:
        body: dict[str, object] = {}
        copy_optional(
            payload,
            body,
            "title",
            "game_id",
            "broadcaster_language",
            "delay",
            "tags",
            "content_classification_labels",
            "is_branded_content",
        )
        if "category_id" in payload:
            body["game_id"] = payload["category_id"]
        if "category" in payload and "game_id" not in body:
            body["game_id"] = self.search_category(required_string(payload, "category"))
        return body

    def search_category(self, name: str) -> str:
        response = self.request(
            "GET", "search/categories", query={"query": name, "first": 20}
        )
        data = response.get("data")
        if not isinstance(data, list) or not data:
            raise TwitchApiError(f"Twitch category not found: {name}")
        categories: list[dict[str, object]] = [
            cast(dict[str, object], c) for c in data if isinstance(c, dict)
        ]
        exact = [c for c in categories if c.get("name") == name]
        casefolded = [
            c
            for c in categories
            if isinstance((category_name := c.get("name")), str)
            and category_name.casefold() == name.casefold()
        ]
        category = (exact or casefolded or categories)[0]
        category_id = category.get("id")
        if not isinstance(category_id, str):
            raise TwitchApiError(f"Twitch category response missing id: {name}")
        return category_id

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, object] | None = None,
        body: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        url = f"{self.api_url.rstrip('/')}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Client-Id": self.client_id,
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":")).encode()
        status, response = self.transport(TwitchRequest(method, url, headers, data))
        if status >= 400:
            raise TwitchApiError(
                f"Twitch API {status}: {response.decode(errors='replace')}"
            )
        if status == 204 or not response:
            return {}
        parsed = json.loads(response)
        if not isinstance(parsed, dict):
            raise TwitchApiError("Twitch API response was not an object")
        return parsed


def required_string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise TwitchApiError(f"{name} is required")
    return value


def copy_optional(
    source: Mapping[str, object], target: dict[str, object], *names: str
) -> None:
    for name in names:
        if name in source:
            target[name] = source[name]
