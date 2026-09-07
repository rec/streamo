# streamo

`streamo` streams one stereo pair from an audio input device to Twitch while
using a low-resolution pre-rendered animation as the video source.

The first version is intentionally small and independent of `recs`.

## Configuration

Create a JSON config file:

```json
{
  "device_name": "X18",
  "channel": 17,
  "video": "visual-bed.mp4",
  "twitch_key": "live_...",
  "title_card": "title.png",
  "twitch_client_id": "...",
  "twitch_access_token": "...",
  "twitch_broadcaster_id": "123456789"
}
```

Then run:

```bash
streamo --config config.json
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
uv run streamo daemon preview --config ~/.config/streamo/config.json
```

Streamo sends the encoded output to `ffplay`, which opens a live preview window.
The configured audio device must be available, and `ffplay` must be installed
alongside FFmpeg. The config still requires a non-empty `twitch_key`, but preview
does not use it. While preview is running, copy supported image files into
`image_dir`; newly discovered images appear before images already waiting in the
current shuffled cycle. Close the preview window or send `stop` to end both
processes.

## Show-control connection

By default, `streamo` listens on `127.0.0.1:17351` for a local JSON-lines
control connection. Each message is one JSON object followed by a newline.

Start with:

```json
{"type": "hello", "version": 1, "client": "show-control"}
```

Then send commands:

```json
{"type": "command", "id": "1", "command": "status"}
{"type": "command", "id": "2", "command": "mute"}
{"type": "command", "id": "3", "command": "unmute"}
{"type": "command", "id": "4", "command": "stop"}
{"type": "command", "id": "5", "command": "ping"}
{"type": "command", "id": "6", "command": "update_stream_info", "title": "Live at the club", "category": "Music", "tags": ["live"]}
{"type": "command", "id": "7", "command": "chat", "message": "Starting now"}
{"type": "command", "id": "8", "command": "announce", "message": "Recording and streaming"}
{"type": "command", "id": "9", "command": "clip"}
{"type": "command", "id": "10", "command": "marker", "description": "First song"}
```

The control host and port can be changed with `control_host` and
`control_port` in the JSON config. Set `control_enabled` to `false` to disable
the control server.

The Twitch API commands require `twitch_client_id`, `twitch_access_token`, and
`twitch_broadcaster_id`. By default, Streamo uses the broadcaster ID as the chat
sender and announcement moderator. Set `twitch_sender_id` or
`twitch_moderator_id` if those should be different.

The token needs Twitch scopes for the side effects you use:

- `channel:manage:broadcast` for stream info updates and stream markers.
- `user:write:chat` for chat messages.
- `moderator:manage:announcements` for announcements.
- `clips:edit` for clips.

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
