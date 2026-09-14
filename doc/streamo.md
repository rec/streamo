# Streamo operator guide

Streamo captures one stereo pair from an audio device and publishes it through
FFmpeg to a live-streaming destination. A video stream combines the audio with
a looping visual bed and optional title and participant-image overlays. Icecast
streams are audio-only.

## Start here

Streamo needs Python 3.13 or later, FFmpeg, FFprobe, an audio input device, and
FFplay for preview. Install the project environment with:

```bash
uv sync
```

Create `~/.config/streamo/config.toml`. This Twitch configuration is a useful
starting point:

```toml
device_name = "X18"
channel = 17
video = "visual-bed.mp4"
sample_rate = 48000
video_resolution = "1280x720"
video_frame_rate = 30

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
title = "Live show"
category = "Music"
tags = ["live"]
```

`device_name`, `channel`, and `streaming_service` are required. `channel` is
one-based and selects the first channel in the stereo pair: `17` selects
channels 17 and 18. `video` is required whenever the encoding profile contains
a video section.

Run Streamo with the default configuration:

```bash
uv run streamo
```

Use another configuration file when needed:

```bash
uv run streamo --config config.toml
```

Before a show, preview the local composition without contacting the destination:

```bash
uv run streamo daemon preview --config ~/.config/streamo/config.toml
```

Preview uses FFplay and does not prepare, publish, or finish a remote stream.
Close its window or send the `stop` control command to end it.

## Configuration

The top-level settings control capture and the working format for overlays.
The nested encoding settings control the published stream. Set both sets of
audio and video values when they should agree.

| Field | Default | Purpose |
| --- | --- | --- |
| `sample_rate` | `48000` | Audio capture sample rate |
| `video` | none | Looping visual-bed file |
| `video_resolution` | `"640x360"` | Overlay working resolution |
| `video_frame_rate` | `10` | Overlay working frame rate |
| `title_card` | none | Title-card image |
| `title_interval` | `180.0` | Seconds between title-card appearances |
| `title_duration` | `8.0` | Seconds the title card remains visible |
| `title_fade` | `2.0` | Fade-in and fade-out duration, in seconds |
| `local_display` | `true` | Show the composed program on local HDMI |
| `image_dir` | `"images"` | Participant-image directory |
| `image_interval` | `0.0` | Seconds between participant images; zero disables them |
| `image_duration` | `8.0` | Seconds each participant image is visible |
| `image_fade` | `2.0` | Participant-image fade duration, in seconds |
| `current_session_image_weight` | `3` | Current-session images per older image |

An encoding profile always states its container, audio encoding, and, for video
streams, video encoding. Audio codecs are `aac`, `mp3`, `opus`, and `vorbis`.
Video codecs are `h264`, `hevc`, and `av1`. Valid audio/container pairs are
AAC/ADTS, AAC or MP3/FLV, MP3/MP3, AAC, MP3, or Opus/MPEG-TS, and Opus or
Vorbis/Ogg. `pixel_format` defaults to `"yuv420p"`.

Keep stream keys, passwords, API tokens, and OAuth credential files private.
Do not commit them.

Ready-to-copy configurations are available for [Twitch](../examples/twitch.toml),
[a generic RTMPS service](../examples/generic-rtmps.toml), and
[Icecast](../examples/icecast.toml).

## Destinations

| Service | Allowed protocols | Video |
| --- | --- | --- |
| Twitch | RTMP, RTMPS | required |
| YouTube | RTMP, RTMPS, HLS push | required |
| Facebook | RTMP, RTMPS | required |
| Kick | RTMPS | required |
| Vimeo | RTMP, RTMPS, SRT | required |
| LinkedIn | RTMP, RTMPS | required |
| Icecast | Icecast | not used |
| Custom | RTMP, RTMPS, SRT, HLS push, Icecast | optional |

RTMP and RTMPS use the FLV container. Keep the stream key in `stream_key` and
out of `server_url`. SRT and HLS push use MPEG-TS. An HLS `upload_url` must
contain `{stream_key}`, which Streamo substitutes while starting FFmpeg.
YouTube HLS requires a one-to-four-second segment duration, AAC audio, and
H.264 or HEVC video.

Icecast is audio-only. Its ingest settings use an `icecast://` server URL, a
mountpoint beginning with `/`, source username and password, and optional TLS.
Use one of AAC/ADTS, MP3/MP3, Opus/Ogg, or Vorbis/Ogg.

For Twitch, provider API operations need `client_id`, `access_token`, and
`broadcaster_id`. `sender_id` and `moderator_id` default to the broadcaster ID.
Facebook, Vimeo, and LinkedIn accept their provider-specific identifiers and
credentials in `streaming_service`; publishing still uses the configured ingest.

YouTube can use direct ingest, or OAuth credentials plus an existing
`stream_id` and `broadcast_id`. With OAuth, Streamo updates metadata, binds the
broadcast to the stream, and obtains the ingest address. `auto_start` and
`auto_stop` default to `true`. Streamo does not create the stream or broadcast.

Kick can use direct RTMPS ingest, or OAuth credentials and a channel slug. With
OAuth, Streamo updates the channel and retrieves its RTMPS URL and stream key.

## Service operation

Install Streamo as a background service with the configuration it should run:

