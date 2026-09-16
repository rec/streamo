# streamO

streamO sends a stereo audio-device pair to a live-streaming destination. It
combines audio with a looping visual bed, title cards, participant photos, and
an optional local HDMI display, or publishes audio-only to Icecast.

Start with the [operator guide](doc/streamo.md) for installation, configuration,
preview, show control, uploads, authorization, and media preparation.
Copy an example for [Twitch](examples/twitch.toml),
[generic RTMPS](examples/generic-rtmps.toml), or [Icecast](examples/icecast.toml).

```bash
uv sync
uv run streamo --help
uv run streamo daemon preview --config ~/.config/streamo/config.toml
```

Requires Python 3.13+, FFmpeg, FFprobe, an audio input device, and FFplay for
preview or local display. Live operation uses the same configuration:
`uv run streamo --config ~/.config/streamo/config.toml`.

Contributor context: [handover](doc/handover.md).
