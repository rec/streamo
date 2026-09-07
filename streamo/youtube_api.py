import json
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, Self, cast

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from pydantic import BaseModel, SecretStr, ValidationError

from .auth import YOUTUBE_SCOPE
from .services import HlsPushIngest, RtmpIngest, StreamMetadata, YouTubeService

YOUTUBE_API_URL = "https://www.googleapis.com/youtube/v3"
YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class YouTubeApiError(ValueError):
    pass


class StoredYouTubeCredentials(BaseModel, frozen=True):
    client_id: str
    client_secret: SecretStr
    refresh_token: SecretStr


class AccessTokenProvider(Protocol):
    def access_token(self, *, force_refresh: bool = False) -> str: ...


class GoogleAccessTokenProvider:
    def __init__(self, credentials: Credentials) -> None:
        self.credentials = credentials
        self.lock = threading.Lock()

    def access_token(self, *, force_refresh: bool = False) -> str:
        with self.lock:
            if force_refresh or not self.credentials.valid:
                try:
                    self.credentials.refresh(Request())
                except RefreshError as error:
                    raise YouTubeApiError(
                        "YouTube authorization has expired; run streamo auth youtube"
                    ) from error
            if self.credentials.token is None:
                raise YouTubeApiError("Google did not return a YouTube access token")
            return self.credentials.token


@dataclass(frozen=True)
class YouTubeRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


def urllib_transport(request: YouTubeRequest) -> tuple[int, bytes]:
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
        raise YouTubeApiError(f"YouTube API request failed: {error.reason}") from error


