# Porting from Twitcho to Streamo

Streamo replaces Twitcho rather than providing a compatibility layer. Port an
installation by changing the package and service identity, converting the JSON
configuration to TOML, and moving Twitch settings under `streaming_service`.
JSON Lines control messages remain JSON Lines.

## 1. Meet the new requirements

Streamo requires Python 3.13 or later. It still requires FFmpeg, FFprobe, and an
audio input device. FFplay is required for preview.

Install Streamo's environment from the repository:

```bash
uv sync
```

The installed command and Python package are both named `streamo`:

| Twitcho | Streamo |
| --- | --- |
| `twitcho` | `streamo` |
| `python -m twitcho` | `python -m streamo` |
| `from twitcho.config import Twitcho` | `from streamo.config import Streamo` |

There is no `twitcho` command, package alias, or `Twitcho` class in Streamo.

## 2. Replace the installed service

Stop and uninstall the old service while the `twitcho` command is still
available:

```bash
twitcho daemon stop
twitcho daemon uninstall
```

After converting the configuration, install and start Streamo:

```bash
streamo daemon install --config ~/.config/streamo/config.toml
streamo daemon start
```

The service identity changes throughout the installation:

| Twitcho | Streamo |
| --- | --- |
| `com.swirly.twitcho` | `com.swirly.streamo` |
| `TWITCHO_DAEMON` | `STREAMO_DAEMON` |
| `twitcho` RPC role | `streamo` RPC role |
| `~/.local/state/twitcho/` | `~/.local/state/streamo/` |

With the default state directory on macOS and other Unix systems, the local
control socket changes from `~/.local/state/twitcho/gui.sock` to
`~/.local/state/streamo/gui.sock`. Prefer discovering the endpoint through
Reccy rather than constructing it in application code.

Do not delete the old configuration until the Streamo service has been tested.

## 3. Convert JSON configuration to TOML

Twitcho read `~/.config/twitcho/config.json`. Streamo reads
`~/.config/streamo/config.toml` by default and does not parse JSON
configuration.

These capture and overlay fields remain at the top level:

- `device_name`
- `channel`
- `video`
- `title_card`
- `sample_rate`
- `video_resolution`
- `video_frame_rate`
- `title_interval`, `title_duration`, and `title_fade`
- `image_dir`, `image_interval`, `image_duration`, and `image_fade`

Twitch ingest, API, and output-encoding fields move into the nested service
configuration:

| Twitcho JSON field | Streamo TOML field |
| --- | --- |
| `twitch_url` | `streaming_service.ingest.server_url` |
| `twitch_key` | `streaming_service.ingest.stream_key` |
| `twitch_client_id` | `streaming_service.client_id` |
| `twitch_access_token` | `streaming_service.access_token` |
| `twitch_broadcaster_id` | `streaming_service.broadcaster_id` |
| `twitch_sender_id` | `streaming_service.sender_id` |
| `twitch_moderator_id` | `streaming_service.moderator_id` |
| `twitch_api_url` | `streaming_service.api_url` |
| `audio_bitrate` | `streaming_service.encoding.audio.bitrate` |
| `video_bitrate` | `streaming_service.encoding.video.bitrate` |

Some old fields supplied settings to both capture or overlays and the encoded
output. They must now appear in both places if the two values should remain the
same:

| Twitcho field | Streamo locations |
| --- | --- |
| `sample_rate` | top-level `sample_rate` and `streaming_service.encoding.audio.sample_rate` |
| `video_resolution` | top-level `video_resolution` and `streaming_service.encoding.video.resolution` |
| `video_frame_rate` | top-level `video_frame_rate` and `streaming_service.encoding.video.frame_rate` |

The top-level video resolution and frame rate control the working format for
overlays. The nested values control the encoded stream.

If an optional Twitcho encoding field was absent, use its old default when
constructing the new explicit profile: `48000` for sample rate, `160k` for
audio bitrate, `150k` for video bitrate, `640x360` for resolution, and `10` for
frame rate.

For example, this Twitcho configuration:

```json
{
  "device_name": "X18",
  "channel": 17,
  "video": "visual-bed.mp4",
  "title_card": "title.png",
  "twitch_url": "rtmps://live.twitch.tv/app",
  "twitch_key": "replace-with-stream-key",
  "sample_rate": 48000,
  "audio_bitrate": "160k",
  "video_bitrate": "150k",
  "video_resolution": "640x360",
  "video_frame_rate": 10,
  "twitch_client_id": "replace-with-client-id",
  "twitch_access_token": "replace-with-access-token",
  "twitch_broadcaster_id": "123456789"
}
```

becomes:

```toml
device_name = "X18"
channel = 17
video = "visual-bed.mp4"
title_card = "title.png"
sample_rate = 48000
video_resolution = "640x360"
video_frame_rate = 10

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
bitrate = "150k"
resolution = "640x360"
frame_rate = 10
keyframe_interval = 2
pixel_format = "yuv420p"
```

The encoding section is now explicit. The values above reproduce Twitcho's AAC,
H.264, FLV, stereo, and pixel-format choices. `keyframe_interval` is new and is
measured in seconds. If the old `twitch_url` starts with `rtmp://`, either set
`protocol = "rtmp"` or move to Twitch's secure RTMPS endpoint as shown above.
Keep the stream key in `stream_key`; do not append it to `server_url`.

The API credential fields are optional. Keep them only if the application uses
Twitch metadata, chat, announcement, clip, or marker commands. `sender_id` and
`moderator_id` still default to `broadcaster_id`.

See the complete [Twitch example](../examples/twitch.toml) and the main
[Streamo documentation](../README.md) for all current fields and defaults.

## 4. Keep JSON Lines control messages

Do not convert `.jsonl` files or control messages to TOML. Streamo uses Reccy's
JSON Lines RPC transport, in which each message is one JSON object followed by
a newline.

The current handshake and request forms are:

```json
{"type": "hello", "role": "show-control", "version": 1}
{"type": "request", "command": "status", "params": {}}
{"type": "request", "command": "mute", "params": {}}
{"type": "request", "command": "update_stream_info", "params": {"title": "Live at the club"}}
```

The command names `status`, `mute`, `unmute`, `stop`, `ping`, `image`,
`update_stream_info`, `chat`, `announce`, `clip`, and `marker` remain available
for a Twitch configuration. Twitch API commands require the corresponding
credentials and token scopes.

Very old Twitcho clients that send `type = "command"`, put command parameters
at the top level, or connect using `control_host` and `control_port` must be
ported to the Reccy request envelope and local control endpoint. Those old
control configuration fields have no effect in Streamo.

## 5. Update status consumers

The existing audio, FFmpeg, mute, timing, and error fields remain. Streamo also
reports:

- `service`, the configured service name;
- `endpoint_host`, the ingest hostname with no credentials;
- `capabilities`, the operations supported by the configured adapter;
- `remote_health`, provider health data when the adapter supplies it.

Code consuming status must tolerate `remote_health` being `null`. It must not
expect a stream key, password, passphrase, access token, or complete publish URL
in status or failure diagnostics.

## 6. Verify the port

First preview the local audio and video composition without contacting Twitch:

```bash
streamo daemon preview --config ~/.config/streamo/config.toml
```

Preview requires the configured audio and media devices but does not prepare or
publish a remote stream. Check the title card and participant-image overlays if
they are enabled.

Then install or run Streamo with the real destination credentials. Confirm the
audio pair, encoded resolution and frame rate, Twitch ingest in the Twitch
operator interface, control commands, and service restart behavior before
removing the Twitcho installation or configuration.
