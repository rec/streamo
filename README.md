# Streamo

Streamo captures one stereo pair from an audio input device and sends it to an
FFmpeg-supported live ingest destination. It supports video streams built from a
looping visual bed and audio-only Icecast streams.

## Requirements

- Python 3.13 or later
- FFmpeg and FFprobe
- FFplay for local preview
- An audio input device that exposes the configured stereo pair

Install the Python environment with:

```bash
uv sync
```

## Configuration

Streamo reads TOML from `~/.config/streamo/config.toml` by default. This is a
complete Twitch configuration:

```toml
device_name = "X18"
channel = 17
video = "visual-bed.mp4"
title_card = "title.png"

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

[streaming_service.metadata]
title = "Live at the club"
category = "Music"
tags = ["live"]
language = "en"
```

`device_name`, `channel`, and `streaming_service` are required. `channel` is
one-based and selects the first channel of the stereo pair, so `17` captures
channels 17 and 18.

The top-level capture and composition defaults are:

| Field | Default | Meaning |
| --- | --- | --- |
| `sample_rate` | `48000` | Audio capture sample rate |
| `video` | none | Looping visual-bed file; required when video encoding is configured |
| `video_resolution` | `"640x360"` | Working size for title and participant overlays |
| `video_frame_rate` | `10` | Working frame rate for overlays |
| `title_card` | none | Optional title image |
| `title_interval` | `180.0` | Seconds between title-card appearances |
| `title_duration` | `8.0` | Seconds the title card is visible |
| `title_fade` | `2.0` | Fade-in and fade-out duration in seconds |
| `image_dir` | `"images"` | Participant-image directory |
| `image_interval` | `0.0` | Seconds between participant images; zero disables them |
| `image_duration` | `8.0` | Seconds each participant image is visible |
| `image_fade` | `2.0` | Fade-in and fade-out duration in seconds |

The encoding profile is always explicit. Audio codecs are `aac`, `mp3`, `opus`,
and `vorbis`; video codecs are `h264`, `hevc`, and `av1`.

## Destinations

Named services validate these ingest protocols:

| Service | Protocols | Video required |
| --- | --- | --- |
| `twitch` | RTMP, RTMPS | yes |
| `youtube` | RTMP, RTMPS, HLS push | yes |
| `facebook` | RTMP, RTMPS | yes |
| `kick` | RTMPS | yes |
| `vimeo` | RTMP, RTMPS, SRT | yes |
| `linkedin` | RTMP, RTMPS | yes |
| `icecast` | Icecast | no |
| `custom` | RTMP, RTMPS, SRT, HLS push, or Icecast | no |

RTMP and RTMPS require the FLV container. SRT and HLS require MPEG-TS. An HLS
`upload_url` must contain `{stream_key}`; Streamo substitutes the configured
secret before starting FFmpeg. YouTube additionally restricts HLS segment
duration to one through four seconds.

Icecast is audio-only and supports these codec/container pairs: AAC/ADTS,
MP3/MP3, Opus/Ogg, and Vorbis/Ogg. For example:

```toml
device_name = "X18"
channel = 17

[streaming_service]
service = "icecast"

[streaming_service.ingest]
protocol = "icecast"
server_url = "icecast://radio.example.com:8000"
mountpoint = "/live"
username = "source"
password = "replace-with-password"
tls = false

[streaming_service.encoding]
container = "mp3"

[streaming_service.encoding.audio]
codec = "mp3"
bitrate = "192k"
sample_rate = 48000
channels = 2
```

Twitch, YouTube, and Kick can perform provider API operations. The other
adapters send the configured FFmpeg output directly to ingest and reject
provider-specific control commands.

## YouTube authorization

Enable the YouTube Data API in a Google Cloud project, create an OAuth client of
type Desktop app, and download its client-secrets JSON file. Authorize Streamo
on a machine with a browser using:

```bash
uv run streamo auth youtube --client-secrets client-secret.json
```

Streamo requests offline access with the `youtube.force-ssl` scope and stores
the reusable credentials in `~/.config/streamo/youtube-auth.toml` with mode
`0600`. Access tokens are refreshed in memory before use. Do not commit either
the downloaded client-secrets file or the stored credentials.

For a target machine without a browser, open an SSH connection from the machine
with the browser and forward the callback port:

```bash
ssh -L 8765:127.0.0.1:8765 target
```

Run authorization through that connection:

```bash
uv run streamo auth youtube \
  --client-secrets client-secret.json \
  --no-browser \
  --callback-port 8765
```

Open the printed Google authorization URL in the local browser. Google redirects
the result through the SSH tunnel to Streamo's loopback listener on the target.

The first YouTube integration uses an existing live stream and broadcast. Put
their IDs in the configuration; Streamo does not create either resource:

```toml
device_name = "X18"
channel = 17
video = "visual-bed.mp4"

[streaming_service]
service = "youtube"
credentials = "~/.config/streamo/youtube-auth.toml"
stream_id = "replace-with-live-stream-id"
broadcast_id = "replace-with-live-broadcast-id"
auto_start = true
auto_stop = true

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

[streaming_service.metadata]
title = "Live at the club"
description = "Tonight's stream"
category = "10"
privacy = "unlisted"
scheduled_start = 2026-09-07T20:00:00Z
```

At preparation time Streamo updates the broadcast metadata and automatic
start/stop settings, binds the broadcast to the stream, and retrieves the RTMP
or HLS ingest address from YouTube. `category` is a YouTube video category ID,
and `scheduled_start` must include a timezone. A Google OAuth consent screen in
Testing status can issue refresh tokens that expire after seven days; use a
Production consent screen for a persistent installation.

## Kick authorization

Create an application at the Kick developer site and register
`http://127.0.0.1:8765/` as its redirect URL. Put the application credentials in
a private TOML file:

