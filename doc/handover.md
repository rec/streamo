# Handover

## Current work

Feature implementation is tracked in [the feature plan](../plan/features.md).
Preflight is implemented, including CLI/RPC reports and explicit device/provider
probes. Publishing recovery is next and requires a reviewed lifecycle design.

All 22 findings from the completed issue review are addressed in source.
The resolved issue list has been removed; its history remains in Git.
The operator guide is [streamo.md](streamo.md); README only links into it.

Issue 1 was committed as `7cf183f` and audio recovery/lifecycle fixes as
`9b04a71`. CLI-help dependencies are recorded separately in `97c8595`.
The remaining fixes, tests, module moves, and documentation were committed in
`402d0d6`. All implementation commits were pushed.

Verification: 207 tests pass, including preflight, reccy CLI-help snapshots, and a small
rendered-video regression. Ruff, type checks, pyupgrade, and diff checks pass.
PHP syntax and feed pagination were checked with 10,000 manifest records.
No live provider, audio-device, HDMI, or browser upload tests were run. Planning
is linear and graphs use files, but long-render decoder/memory capacity remains
a target-machine check, not an established guarantee.

## Decisions

- Audio capture keeps trying after device failure and drops old samples under
  pressure. Status exposes current/last errors, error counts, and dropped frames.
- Backup ingest is rejected until supported; no redundancy is implied.
- Local status never waits for provider health. Health refreshes in the background.
- Preview does not load provider credentials or contact provider APIs. A configured
  participant-image feed still polls.
- Media tools reject output collisions. Paths in configuration resolve from that
  file; generated render plans contain absolute paths.
- Media tools are `scripts.auto_loop` and `scripts.preview_loops`; bitrate parsing
  is in `streamo.ffmpeg_progress`. Invoke utilities with `python -m scripts.NAME`.

## Preserve

Two user-owned, untracked event files must remain untouched:

- `open-loop-forever.md`
- `open-loop.mp4`

Other agents are working in sibling repositories. This task changes streamO only.