```bash
uv run streamo daemon install --config ~/.config/streamo/config.toml
uv run streamo daemon start
uv run streamo daemon status
uv run streamo daemon stop
uv run streamo daemon restart
uv run streamo daemon uninstall
```

Video streams can show the already-composed program on a local HDMI display.
This is enabled by default through `local_display = true`; it does not change
the remote stream. It is ignored for audio-only streams. On a Pi, the service
account normally needs access to `/dev/dri`, usually through the `video` group.
Confirm the actual FFplay build and HDMI hot-plug behaviour on the target.

For each show, confirm the selected stereo pair, preview the visual bed and
overlays, then verify the encoded resolution and frame rate at the destination.
After starting, check audio levels, output bitrate, and provider health through
the `status` control command.

## Show control

Streamo uses Reccy's JSON Lines RPC transport. Its configuration is TOML, but
each control message is one JSON object followed by a newline. Begin with:

```json
{"type": "hello", "role": "show-control", "version": 1}
```

Then send requests in this form:

```json
{"type": "request", "command": "status", "params": {}}
{"type": "request", "command": "mute", "params": {}}
{"type": "request", "command": "unmute", "params": {}}
{"type": "request", "command": "stop", "params": {}}
{"type": "request", "command": "ping", "params": {}}
{"type": "request", "command": "image", "params": {"urls": ["file:///tmp/guest.png"]}}
{"type": "request", "command": "remove_last_image", "params": {}}
```

The server returns a raw JSON result or an error object. `image` accepts one or
more `file:`, HTTP, or HTTPS URLs and publishes valid images into `image_dir`
atomically. Supported participant-image formats are GIF, JPEG, PNG, and WebP.

`status` reports audio levels and timing, FFmpeg state and bitrate, mute state,
the service, a secret-free ingest hostname, adapter capabilities, and
`remote_health`. `remote_health` may be `null`. Status and failure diagnostics
do not include stream keys, passwords, passphrases, access tokens, or complete
publish URLs.

Twitch supports `update_stream_info`, `chat`, `announce`, `clip`, and `marker`
when the relevant credentials and token scopes are present. Required scopes are
`channel:manage:broadcast` for stream information and markers,
`user:write:chat` for chat, `moderator:manage:announcements` for announcements,
and `clips:edit` for clips. Kick supports `update_stream_info` and `chat` with
OAuth credentials. Its categories are numeric IDs.
YouTube supports `update_stream_info` with OAuth credentials.

## Overlays and participant images

Set `title_card` for a video stream to show a title image at startup and every
`title_interval` seconds. A title card requires a positive interval and
duration; its duration must be shorter than its interval, and both fades must
fit within its duration.

Set `image_interval` to a positive value to enable participant images. Streamo
rescans `image_dir` before every interval. Images added after startup are shown
once before pre-existing images repeat. Afterwards,
`current_session_image_weight = 3` means three current-session images for each
older image. Set it to `0` to show older images whenever no new image remains.
Invalid files are logged and skipped; deleted paths leave the cycle.

An optional feed downloads uploaded images into the same directory:

```toml
image_dir = "images"
image_interval = 20.0
image_duration = 8.0
image_fade = 2.0

[image_feed]
url = "https://example.com/foto.php"
token = "replace-with-the-room-token"
poll_interval = 2.0
```

The feed URL and token grant upload access. Use a new token and an empty remote
data directory for each show.

## YouTube and Kick authorization

For YouTube, enable the YouTube Data API, create a Desktop OAuth client, and
download its client-secrets JSON file. Authorize on a machine with a browser:

```bash
uv run streamo auth youtube --client-secrets client-secret.json
```

Streamo stores reusable credentials in `~/.config/streamo/youtube-auth.toml`
with restrictive permissions. It requests the `youtube.force-ssl` scope. A
Google consent screen in Testing can issue refresh tokens that expire after
seven days; use Production for a persistent installation.

For Kick, create an application, register `http://127.0.0.1:8765/` as its
redirect URL, and create a private TOML file containing `client_id` and
`client_secret`. Then run:

```bash
uv run streamo auth kick --client-secrets kick-client.toml
```

Streamo stores reusable credentials in `~/.config/streamo/kick-auth.toml` and
rotates Kick refresh tokens. It requests `channel:read`, `channel:write`,
`chat:write`, and `streamkey:read`.

For either provider on a machine without a browser, tunnel the callback port
from a machine with one, then use `--no-browser` and `--callback-port 8765`:

```bash
ssh -L 8765:127.0.0.1:8765 target
```

Open the authorization URL printed by Streamo in the local browser. The
provider's redirect URL and the configured callback port must match.

## Preparing visual media

The project includes utilities for visual beds:

```bash
uv run python scripts/loop_tester.py videos/*.mp4
uv run python scripts/auto_tester.py videos/*.mp4
uv run python scripts/render.py \
  --inputs a-looped.mp4 b-looped.mp4 still.png \
  --output visual-bed.mp4 \
  --duration 3600 \
  --seed 1234 \
  --title-card title.md
```

`loop_tester.py` lets an operator check and create loops. `auto_tester.py`
converts sources whose first and near-final frames differ by the selected
threshold. The renderer combines looped videos and still images in randomized
cycles with crossfades. Add `--plan-only` to write a reusable TOML render plan,
then run it as the sole input:

```bash
uv run python scripts/render.py --inputs visual-bed.toml
```

## Development

Run the test suite with:

```bash
uv run pytest
```