```toml
client_id = "replace-with-client-id"
client_secret = "replace-with-client-secret"
```

Authorize Streamo using:

```bash
uv run streamo auth kick --client-secrets kick-client.toml
```

Streamo uses OAuth 2.1 with PKCE and requests `channel:read`, `channel:write`,
`chat:write`, and `streamkey:read`. It stores the reusable credentials in
`~/.config/streamo/kick-auth.toml` with mode `0600`. Kick rotates refresh tokens;
Streamo writes each replacement to that file atomically. The access token and
stream key are not written to configuration files.

The same SSH tunnel works when the target has no browser:

```bash
ssh -L 8765:127.0.0.1:8765 target
uv run streamo auth kick \
  --client-secrets kick-client.toml \
  --no-browser \
  --callback-port 8765
```

Open the printed URL in the local browser. The registered redirect URL must use
the same callback port. Once authorization succeeds, configure the channel by
its Kick URL slug; no ingest section is needed:

```toml
device_name = "X18"
channel = 17
video = "visual-bed.mp4"

[streaming_service]
service = "kick"
credentials = "~/.config/streamo/kick-auth.toml"
channel = "replace-with-channel-slug"

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

[streaming_service.metadata]
title = "Live at the club"
category = "15"
tags = ["music", "live"]
```

At preparation time Streamo updates the channel metadata and retrieves its
RTMPS URL and stream key. `category` is Kick's numeric category ID.

## Running Streamo

Run with the default configuration path:

```bash
uv run streamo
```

Use a different configuration file with:

```bash
uv run streamo --config config.toml
```

Preview the same audio, video, title-card, and participant-image composition
without connecting to the destination:

```bash
uv run streamo daemon preview --config ~/.config/streamo/config.toml
```

Preview sends a NUT stream from FFmpeg to FFplay. It does not prepare, publish,
or finish a remote stream. Close the preview window or send the `stop` control
command to stop it.

Manage the background service with:

```bash
uv run streamo daemon install --config ~/.config/streamo/config.toml
uv run streamo daemon start
uv run streamo daemon status
uv run streamo daemon stop
uv run streamo daemon restart
uv run streamo daemon uninstall
```

## Live overlays

When `title_card` is configured for a video stream, Streamo overlays it at
startup and every `title_interval` seconds without modifying the visual-bed
file.

Set `image_interval` to a positive value to enable participant images. Streamo
accepts GIF, JPEG, PNG, and WebP files from `image_dir`. It rescans before each
interval, shows every current image once in shuffled order, and gives newly
discovered images priority in the current cycle. Invalid images are logged and
skipped. Deleted paths leave the cycle; recreating a path makes it new again.

The `image` control command can copy a `file:` URL or download an HTTP(S) URL
into `image_dir`. Files are published atomically so the frame producer does not
read a partial image.

## Show control

Streamo uses Reccy's JSON Lines RPC transport. Configuration is TOML, but each
control message remains one JSON object followed by a newline. Begin a
connection with:

```json
{"type": "hello", "role": "show-control", "version": 1}
```

After the server's `hello`, send one request. The server returns a raw JSON
result or an error object.

```json
{"type": "request", "command": "status", "params": {}}
{"type": "request", "command": "mute", "params": {}}
{"type": "request", "command": "unmute", "params": {}}
{"type": "request", "command": "stop", "params": {}}
{"type": "request", "command": "ping", "params": {}}
{"type": "request", "command": "image", "params": {"urls": ["file:///tmp/guest.png"]}}
```

Twitch configurations with `client_id`, `access_token`, and `broadcaster_id`
also support `update_stream_info`, `chat`, `announce`, `clip`, and `marker`.
`sender_id` and `moderator_id` default to the broadcaster ID. The token needs
the Twitch scopes required by the operations being used:

- `channel:manage:broadcast` for stream information and markers
- `user:write:chat` for chat messages
- `moderator:manage:announcements` for announcements
- `clips:edit` for clips

Kick configurations with OAuth credentials support `update_stream_info` and
`chat`. The update accepts `title`, `category` or `category_id`, and `tags`;
categories are numeric Kick IDs. Chat accepts a `message` string.

The `status` result includes audio levels and timing, FFmpeg state and bitrate,
the service name, the ingest hostname without secrets, available capabilities,
and `remote_health`. YouTube reports its stream and ingest health there, while
Kick reports live state and viewer count. Adapters without a health API report
`null`. Stream keys, passwords,
passphrases, access tokens, and complete publish URLs are excluded from status
and failure diagnostics.

## Preparing visual media

The scripts use FFmpeg, FFprobe, and, where noted, FFplay.

Preview a video's loop point interactively:

```bash
uv run python scripts/loop_tester.py videos/*.mp4
```

Enter `r` to replay, `l` to create and accept a forward/backward loop, `m` to
mark the original as already looping, or return to leave the file in place.
Accepted loops go to `loops/`; source files used to create a loop go to
`originals/`. Files whose names contain `looped` are moved to `loops/`
unchanged.

Automatically convert files whose first and near-final frames differ by at
least the configured threshold:

```bash
uv run python scripts/auto_tester.py videos/*.mp4
```

Files that might already loop remain in place. Converted loops go to `loops/`
and their source files go to `originals/`.

Build a visual bed from looped videos and still images:

```bash
uv run python scripts/render.py \
  --inputs a-looped.mp4 b-looped.mp4 still.png \
  --output visual-bed.mp4 \
  --duration 3600 \
  --seed 1234 \
  --title-card title.md
```

The renderer starts from black, selects media in randomized cycles, crossfades
between scenes, and can overlay a PNG, other supported still image, or rendered
Markdown title card.

## Development

Run the test suite with:

```bash
uv run pytest
```
