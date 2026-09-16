# streamO operator guide

streamO captures one stereo pair from an audio device and publishes it through
FFmpeg to a live-streaming destination. A video stream combines the audio with
a looping visual bed and optional title and participant-image overlays. Icecast
streams are audio-only.

Use the local preview before publishing. During a show, control clients can mute,
stop, submit images, and read audio, FFmpeg, and provider health. Audio capture
retries after device failures and drops old audio when FFmpeg falls behind,
reporting those problems through status. No backup ingest or failover is provided.

## Start here

streamO needs Python 3.13 or later, FFmpeg, FFprobe, an audio input device, and
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
a video section. Relative media, image-directory, and provider-credential paths
resolve from the configuration file’s directory. `~` expands to the user’s home
directory, including for credential files.

Run streamO with the default configuration:

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

Preview uses FFplay without loading provider credentials or calling provider APIs.
The configured participant-image feed still polls when enabled.
Close its window or send the `stop` control command to end it.

## Configuration

The top-level settings control capture and the working format for overlays.
The nested encoding settings control the published stream. Set both sets of
audio values when they should agree. Overlay dimensions and frame rate affect
the overlays only; the base video retains the published resolution and frame rate.
Local media must be readable before any provider preparation begins. Timings must
be finite, and working dimensions must be positive.

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

MP3, ADTS, and Ogg are audio-only with the supported codecs.

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
out of `server_url`. Backup URL/key settings are rejected. SRT and HLS push use MPEG-TS. An HLS `upload_url` must
contain `{stream_key}`, which streamO substitutes while starting FFmpeg.
YouTube HLS requires a one-to-four-second segment duration, AAC audio, and
H.264 or HEVC video. SRT uses `url`, optional `passphrase`, and `latency_ms`
(default 120). HLS uses `upload_url`, `stream_key`, and `segment_duration`.

Icecast is audio-only. Its ingest settings use an `icecast://` server URL, a
mountpoint beginning with `/`, source username and password, and optional TLS.
Use one of AAC/ADTS, MP3/MP3, Opus/Ogg, or Vorbis/Ogg.

For Twitch, provider API operations need `client_id`, `access_token`, and
`broadcaster_id`. `sender_id` and `moderator_id` default to the broadcaster ID.
Facebook, Vimeo, and LinkedIn accept their provider-specific identifiers and
credentials in `streaming_service`; publishing still uses the configured ingest.

YouTube can use direct ingest, or OAuth credentials plus an existing
`stream_id` and `broadcast_id`. With OAuth, streamO updates metadata, binds the
broadcast to the stream, and obtains the ingest address. `auto_start` and
`auto_stop` default to `true`. streamO does not create the stream or broadcast.

Kick can use direct RTMPS ingest, or OAuth credentials and a channel slug. With
OAuth, streamO updates the channel and retrieves its RTMPS URL and stream key.
Its nested `channel` is the provider slug; the top-level `channel` remains the
one-based audio input number. Metadata `category` is a name for Twitch and a
numeric ID for YouTube or Kick. Other adapters publish ingest only and reject
provider-specific control commands.

## Service operation

Install streamO as a background service with the configuration it should run:

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
The player uses SDL KMSDRM without a desktop session and receives the already
encoded program over loopback-only MPEG-TS. A failed player retries after five
seconds while HDMI remains connected; its failure is logged. Inspect the service
with `systemctl cat streamo` and device permissions with `ls -l /dev/dri`. Confirm
the actual FFplay build and HDMI hot-plug behaviour on the target.

For each show, confirm the selected stereo pair, preview the visual bed and
overlays, then verify the encoded resolution and frame rate at the destination.
After starting, check audio levels, output bitrate, and provider health through
the `status` control command.

## Show control

streamO uses reccy's JSON Lines RPC transport. Its configuration is TOML, but
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
with atomic publication of each file after the entire batch validates. A failed
validation publishes nothing; a publication error rolls back files from that batch.
Each image is limited to 8 MiB and 2048 pixels per side. Supported formats are
GIF, JPEG, PNG, and WebP.

`status` reports audio levels and timing, FFmpeg state and bitrate, mute state,
the service, a secret-free ingest hostname, adapter capabilities, and
`remote_health`. Status reads local cached state and never waits for a provider
request. Health refreshes every 30 seconds, with `remote_health_error` and
`remote_health_updated_at` (Unix seconds) distinguishing failures and stale data.
Adapters without a health API report `null`.

