# General streaming services

## Goal

Replace Streamo's Twitch-specific output configuration with a streaming-service
model that can describe any FFmpeg-addressable live video or audio destination.
Streamo must support generic protocol destinations without code changes, while
named service adapters add API operations that are unique to Twitch, YouTube,
Facebook, Kick, Vimeo, LinkedIn, and Icecast.

This change concerns producer ingest: services to which an encoder can publish
a live stream. Consumer-only music and video services that do not expose an
encoder ingest protocol are not streaming destinations in this sense and are
outside Streamo's scope.

Keep one configured destination per Streamo process. Simultaneous publication
to several services is a separate reliability and resource-management problem;
it is not necessary to represent or support each service correctly.

## Service survey

The initial catalog should cover these important service families:

| Service | Media | Ingest | Authentication | Distinct service behavior |
| --- | --- | --- | --- | --- |
| Twitch | audio and video | RTMP | stream key in the publish path | channel metadata, chat, announcements, clips, and markers through Helix |
| YouTube Live | audio and video | RTMPS, RTMP, HLS | stream name/key; OAuth for management | separate stream and broadcast resources, scheduling, lifecycle transitions, health, DVR, privacy, and cue points |
| Facebook Live | audio and video | RTMPS/RTMP | stream key or Live Video API result | publish to a profile, Page, group, or event; description and Live Video lifecycle |
| Kick | audio and video | RTMPS | server URL and stream key | title and category are currently managed separately from encoder ingest |
| Vimeo | audio and video | RTMPS/RTMP, with SRT on eligible plans | event URL and stream key | event lifecycle, archive, privacy, and optional primary/backup ingest |
| LinkedIn Live | audio and video | RTMP/RTMPS custom stream | event URL and stream key | scheduled event, time-limited preparation/preview, and an explicit operator Go Live action |
| Icecast | audio only | HTTP source connection, optionally TLS | source username/password | mountpoint, content type, stream metadata, listener statistics, and source administration |
| Custom | audio, video, or both | any explicitly supported FFmpeg output protocol | protocol-specific | no branded control API; allows another CDN, self-hosted server, or future service immediately |

The catalog is deliberately not an enum used to gate generic ingest. A new
RTMP endpoint must work as `custom` before Streamo has a branded adapter for it.

Primary references:

