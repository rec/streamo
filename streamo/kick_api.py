import json
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Self, cast

from pydantic import BaseModel, SecretStr, ValidationError

from .credentials import write_private_toml
from .services import KickService, RtmpIngest, StreamMetadata

KICK_API_URL = "https://api.kick.com/public/v1"
KICK_TOKEN_URL = "https://id.kick.com/oauth/token"


class KickApiError(ValueError):
    pass


class KickClientSecrets(BaseModel, frozen=True):
    client_id: str
    client_secret: SecretStr


class StoredKickCredentials(KickClientSecrets, frozen=True):
    refresh_token: SecretStr


class KickToken(BaseModel, frozen=True):
    access_token: SecretStr
    refresh_token: SecretStr
    expires_in: int


class AccessTokenProvider(Protocol):
    def access_token(self, *, force_refresh: bool = False) -> str: ...


@dataclass(frozen=True)
class KickRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


def urllib_transport(request: KickRequest) -> tuple[int, bytes]:
    url_request = urllib.request.Request(
        request.url,
        data=request.body,
        headers=request.headers,
        method=request.method,
    )
    try:
        with urllib.request.urlopen(url_request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except urllib.error.URLError as error:
        raise KickApiError(f"Kick API request failed: {error.reason}") from error


def exchange_kick_code(
    client: KickClientSecrets,
    code: str,
    verifier: str,
    redirect_uri: str,
    transport: Callable[[KickRequest], tuple[int, bytes]] = urllib_transport,
) -> KickToken:
    return request_kick_token(
        {
            "grant_type": "authorization_code",
            "client_id": client.client_id,
            "client_secret": client.client_secret.get_secret_value(),
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        transport,
    )


class KickAccessTokenProvider:
    def __init__(
        self,
        credentials: StoredKickCredentials,
        path: Path,
        transport: Callable[[KickRequest], tuple[int, bytes]] = urllib_transport,
    ) -> None:
        self.credentials = credentials
        self.path = path
        self.transport = transport
        self.token: str | None = None
        self.expires_at = 0.0
        self.lock = threading.Lock()

    def access_token(self, *, force_refresh: bool = False) -> str:
        with self.lock:
            if (
                force_refresh
                or self.token is None
                or time.monotonic() >= self.expires_at
            ):
                self.refresh()
            assert self.token is not None
            return self.token

    def refresh(self) -> None:
        token = request_kick_token(
            {
                "grant_type": "refresh_token",
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret.get_secret_value(),
                "refresh_token": self.credentials.refresh_token.get_secret_value(),
            },
            self.transport,
        )
        credentials = StoredKickCredentials(
            client_id=self.credentials.client_id,
            client_secret=self.credentials.client_secret,
            refresh_token=token.refresh_token,
        )
        write_private_toml(
            self.path,
            {
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret.get_secret_value(),
                "refresh_token": credentials.refresh_token.get_secret_value(),
            },
        )
        self.credentials = credentials
        self.token = token.access_token.get_secret_value()
        self.expires_at = time.monotonic() + max(token.expires_in - 60, 0)


@dataclass
class KickApi:
    tokens: AccessTokenProvider
    channel_slug: str
    transport: Callable[[KickRequest], tuple[int, bytes]] = urllib_transport
    api_url: str = KICK_API_URL
    broadcaster_id: int | None = None

    @classmethod
    def from_service(cls, service: KickService) -> Self | None:
        if service.credentials is None or service.channel is None:
            return None
        path = service.credentials.expanduser()
        credentials = load_toml_model(path, StoredKickCredentials, "Kick credentials")
        return cls(
            tokens=KickAccessTokenProvider(credentials, path),
            channel_slug=service.channel,
            api_url=service.api_url,
        )

    def prepare(self, metadata: StreamMetadata) -> tuple[RtmpIngest, dict[str, str]]:
        channel = self.channel()
        stream = object_mapping(channel.get("stream"), "channel stream")
        server_url = string_value(stream, "url")
        stream_key = string_value(stream, "key")
        if urllib.parse.urlsplit(server_url).scheme != "rtmps":
            raise KickApiError("Kick did not return an RTMPS ingest URL")
        if not stream_key:
            raise KickApiError("Kick did not return a stream key")
        broadcaster_id = integer_value(channel, "broadcaster_user_id")
        ingest = RtmpIngest(
            protocol="rtmps",
            server_url=server_url,
            stream_key=SecretStr(stream_key),
        )
        self.broadcaster_id = broadcaster_id
        self.update_metadata(metadata)
        return ingest, {"broadcaster_id": str(broadcaster_id)}

    def perform(self, command: str, payload: Mapping[str, object]) -> dict[str, object]:
        if command == "update_stream_info":
            self.update_channel(kick_metadata_body(payload))
            return {}
        if command == "chat":
            message = required_string(payload, "message")
            return self.send_chat_message(message)
        raise KickApiError(f"unsupported Kick API command {command}")

    def update_metadata(self, metadata: StreamMetadata) -> None:
        self.update_channel(
            kick_metadata_body(
                metadata.model_dump(exclude_none=True, exclude_defaults=True)
            )
        )

    def update_channel(self, body: Mapping[str, object]) -> None:
        if body:
            self.request("PATCH", "channels", body=body)

    def send_chat_message(self, message: str) -> dict[str, object]:
        if self.broadcaster_id is None:
            self.broadcaster_id = integer_value(self.channel(), "broadcaster_user_id")
        return self.request(
            "POST",
            "chat",
            body={
                "broadcaster_user_id": self.broadcaster_id,
                "content": message,
                "type": "user",
            },
        )

    def health(self) -> tuple[str, str | None]:
        channel = self.channel()
        if channel.get("stream") is None:
            return "offline", None
        stream = object_mapping(channel["stream"], "channel stream")
        is_live = boolean_value(stream, "is_live")
        viewers = integer_value(stream, "viewer_count")
        return ("live" if is_live else "offline"), f"viewers={viewers}"

    def channel(self) -> dict[str, object]:
        response = self.request("GET", "channels", query={"slug": self.channel_slug})
        data = response.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise KickApiError(f"Kick channel not found: {self.channel_slug}")
        return cast(dict[str, object], data[0])

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        query: Mapping[str, object] | None = None,
        body: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        url = f"{self.api_url.rstrip('/')}/{endpoint.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        encoded = None if body is None else json.dumps(body).encode()
        for attempt in range(2):
            token = self.tokens.access_token(force_refresh=attempt == 1)
            status, data = self.transport(
                KickRequest(
                    method=method,
                    url=url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    body=encoded,
                )
            )
            if status == 401 and attempt == 0:
                continue
            if not 200 <= status < 300:
                raise KickApiError(kick_error(status, data))
            if not data:
                return {}
            try:
                result = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise KickApiError("Kick API response was not valid JSON") from error
            if not isinstance(result, dict):
                raise KickApiError("Kick API response was not an object")
            return result
        raise AssertionError("unreachable")


def request_kick_token(
    values: Mapping[str, str],
    transport: Callable[[KickRequest], tuple[int, bytes]],
) -> KickToken:
    status, data = transport(
        KickRequest(
            method="POST",
            url=KICK_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=urllib.parse.urlencode(values).encode(),
        )
    )
    if not 200 <= status < 300:
        raise KickApiError(kick_error(status, data))
    try:
        return KickToken.model_validate_json(data)
    except ValidationError as error:
        raise KickApiError("Kick returned an invalid OAuth token") from error


def kick_metadata_body(payload: Mapping[str, object]) -> dict[str, object]:
    body: dict[str, object] = {}
    if "title" in payload:
        body["stream_title"] = required_string(payload, "title")
    if "category_id" in payload:
        body["category_id"] = positive_integer(payload["category_id"], "category_id")
    elif "category" in payload:
        body["category_id"] = positive_integer(payload["category"], "category")
    if "tags" in payload:
        tags = string_list(payload["tags"], "tags")
        if len(tags) > 10:
            raise KickApiError("tags must contain at most 10 values")
        body["custom_tags"] = tags
    return body


def load_toml_model[T: BaseModel](path: Path, model: type[T], name: str) -> T:
    try:
        data = tomllib.loads(path.expanduser().read_text())
        return model.model_validate(data)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as error:
        raise KickApiError(f"Could not load {name} from {path}") from error


def object_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise KickApiError(f"Kick response has no {name}")
    return cast(dict[str, object], value)


def string_value(values: Mapping[str, object], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str):
        raise KickApiError(f"Kick response has no {name}")
    return value


def integer_value(values: Mapping[str, object], name: str) -> int:
    value = values.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise KickApiError(f"Kick response has no {name}")
    return value


def boolean_value(values: Mapping[str, object], name: str) -> bool:
    value = values.get(name)
    if not isinstance(value, bool):
        raise KickApiError(f"Kick response has no {name}")
    return value


def required_string(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise KickApiError(f"{name} is required")
    return value


def positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise KickApiError(f"{name} must be a positive integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdecimal():
        result = int(value)
    else:
        raise KickApiError(f"{name} must be a positive integer")
    if result <= 0:
        raise KickApiError(f"{name} must be a positive integer")
    return result


def string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise KickApiError(f"{name} must be a list of strings")
    return cast(list[str], value)


def kick_error(status: int, data: bytes) -> str:
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return f"Kick API returned HTTP {status}"
    if isinstance(payload, dict):
        for name in ("message", "error"):
            if isinstance((message := payload.get(name)), str):
                return f"Kick API {status}: {message}"
    return f"Kick API returned HTTP {status}"
