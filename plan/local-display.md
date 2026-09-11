# Local HDMI display

## Goal

When `local_display` is enabled, a video Streamo run keeps a local MPEG-TS
preview feed available on loopback as well as publishing to its remote ingest.
Streamo watches DRM connector status while it is running. It starts a
fullscreen HDMI player when an output becomes connected and stops that player
when the last connected output disappears. This must not interrupt the remote
stream or restart FFmpeg.

`local_display` defaults to `true`. It has no effect for audio-only services,
which have no video to display.

The local player is intentionally silent. The MPEG-TS feed includes audio so
that it is an exact encoded copy of the live program, but FFplay uses `-an` to
avoid creating an unexpected second audio output through HDMI.

## Design

### Configuration and constants

1. Add `local_display: bool = True` to `Streamo` in `streamo/config.py` and
   document it in the top-level configuration table in `README.md`.
2. Define the loopback UDP URL, DRM status root, connector poll interval, and
   player shutdown timeout in `streamo/streamer.py`. Keep these implementation
   constants rather than adding configuration for one Raspberry Pi display.
3. Use a fixed high unprivileged UDP port on `127.0.0.1`. The feed must never
   bind a LAN interface or expose the show network to participants.

### FFmpeg output

1. Replace the current final-output assembly with a representation of one
   encoded output destination: muxer, URL, muxer options, and which fields are
   secret. Build this from each existing RTMP, SRT, HLS push, and Icecast
   service configuration rather than trying to parse the current flat FFmpeg
   argument list.
2. Keep the existing normal output for runs without local display, for
   audio-only streams, and for `daemon preview`.
3. For a non-preview video run with local display enabled, make FFmpeg’s sole
   final output a tee muxer with two slave outputs:

   - the existing remote destination, retaining its container and all existing
     service-specific output options;
   - `mpegts` to the loopback UDP preview URL.

   The codecs, overlays, maps, and encoder options remain before this final tee
   output, so FFmpeg composes and encodes exactly once. The two outputs only
   mux and transmit the resulting packets.
4. Serialize tee slave options with FFmpeg tee-muxer escaping, including URLs
   and secrets that contain tee delimiters. Preserve secret redaction in failed
   FFmpeg diagnostics after this change.
5. Preserve the existing NUT-on-stdout preview path and its dedicated FFplay
   command. It remains a manually requested preview and is not the HDMI
   implementation.

### DRM monitor and player lifecycle

1. Add a small local-display controller in `streamo/streamer.py`, owned by the
   `stream()` call. It polls `/sys/class/drm/*/status` while FFmpeg is alive.
   A display is present when at least one connector status file reads
   `connected`. Missing or unreadable DRM entries mean no display, not a
   streaming failure.
2. Start the controller after FFmpeg has started, so the UDP sender already
   exists. On the transition from no connected outputs to one or more, launch
   FFplay. On the inverse transition, terminate and reap only that FFplay
   process. Repeated status values do nothing.
3. Make the player command open the loopback MPEG-TS URL with low buffering,
   use FFplay fullscreen mode, disable audio, and select SDL’s KMSDRM video
   driver. It must not require X11, Wayland, or a desktop session.
4. Treat an FFplay launch or exit as a display failure, not a Streamo failure.
   Continue polling and allow a later connector transition to launch a new
   player. Ensure every player is terminated in `stream()` cleanup, including
   FFmpeg failure, stop command, and keyboard interruption.
5. Do not stop Streamo because HDMI is absent at startup. A cable connected
   later should start the player; after the next video keyframe it should show
   the already-running program. With the current two-second keyframe interval,
   this is normally within two seconds.

### Service installation

1. Inspect the systemd unit produced by `streamo daemon install` on the Pi.
   Ensure the service account has read/write access to `/dev/dri` through the
   `video` group and is not restricted by a device allow-list that excludes
   DRM devices.
2. If the generated unit cannot express those permissions, add a narrow
   documented systemd drop-in for Streamo, not a second streaming service. The
   Streamo child process should inherit the required DRM access. Reload systemd
   and restart Streamo after installing the drop-in.
3. Verify the Pi FFplay build contains SDL KMSDRM support before enabling the
   feature in production. If it does not, report that deployment prerequisite
   rather than silently falling back to a desktop display driver.

## Tests

1. Extend `test/test_config.py` to prove that `local_display` defaults to true
   and TOML can disable it.
2. Extend `test/test_streamer.py` to assert that a video command with local
   display has one tee output containing the remote destination and the
   loopback MPEG-TS destination, and that the encoder arguments occur once.
   Cover remote output serialization and redaction for each supported ingest
   type that has per-output options.
3. Test that audio-only and manual preview commands do not use the local
   display tee output.
4. Unit-test DRM status transitions with a temporary sysfs-like directory and
   mocked subprocesses: disconnected at startup, connection, no repeated
   launch, disconnection, reconnection, launch failure, and cleanup.
5. On the Pi, manually verify a stream started with HDMI unplugged, then plug
   and unplug the cable. Confirm fullscreen display begins within a keyframe,
   the remote stream is continuous, and no desktop session is required.

## Files expected to change

- `streamo/config.py`
- `streamo/streamer.py`
- `streamo/services.py`
- `test/test_config.py`
- `test/test_streamer.py`
- `README.md`
- systemd documentation or a committed Streamo unit drop-in only if the
  installed service needs explicit DRM permissions