@dataclass
class YouTubeApi:
    tokens: AccessTokenProvider
    transport: Callable[[YouTubeRequest], tuple[int, bytes]] = urllib_transport
    api_url: str = YOUTUBE_API_URL

    @classmethod
    def from_service(cls, service: YouTubeService) -> Self | None:
        if service.credentials is None:
            return None
        stored = load_credentials(service.credentials)
        credentials = Credentials(
            token=None,
            refresh_token=stored.refresh_token.get_secret_value(),
            token_uri=YOUTUBE_TOKEN_URL,
            client_id=stored.client_id,
            client_secret=stored.client_secret.get_secret_value(),
            scopes=[YOUTUBE_SCOPE],
        )
        return cls(tokens=GoogleAccessTokenProvider(credentials))

    def prepare(
        self, service: YouTubeService, metadata: StreamMetadata
    ) -> tuple[RtmpIngest | HlsPushIngest, dict[str, str]]:
        assert service.stream_id is not None
        assert service.broadcast_id is not None
        stream = self.resource("liveStreams", service.stream_id, "cdn,status")
        broadcast = self.resource(
            "liveBroadcasts",
            service.broadcast_id,
            "snippet,status,contentDetails",
        )
        self.update_broadcast(service, metadata, broadcast, configure_lifecycle=True)
        self.request(
            "POST",
            "liveBroadcasts/bind",
            query={
                "part": "id,snippet",
                "id": service.broadcast_id,
                "streamId": service.stream_id,
            },
        )
        return youtube_ingest(stream, service), {
            "stream_id": service.stream_id,
            "broadcast_id": service.broadcast_id,
        }

    def update_metadata(
        self, service: YouTubeService, metadata: StreamMetadata
    ) -> None:
        assert service.broadcast_id is not None
        broadcast = self.resource(
            "liveBroadcasts",
            service.broadcast_id,
            "snippet,status",
        )
        self.update_broadcast(service, metadata, broadcast, configure_lifecycle=False)

    def health(self, stream_id: str) -> tuple[str, str | None]:
        stream = self.resource("liveStreams", stream_id, "status")
        status = object_mapping(stream.get("status"), "live stream status")
        stream_status = string_value(status, "streamStatus")
        health = object_mapping(status.get("healthStatus"), "health status")
        health_status = string_value(health, "status")
        issues = health.get("configurationIssues")
        details: list[str] = []
        if isinstance(issues, list):
            for issue in issues:
                if isinstance(issue, dict):
                    issue_values = cast(dict[str, object], issue)
                    if isinstance((issue_type := issue_values.get("type")), str):
                        details.append(issue_type)
        detail = f"health={health_status}"
        if details:
            detail = f"{detail}; issues={', '.join(details)}"
        return stream_status, detail

    def update_broadcast(
        self,
        service: YouTubeService,
        metadata: StreamMetadata,
        broadcast: Mapping[str, object],
        *,
        configure_lifecycle: bool,
    ) -> None:
        assert service.broadcast_id is not None
        snippet = object_mapping(broadcast.get("snippet"), "broadcast snippet")
        status = object_mapping(broadcast.get("status"), "broadcast status")
        updated_snippet: dict[str, object] = {
            "title": metadata.title or string_value(snippet, "title"),
            "description": (
                metadata.description
                if metadata.description is not None
                else string_value(snippet, "description", default="")
            ),
            "scheduledStartTime": (
                rfc3339(metadata.scheduled_start)
                if metadata.scheduled_start is not None
                else string_value(snippet, "scheduledStartTime")
            ),
        }
        copy_fields(snippet, updated_snippet, "scheduledEndTime", "categoryId")
        if metadata.category is not None:
            updated_snippet["categoryId"] = metadata.category
        updated_status: dict[str, object] = {
            "privacyStatus": metadata.privacy or string_value(status, "privacyStatus")
        }
        copy_fields(status, updated_status, "selfDeclaredMadeForKids")
        parts = "snippet,status"
        body: dict[str, object] = {
            "id": service.broadcast_id,
            "snippet": updated_snippet,
            "status": updated_status,
        }
        if configure_lifecycle:
            content = object_mapping(
                broadcast.get("contentDetails"), "broadcast content details"
            )
            monitor = object_mapping(content.get("monitorStream"), "monitor stream")
            updated_content: dict[str, object] = {
                "monitorStream": {
                    "enableMonitorStream": bool(monitor.get("enableMonitorStream")),
                    "broadcastStreamDelayMs": int(
                        cast(int, monitor.get("broadcastStreamDelayMs", 0))
                    ),
                },
                "enableAutoStart": service.auto_start,
                "enableAutoStop": service.auto_stop,
            }
            copy_fields(
                content,
                updated_content,
                "enableClosedCaptions",
                "enableDvr",
                "enableEmbed",
                "recordFromStart",
                "availabilityConfig",
            )
            parts = f"{parts},contentDetails"
            body["contentDetails"] = updated_content
        self.request(
            "PUT",
            "liveBroadcasts",
            query={"part": parts},
            body=body,
        )

    def resource(self, endpoint: str, resource_id: str, part: str) -> dict[str, object]:
        response = self.request(
            "GET", endpoint, query={"part": part, "id": resource_id}
        )
        items = response.get("items")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise YouTubeApiError(
                f"YouTube {endpoint} resource {resource_id} not found"
            )
        return cast(dict[str, object], items[0])

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        query: Mapping[str, object] | None = None,
        body: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        url = f"{self.api_url.rstrip('/')}/{endpoint}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        encoded = None if body is None else json.dumps(body).encode()
        for attempt in range(2):
            token = self.tokens.access_token(force_refresh=attempt == 1)
            status, data = self.transport(
                YouTubeRequest(
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
                raise YouTubeApiError(youtube_error(status, data))
            if not data:
                return {}
            result = json.loads(data)
            if not isinstance(result, dict):
                raise YouTubeApiError("YouTube API response was not an object")
            return result
        raise AssertionError("unreachable")


def load_credentials(path: Path) -> StoredYouTubeCredentials:
    try:
        data = tomllib.loads(path.expanduser().read_text())
        return StoredYouTubeCredentials.model_validate(data)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as error:
        raise YouTubeApiError(
            f"Could not load YouTube credentials from {path}"
        ) from error


def youtube_ingest(
    stream: Mapping[str, object], service: YouTubeService
) -> RtmpIngest | HlsPushIngest:
    cdn = object_mapping(stream.get("cdn"), "live stream CDN")
    information = object_mapping(cdn.get("ingestionInfo"), "ingestion information")
    stream_name = string_value(information, "streamName")
    ingestion_type = string_value(cdn, "ingestionType")
    if ingestion_type == "rtmp":
        address = string_value(
            information,
            "rtmpsIngestionAddress",
            default=string_value(information, "ingestionAddress"),
        )
        scheme = urllib.parse.urlsplit(address).scheme
        if scheme not in {"rtmp", "rtmps"}:
            raise YouTubeApiError(f"Unsupported YouTube RTMP address {address}")
        return RtmpIngest(
            protocol="rtmps" if scheme == "rtmps" else "rtmp",
            server_url=address,
            stream_key=SecretStr(stream_name),
        )
    if ingestion_type == "hls":
        address = string_value(information, "ingestionAddress")
        duration = (
            service.ingest.segment_duration
            if isinstance(service.ingest, HlsPushIngest)
            else 2.0
        )
        return HlsPushIngest(
            protocol="hls",
            upload_url=hls_upload_url(address, stream_name),
            stream_key=SecretStr(stream_name),
            segment_duration=duration,
        )
    raise YouTubeApiError(f"Unsupported YouTube ingestion type {ingestion_type}")


def hls_upload_url(address: str, stream_name: str) -> str:
    for value in (stream_name, urllib.parse.quote(stream_name, safe="")):
        if value in address:
            template = address.replace(value, "{stream_key}", 1)
            break
    else:
        raise YouTubeApiError(
            "YouTube HLS ingestion address does not contain its stream name"
        )
    if not template.endswith("file="):
        raise YouTubeApiError("YouTube HLS ingestion address has no file parameter")
    return f"{template}stream.m3u8"


def object_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise YouTubeApiError(f"YouTube response has no {name}")
    return cast(dict[str, object], value)


def string_value(
    values: Mapping[str, object], name: str, *, default: str | None = None
) -> str:
    value = values.get(name, default)
    if not isinstance(value, str):
        raise YouTubeApiError(f"YouTube response has no {name}")
    return value


def copy_fields(
    source: Mapping[str, object], target: dict[str, object], *names: str
) -> None:
    for name in names:
        if name in source:
            target[name] = source[name]


def rfc3339(value: datetime) -> str:
    if value.utcoffset() is None:
        raise YouTubeApiError("YouTube scheduled_start must include a timezone")
    return value.isoformat()


def youtube_error(status: int, data: bytes) -> str:
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return f"YouTube API returned HTTP {status}"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return f"YouTube API {status}: {error['message']}"
    return f"YouTube API returned HTTP {status}"