`audio_error` reports a current problem; `audio_last_error`, `audio_error_count`,
and `audio_dropped_frames` retain the history after recovery. Capture retries
unavailable devices every five seconds and inactive capture after one second.
Controllers should monitor these fields as well as `last_audio_at`, rather than
treating a running FFmpeg process as proof of healthy audio. An FFmpeg process
exit is reported as failure; the capture retry policy does not restart FFmpeg.

Capabilities describe available provider commands and health queries. Lifecycle
steps and the always-available local `stop` command are not provider capabilities.
Status and failure diagnostics
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

Set `image_interval` to a positive value to enable participant images. streamO
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

### Deploying the upload page

The upload application in `web/foto.php` lets participants submit
photos without joining the showCo network. Their browser prepares each photo as
a JPEG no larger than 2048 pixels on either side, then sends it to `ax.to`.
streamO polls the server over outbound HTTPS and stores each new JPEG in
`image_dir`.

The page uses French when the browser's primary language begins with `fr` and
English for every other browser. Current Safari can prepare HEIC photos without
a server-side converter. There is deliberately no WebAssembly fallback. If a
browser cannot decode a selected HEIC photo, the page explains that limitation
and asks the participant to contact Tom.

On the Virtualmin server, copy the PHP file into the virtual server's document
root, create a private data directory writable by its PHP-FPM user, and expose
these two environment variables to that PHP-FPM pool:

```text
STREAMO_IMAGE_TOKEN=<a new random secret of at least 20 characters>
STREAMO_IMAGE_DATA_DIR=/home/example/streamo-image-data
```

The data directory must remain outside the document root. The PHP installation
needs the normally enabled `fileinfo` extension and must allow uploads of at
least 8 MiB. The application accepts only JPEG files whose dimensions and size
fit the same limits enforced by the browser.

If the PHP file is available at `https://ax.to/show/foto.php`, put this URL in
streamO's TOML configuration using the same token:

```toml
image_dir = "images"
current_session_image_weight = 3
image_interval = 20.0
image_duration = 8.0
image_fade = 2.0

[image_feed]
url = "https://ax.to/show/foto.php"
token = "replace-with-the-room-token"
poll_interval = 2.0
```

The participant URL, suitable for a room-only QR code, is:

```text
https://ax.to/show/foto.php?token=replace-with-the-room-token
```

Treat the URL as a capability: anyone who receives it can submit images. streamO
keeps a per-feed cursor inside `image_dir`, so restarting streamO or removing a
local image does not download it again. After the show, remove the uploaded JPEGs
and `images.jsonl` from `STREAMO_IMAGE_DATA_DIR`. Use an empty data directory and
a new token for the next show.

The server returns at most 1000 entries per poll. streamO logs and skips entries
whose images are missing (HTTP 404/410), too large, or invalid. Transient download
failures retain the cursor for retry. The cursor persists across restarts.

## YouTube and Kick authorization

For YouTube, enable the YouTube Data API, create a Desktop OAuth client, and
download its client-secrets JSON file. Authorize on a machine with a browser:

```bash
uv run streamo auth youtube --client-secrets client-secret.json
```

streamO stores reusable credentials in `~/.config/streamo/youtube-auth.toml`
with restrictive permissions. It requests the `youtube.force-ssl` scope. A
Google consent screen in Testing can issue refresh tokens that expire after
seven days; use Production for a persistent installation.

For Kick, create an application, register `http://127.0.0.1:8765/` as its
redirect URL, and create a private TOML file containing `client_id` and
`client_secret`. Then run:

```bash
uv run streamo auth kick --client-secrets kick-client.toml
```

streamO stores reusable credentials in `~/.config/streamo/kick-auth.toml` and
rotates Kick refresh tokens. It requests `channel:read`, `channel:write`,
`chat:write`, and `streamkey:read`.

For either provider on a machine without a browser, tunnel the callback port
from a machine with one, then use `--no-browser` and `--callback-port 8765`:

```bash
ssh -L 8765:127.0.0.1:8765 target
```

Open the authorization URL printed by streamO in the local browser. The
provider's redirect URL and the configured callback port must match.

For OAuth, keep the encoding sections from the initial example and replace the
provider section with one of these:

```toml
[streaming_service]
service = "youtube"
credentials = "~/.config/streamo/youtube-auth.toml"
stream_id = "replace-with-live-stream-id"
broadcast_id = "replace-with-live-broadcast-id"
auto_start = true
auto_stop = true

[streaming_service.metadata]
title = "Live show"
category = "10"
privacy = "unlisted"
```

```toml
[streaming_service]
service = "kick"
credentials = "~/.config/streamo/kick-auth.toml"
channel = "replace-with-channel-slug"

[streaming_service.metadata]
title = "Live show"
category = "15"
tags = ["music", "live"]
```

An optional `scheduled_start` is a TOML datetime with a timezone. YouTube also
accepts `description`, `tags`, and `privacy` (`public`, `unlisted`, or `private`).
Kick chat accepts a `message` string; updates accept `title`, `category` or
`category_id`, and `tags`. Provider operations require their corresponding scopes.

## Preparing visual media

The scripts use FFmpeg, FFprobe, and, where noted, FFplay.

Preview a video's loop point interactively:

```bash
uv run python -m scripts.preview_loops videos/*.mp4
```

Enter `r` to replay, `l` to create and accept a forward/backward loop, `m` to
mark the original as already looping, or return to leave the file in place.
Accepted loops go to `loops/`; source files used to create a loop go to
`originals/`. Files whose names contain `looped` are moved to `loops/`
unchanged.

Automatically convert files whose first and near-final frames differ by at
least the configured threshold:

```bash
uv run python -m scripts.auto_loop videos/*.mp4
```

Files that might already loop remain in place. Converted loops go to `loops/`
and their source files go to `originals/`.

Build a visual bed from looped videos and still images:

```bash
uv run python -m scripts.render \
  --inputs a-looped.mp4 b-looped.mp4 still.png \
  --output visual-bed.mp4 \
  --duration 3600 \
  --seed 1234 \
  --title-card title.md
```

The renderer starts from black, selects randomly from all media except the immediate predecessor, crossfades
between scenes, and can overlay a PNG, other supported still image, or rendered
Markdown title card.

Add `--plan` (or `-p`) to print the generated render plan as TOML before
rendering. Add `--plan-only` (or `-P`) to print it without rendering:

```bash
uv run python -m scripts.render \
  --inputs a-looped.mp4 b-looped.mp4 \
  --output visual-bed.mp4 \
  --plan-only > visual-bed.toml
```

Run a saved plan without rebuilding its randomized scene selection:

```bash
uv run python -m scripts.render --inputs visual-bed.toml
```

A plan includes its render settings and is the sole input. `--plan` and
`--plan-only` are only for creating plans, so they cannot be used when running
one.

The automatic loop check is a heuristic: it compares the first frame with a
sample 0.25 seconds before the end, not the exact loop boundary. It does not
prove that a source loops seamlessly. Random scene selection prevents immediate
repeats when distinct inputs are available; it does not guarantee each source
appears once per cycle.

To create a forward/backward loop without moving the original, use
`uv run python -m scripts.loop_videos videos/*.mp4`. To prepare smaller 720p
H.264 sources, use `uv run python -m scripts.reencode_videos output/ videos/*.mp4`.
Its default bitrate is capped at 1200 kbps and 80% of the source bitrate;
`--max-bitrate-kbps` and `--source-ratio` adjust those limits.

Media outputs are published only after a successful render. Existing outputs,
archive collisions, and duplicate re-encoding stems are rejected. Choose a new
output path if the target already exists. New render plans contain absolute
paths; relative paths in hand-written plans resolve from the plan’s directory.
Saved plans reject empty/short timelines, invalid durations, missing transitions,
and excessive overlaps before rendering. Filter graphs are passed through a file
to avoid oversized command arguments. Long renders still open one input per
scene; decoder and memory capacity depend on the target machine.

## Development

The public help is recorded with reccy’s `cli_help` pytest fixture.
Behavior tests separately cover lifecycle, routing, recovery, and media handling.

Run the test suite with:

```bash
uv run pytest
```

Source responsibilities are separated into `provider_config.py` (configuration),
`providers.py` (adapters and ingest output), `composition.py` (live FFmpeg graph),
`streamer.py` (process lifecycle), and `audio.py` (capture). Offline media uses
`scripts/render_plan.py` for planning, `scripts/title_card.py` for Markdown cards,
and `scripts/render.py` for execution. See [the handover](handover.md)
for completed work and the limits of verification.