- [Twitch video broadcast](https://dev.twitch.tv/docs/video-broadcast/) defines
  an RTMP ingest server plus stream key and documents bandwidth-test mode.
- [YouTube liveStream resources](https://developers.google.com/youtube/v3/live/docs/liveStreams)
  expose RTMP, RTMPS, HLS, primary and backup ingest addresses, and stream
  health; [liveBroadcast resources](https://developers.google.com/youtube/v3/live/docs/liveBroadcasts)
  carry event metadata and lifecycle.
- [YouTube encoder settings](https://support.google.com/youtube/answer/2853702)
  document codec, bitrate, resolution, frame-rate, and keyframe constraints.
- [Kick encoder setup](https://help.kick.com/en/articles/7066931-how-to-stream-on-kick-com)
  provides an RTMPS server URL and stream key and documents its bitrate limit.
- [Vimeo live-event FAQ](https://help.vimeo.com/hc/en-us/articles/12426923921681-FAQ-Live-events)
  provides an RTMPS URL and key; [Vimeo backup streams](https://help.vimeo.com/hc/en-us/articles/12426985863313-How-to-create-a-backup-stream)
  add RTMP/RTMPS/SRT and primary/backup behavior.
- [LinkedIn custom-stream setup](https://www.linkedin.com/help/linkedin/answer/a564446)
  describes event-specific RTMP URL/key generation, preview, and the separate
  operator-controlled Go Live transition.
- [Icecast basic setup](https://www.icecast.org/docs/icecast-trunk/basic_setup/)
  defines source host, port, password, and mountpoint; its
  [configuration reference](https://www.icecast.org/docs/icecast-latest/config_file/)
  adds mount-specific source credentials and settings.

Facebook's current official help requires login for some encoder details. Its
adapter must be checked against the Live Video API documentation immediately
before implementation rather than copying a third-party description into code.

## Common characteristics

Every supported destination has the following base concerns, even when the
service leaves some of them to an operator rather than an API:

1. **Media contract:** audio-only or audio-and-video, codecs, container, sample
   rate, channel count, resolution, frame rate, bitrate mode, bitrate bounds,
   and keyframe interval.
2. **Ingest:** protocol, server or publish URL, path or mountpoint, credentials,
   optional backup endpoint, and FFmpeg muxer options.
3. **Lifecycle:** prepare an event, begin sending media, make the event public,
   observe health, stop publishing, and finish/archive the event. Some services
   infer lifecycle from the media connection; others require API or manual
   transitions.
4. **Metadata:** title, description, category, tags, language, privacy, and
   scheduled start time. The common vocabulary is intentionally small because
   platform schemas differ.
5. **Interaction:** chat, announcements, clips, markers/cue points, and other
   platform-specific actions.
6. **Observability:** local FFmpeg status, remote ingest health when available,
   public watch URL, and service-specific error detail.
7. **Secrets:** stream keys, source passwords, OAuth tokens, and service client
   credentials must validate without being rendered in logs, status, or command
   diagnostics.

Transport and service API are independent. For example, Twitch and Kick have
similar RTMP-family ingest, but do not expose the same control operations.
YouTube can use more than one ingest protocol for the same service API. Icecast
has no video track and uses a mountpoint instead of an event stream key.

## Data model

Use frozen Pydantic models, consistent with Streamo's configuration. Avoid one
large model containing optional fields for every provider. A shared base model
holds the complete producer contract, and a discriminated union of subclasses
adds only the fields belonging to a named service.

The intended shape is:

```python
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr


class AudioEncoding(BaseModel, frozen=True):
    codec: Literal["aac", "mp3", "opus", "vorbis"]
    bitrate: str
    sample_rate: int
    channels: Literal[1, 2]


class VideoEncoding(BaseModel, frozen=True):
    codec: Literal["h264", "hevc", "av1"]
    bitrate: str
    resolution: str
    frame_rate: int
    keyframe_interval: float
    pixel_format: str = "yuv420p"


class EncodingProfile(BaseModel, frozen=True):
    container: Literal["flv", "mpegts", "ogg", "mp3", "adts"]
    audio: AudioEncoding
    video: VideoEncoding | None = None


class RtmpIngest(BaseModel, frozen=True):
    protocol: Literal["rtmp", "rtmps"]
    server_url: str
    stream_key: SecretStr
    backup_server_url: str | None = None
    backup_stream_key: SecretStr | None = None


class SrtIngest(BaseModel, frozen=True):
    protocol: Literal["srt"]
    url: str
    passphrase: SecretStr | None = None
    latency_ms: int = 120


class HlsPushIngest(BaseModel, frozen=True):
    protocol: Literal["hls"]
    upload_url: str
    stream_key: SecretStr
    segment_duration: float


class IcecastIngest(BaseModel, frozen=True):
    protocol: Literal["icecast"]
    server_url: str
    mountpoint: str
    username: str = "source"
    password: SecretStr


class StreamMetadata(BaseModel, frozen=True):
    title: str | None = None
    description: str | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    language: str | None = None
    privacy: Literal["public", "unlisted", "private"] | None = None
    scheduled_start: datetime | None = None


class StreamingService(BaseModel, frozen=True):
    name: str
    ingest: Annotated[
        RtmpIngest | SrtIngest | HlsPushIngest | IcecastIngest,
        Field(discriminator="protocol"),
    ]
    encoding: EncodingProfile
    metadata: StreamMetadata = Field(default_factory=StreamMetadata)


class TwitchService(StreamingService, frozen=True):
    service: Literal["twitch"]
    client_id: SecretStr | None = None
    access_token: SecretStr | None = None
    broadcaster_id: str | None = None
    sender_id: str | None = None
    moderator_id: str | None = None
    bandwidth_test: bool = False


class YouTubeService(StreamingService, frozen=True):
    service: Literal["youtube"]
    access_token: SecretStr | None = None
    channel_id: str | None = None
    stream_id: str | None = None
    broadcast_id: str | None = None
    auto_start: bool = True
    auto_stop: bool = True


class FacebookService(StreamingService, frozen=True):
    service: Literal["facebook"]
    access_token: SecretStr | None = None
    destination_id: str | None = None
    live_video_id: str | None = None


class KickService(StreamingService, frozen=True):
    service: Literal["kick"]
    channel: str | None = None


class VimeoService(StreamingService, frozen=True):
    service: Literal["vimeo"]
    access_token: SecretStr | None = None
    event_id: str | None = None


class LinkedInService(StreamingService, frozen=True):
    service: Literal["linkedin"]
    event_id: str | None = None
    operator_go_live: bool = True


class IcecastService(StreamingService, frozen=True):
    service: Literal["icecast"]
    admin_url: str | None = None
    admin_username: str | None = None
    admin_password: SecretStr | None = None


class CustomService(StreamingService, frozen=True):
    service: Literal["custom"]


class Streamo(Reccy, frozen=True):
    # Existing capture and composition fields remain here.
    streaming_service: Annotated[
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
```

This is a schema sketch, not code to paste unchanged. During implementation,
put each public model in the module that owns it and use validators to enforce
protocol/media combinations.

`StreamingService` completely represents the common publish contract:
destination identity, ingest, encoding, and metadata. Subclasses are not
transport implementations. They hold the additional stable identifiers and
credentials required by each service's API.

The model must validate at least:

- audio-only output has `video=None`; video services require video encoding;
- RTMP-family output uses FLV and a non-empty key;
- Icecast uses an audio container/codec and a mountpoint beginning with `/`;
- HLS push uses MPEG-TS segments and a valid positive segment duration;
- backup key and backup URL appear together;
- video dimensions, rates, bitrates, and keyframe interval are positive;
- known service subclasses use an ingest protocol that service supports;
- API actions are unavailable when their credentials or remote identifiers are
  absent, without preventing plain encoder ingest.

Do not encode changing platform limits as unexplained defaults in service
subclasses. Keep named, documented encoding profiles in a service catalog and
validate an explicitly selected profile against it. A `custom` target accepts
an explicit profile and only applies protocol-level validation.

## Runtime interface

Configuration and behavior have different ownership. Add a service adapter
protocol rather than methods full of `if service == ...`:

```python
class StreamingServiceAdapter(Protocol):
    capabilities: list[ServiceCapability]

    def prepare(self, metadata: StreamMetadata) -> PreparedStream: ...
    def output(self, prepared: PreparedStream) -> FfmpegOutput: ...
    def publish(self, prepared: PreparedStream) -> None: ...
    def update_metadata(self, metadata: StreamMetadata) -> None: ...
    def health(self) -> RemoteStreamStatus | None: ...
    def perform(self, command: str, payload: Mapping[str, object]) -> object: ...
    def finish(self) -> None: ...
```

`capabilities` is a list drawn from `prepare`, `publish`, `finish`, `metadata`,
`health`, `chat`, `announce`, `clip`, `marker`, `cue_point`, `schedule`,
`archive`, and `backup_ingest`. It describes what the configured adapter can
actually do with its present credentials, not every feature the platform sells.

`PreparedStream` contains resolved ingest details and remote IDs created during
preparation. `FfmpegOutput` contains an argument list, never a shell string, and
marks indexes containing secrets so diagnostics can redact them. Service
adapters own URL construction; `streamer.py` owns media generation and process
execution. API adapters own HTTP requests and response parsing.

The generic adapter supports ingest, local status, and stop. It does not claim
remote lifecycle or interaction capabilities. Named adapters may reuse generic
RTMP, SRT, HLS, or Icecast output builders while adding API behavior.

## Configuration transition

Backward compatibility is not required. Replace these top-level fields:

- `twitch_key`, `twitch_url`;
- all `twitch_*` API fields;
- output `audio_bitrate` and `video_bitrate`.

Move them under `streaming_service`. Keep capture settings such as device,
channel, and sample rate at the Streamo level. Keep composition settings such as
the source video, resolution, frame rate, title card, and participant images at
the Streamo level. An output profile may downscale or reduce frame rate, but it
must not change the composition clock used by image scheduling.

The first migrated configuration should be a `TwitchService`, preserving the
current output and Helix behavior without a compatibility parser for the old
JSON shape. Add complete example configurations for Twitch video, generic
RTMPS video, and Icecast audio.

## Implementation steps

1. Add the ingest, encoding, metadata, and service models with focused
   cross-field validation and redaction tests.
2. Add a service-adapter registry keyed by the discriminated `service` value.
   Implement generic RTMP/RTMPS and Icecast adapters first.
3. Move the current Twitch API client behind `TwitchServiceAdapter`; preserve
   its metadata, chat, announcement, clip, and marker commands.
4. Change `streamer.py` to ask the selected adapter for output arguments. Split
   audio-only and audio-video filter mapping so Icecast receives no video
   stream while Twitch and other video targets retain the complete composition.
5. Add named adapters in this order: YouTube, Vimeo, Facebook, LinkedIn, and
   Kick. An adapter may initially expose generic ingest only when the platform
   has no stable public API for a feature; its capability list must say so.
6. Route generic RPC commands through adapter capabilities. Reject unsupported
   operations with a clear service name and capability, rather than retaining
   Twitch-specific command dispatch.
7. Extend status with the selected service, local publish state, redacted
   endpoint host, advertised capabilities, and remote health when available.
8. Keep `preview` service-independent. It must bypass adapter preparation and
   remote APIs, while using the selected encoding profile where practical.
9. Replace the README configuration and RPC sections and document how to test a
   custom endpoint without exposing its key.

## Verification

- Parse one valid configuration for every service subclass and every ingest
  protocol.
- Reject invalid protocol/service, media/container, key/URL, mountpoint, and
  backup-endpoint combinations.
- Assert serialized models and validation errors never reveal `SecretStr`
  values.
- Assert exact FFmpeg argument lists for RTMP, RTMPS, SRT, HLS push, and
  Icecast, including correct audio-only mapping.
- Assert process diagnostics redact stream keys, passwords, passphrases, query
  secrets, and OAuth tokens.
- Exercise every adapter's advertised capabilities and verify unsupported
  commands fail before network access.
- Retain the existing Twitch API request tests behind the Twitch adapter.
- Use fake transports for remote API lifecycle and health tests; do not call
  live services from the unit suite.
- Run the full pytest, Ruff, formatting, type-checking, pyupgrade, and
  diff-check workflow after Python changes.
- On the target Mac, run the existing local preview for audio/video and add an
  Icecast test server as the audio-only acceptance target. A real account test
  for each named provider remains a manual credentialed acceptance test.

## Completion criteria

- No Twitch output or API field remains at the top level of `Streamo`.
- `streamer.py` contains no branded service URL or service-specific API logic.
- Generic supported-protocol destinations require configuration only, not a
  code change.
- Named adapters advertise only implemented and currently configured features.
- Audio-only publication does not encode or send video.
- Service and process status never expose secrets.
- Twitch retains its present ingest and control behavior through the new model.

## Additional work beyond the prompt

None.
