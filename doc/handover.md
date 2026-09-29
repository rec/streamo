# Handover

## Project state

The [feature plan](../plan/features.md) contains the remaining proposals:
audio/video alignment, repeatable rehearsal, and visual-bed planning preview.
The [operator guide](streamo.md) describes implemented behavior, including
closing credits and reccy's FFmpeg service logging.

Automated tests do not establish that a live provider, venue audio device, HDMI
display, or browser upload works in the target environment. Long-render capacity
also needs measurement on the target machine.

## Decisions

- Audio capture keeps trying after device failure and drops old samples under
  pressure. Status exposes current and last errors, counts, and dropped frames.
- Backup ingest is rejected until supported; no redundancy is implied.
- Local status never waits for provider health. Health refreshes in the background.
- Preview does not load provider credentials or contact provider APIs. A configured
  participant-image feed still polls.
- Configured image directories are trusted. Only internet uploads and feed images
  enter the approval inbox.
- Media tools reject output collisions. Paths in configuration resolve from that
  file; generated render plans contain absolute paths.

## Preserve

Two user-owned, untracked event files must remain untouched:

- `open-loop-forever.md`
- `open-loop.mp4`
