# streamo

`streamo` streams one stereo pair from an audio input device to an
FFmpeg-addressable live destination. Video streams use a low-resolution
pre-rendered animation as their visual source; Icecast streams are audio-only.

The first version is intentionally small and independent of `recs`.

## Configuration

Create a TOML config file. This Twitch example preserves the original Streamo
behavior:

```toml
device_name = "X18"
channel = 17
video = "visual-bed.mp4"
title_card = "title.png"

[streaming_service]
service = "twitch"
client_id = "..."
access_token = "..."
broadcaster_id = "123456789"

[streaming_service.ingest]
protocol = "rtmps"
server_url = "rtmps://live.twitch.tv/app"
stream_key = "live_..."

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

Then run:

```bash
streamo --config config.toml
```

`channel` is one-based and names the first channel of the stereo pair. For
example, `17` streams channels 17 and 18.

`streamo` requires `ffmpeg` to be installed.

If `title_card` is set, Streamo overlays that image on the outgoing stream
without changing the prepared visual-bed video. The title card appears at stream
start and then repeats every `title_interval` seconds. The defaults are an
8-second title every 180 seconds with 2-second fade in and out. These can be
changed with `title_interval`, `title_duration`, and `title_fade`.

Set `image_interval` to a positive number to show participant images from
`image_dir`. Each image fades in and out using `image_duration` and `image_fade`.
Streamo shows every image once in shuffled order before repeating any image.
Images added while Streamo is running take priority at the next image interval.

## Previewing the live composition

Use the preview action on the target Mac to inspect the same audio, video,
title-card, and participant-image composition without connecting to Twitch:

```bash
uv run streamo daemon preview --config ~/.config/streamo/config.toml
```

Streamo sends the encoded output to `ffplay`, which opens a live preview window.
The configured audio device must be available, and `ffplay` must be installed
alongside FFmpeg. Preview does not use the configured destination or make any
service API request. While preview is running, copy supported image files into
`image_dir`; newly discovered images appear before images already waiting in
the current shuffled cycle. Close the preview window or send `stop` to end both
processes.

## Show-control connection

On macOS and Linux, `streamo` uses the Reccy control socket at
`~/.local/state/streamo/gui.sock`. Each message is one JSON object followed by
a newline.

Start with:

```json
{"type": "hello", "role": "show-control", "version": 1}
```

After the server returns its `hello`, send one request. The server returns the
raw JSON result or an error object and closes that request connection.

```json
{"type": "request", "command": "status", "params": {}}
{"type": "request", "command": "mute", "params": {}}
{"type": "request", "command": "unmute", "params": {}}
{"type": "request", "command": "stop", "params": {}}
{"type": "request", "command": "ping", "params": {}}
{"type": "request", "command": "update_stream_info", "params": {"title": "Live at the club", "category": "Music", "tags": ["live"]}}
{"type": "request", "command": "chat", "params": {"message": "Starting now"}}
{"type": "request", "command": "announce", "params": {"message": "Recording and streaming"}}
{"type": "request", "command": "clip", "params": {}}
{"type": "request", "command": "marker", "params": {"description": "First song"}}
```

The control transport remains JSON Lines; changing configuration to TOML does
not change its message format. Service status includes the selected service,
redacted endpoint host, configured capabilities, and remote health when an
adapter provides it.

The Twitch API commands require `client_id`, `access_token`, and
`broadcaster_id` in `[streaming_service]`. By default, Streamo uses the
broadcaster ID as the chat sender and announcement moderator. Set `sender_id`
or `moderator_id` if those should be different. Other named adapters currently
provide encoder ingest and reject unsupported control operations before making
a network request.

The token needs Twitch scopes for the side effects you use:

- `channel:manage:broadcast` for stream info updates and stream markers.
- `user:write:chat` for chat messages.
- `moderator:manage:announcements` for announcements.
- `clips:edit` for clips.

## Streaming destinations

Streamo supports custom RTMP/RTMPS, SRT, HLS push, and Icecast ingest. Named
configurations validate the protocols supported by Twitch, YouTube, Facebook,
Kick, Vimeo, LinkedIn, and Icecast. A custom destination is not restricted to a
known provider.

The service catalog contains stable protocol and media requirements only.
Encoding is always explicit in the selected `encoding` profile, so changing
provider bitrate recommendations are not hidden in defaults.

Complete examples are available in:

- `examples/twitch.toml`
- `examples/generic-rtmps.toml`
- `examples/icecast.toml`

RTMP and RTMPS use FLV. SRT and HLS use MPEG-TS. HLS `upload_url` must contain
`{stream_key}`, which Streamo replaces without exposing the result in process
diagnostics. Icecast supports AAC/ADTS, MP3, Opus/Ogg, and Vorbis/Ogg and omits
all video inputs, filters, mapping, and encoding.

## Rendering a visual bed

Use `scripts/loop_tester.py` to preview candidate videos before converting them
into ping-pong loops:

```bash
scripts/loop_tester.py videos/*.mp4
```

For each file, the script plays the two seconds before and after the loop point.
Enter `r` to replay, `l` to accept the loop, or return to skip the file.
Files that are already loops, including skipped files and files with `looped` in
the name, are moved into a `loops/` subdirectory. Accepted files are written as
`loops/name-looped.mp4`, and their original files are moved into an `originals/`
subdirectory next to the source file.

Use `scripts/auto_tester.py` to automatically convert videos that are clearly
not loops:

```bash
scripts/auto_tester.py videos/*.mp4
```

The automatic tester compares the first and near-final frames. If they are
clearly different, it writes `loops/name-looped.mp4` and moves the original into
`originals/`. Files that might already be loops are left in place.

Use `scripts/render.py` to turn looped videos and still images into one prepared
video for Streamo:

```bash
scripts/render.py \
  --inputs a-looped.mp4 b-looped.mp4 still.png \
  --output visual-bed.mp4 \
  --duration 3600 \
  --seed 1234 \
  --title-card title.png
```

The renderer starts from black, optionally fades through the title card, and then
chooses inputs at random. It crossfades slowly between scenes and occasionally
fades the title card over the current scene without changing the underlying media
sequence. Each crossfade lasts half the length of the longer adjacent input, and
shorter inputs are looped when necessary to cover the fade.
