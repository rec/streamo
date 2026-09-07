# Porting an application from Twitcho

Streamo does not provide a compatibility layer for Twitcho. Existing
applications must change their command name, Python imports, service identity,
configuration path, and configuration format together.

## 1. Stop and uninstall the old service

Do this while the old `twitcho` command is still installed:

```bash
twitcho daemon stop
twitcho daemon uninstall
```

After installing Streamo, install its replacement service:

```bash
streamo daemon install --config ~/.config/streamo/config.toml
streamo daemon start
```

The launchd label changes from `com.swirly.twitcho` to
`com.swirly.streamo`. The daemon environment variable changes from
`TWITCHO_DAEMON` to `STREAMO_DAEMON`.

## 2. Rename application references

Change command invocations from `twitcho` to `streamo`. Change Python imports
from `twitcho` to `streamo` and the configuration class from `Twitcho` to
`Streamo`.

```python
from streamo.config import Streamo
```

The default configuration path is now
`~/.config/streamo/config.toml`. The Reccy RPC role, state directory, and local
control endpoint also use `streamo`. An application that constructs those
paths directly must update them rather than continuing to look under
`twitcho`.

## 3. Convert JSON configuration to TOML

There is no JSON configuration parser. Move the existing capture and
composition fields to the top level of a TOML file, then place output, API,
and encoding settings under `streaming_service`.

| Twitcho JSON field | Streamo TOML field |
| --- | --- |
| `device_name` | `device_name` |
| `channel` | `channel` |
| `video` | `video` |
| `title_card` and title timing fields | unchanged top-level fields |
| participant image fields | unchanged top-level fields |
| `sample_rate` | `sample_rate` for capture and `streaming_service.encoding.audio.sample_rate` for output |
| `twitch_url` | `streaming_service.ingest.server_url` |
| `twitch_key` | `streaming_service.ingest.stream_key` |
| `audio_bitrate` | `streaming_service.encoding.audio.bitrate` |
| `video_bitrate` | `streaming_service.encoding.video.bitrate` |
| `twitch_client_id` | `streaming_service.client_id` |
| `twitch_access_token` | `streaming_service.access_token` |
| `twitch_broadcaster_id` | `streaming_service.broadcaster_id` |
| `twitch_sender_id` | `streaming_service.sender_id` |
| `twitch_moderator_id` | `streaming_service.moderator_id` |
| `twitch_api_url` | `streaming_service.api_url` |

A migrated Twitch configuration has this structure:

```toml
device_name = "X18"
channel = 17
video = "visual-bed.mp4"
title_card = "title.png"
sample_rate = 48000

[streaming_service]
service = "twitch"
client_id = "replace-with-client-id"
access_token = "replace-with-access-token"
broadcaster_id = "123456789"

[streaming_service.ingest]
protocol = "rtmps"
server_url = "rtmps://live.twitch.tv/app"
stream_key = "replace-with-stream-key"

[streaming_service.encoding]
container = "flv"

[streaming_service.encoding.audio]
codec = "aac"
bitrate = "160k"
sample_rate = 48000
channels = 2

[streaming_service.encoding.video]
codec = "h264"
bitrate = "2500k"
resolution = "1280x720"
frame_rate = 30
keyframe_interval = 2
```

See `examples/twitch.toml` for a complete Twitch configuration. Generic RTMPS
and Icecast examples are also available under `examples/`.

## 4. Keep JSON Lines control messages

Do not convert control messages or `.jsonl` files to TOML. The Reccy RPC and
control payloads remain JSON objects separated by newlines. Existing command
names and payloads for `status`, `mute`, `unmute`, `stop`, `ping`, `image`,
`update_stream_info`, `chat`, `announce`, `clip`, and `marker` remain valid.

Twitch API commands are available only when the nested Twitch credentials are
configured. Other adapters reject unsupported commands with the selected
service name and missing capability.

## 5. Update status handling

The status response retains the existing runtime fields and adds:

- `service`, containing the configured service discriminator;
- `endpoint_host`, containing only the non-secret ingest host;
- `capabilities`, listing operations available with the current credentials;
- `remote_health`, containing adapter health details when implemented.

Applications should tolerate `remote_health = null`. They must not expect a
stream key, password, passphrase, token, or complete publish URL in status or
process diagnostics.

## 6. Verify before publishing

Parse and preview the migrated configuration locally:

```bash
streamo daemon preview --config ~/.config/streamo/config.toml
```

Preview bypasses remote preparation and publishing. After checking the local
audio and video composition, start the daemon with the real destination
credentials and confirm remote ingest health through the provider's operator
interface.
