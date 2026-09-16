# Handover

## Current work

Feature implementation is tracked in [the feature plan](../plan/features.md).
Preflight is implemented, including CLI/RPC reports and explicit device/provider
probes. The approved publishing-recovery design is implemented with opt-in
`recover_publish`, persistent session resources, and controller-visible retries.
Incident history and actionable health warnings, live titles and the intermission
slate are implemented. `live_overlays` defaults to true and is startup-only;
false disables the compositor and feed polling. The optional image approval queue
is implemented with thumbnail previews, approve/reject RPCs, and saved decisions
in `image_dir/.streamo-image-approval.json`. `image_approval_required` defaults to
false. Skip, pause/resume, show-next controls, and current/upcoming image status
are implemented. Audio/video alignment is next in the feature plan.

streamO has adopted reccy's shared atomic output for participant images, feed
cursors, and approval decisions, and shared retry schedules for audio capture,
HDMI-player recovery, and publishing recovery. Failure classification, ownership,
and the existing delay policies remain local to streamO. Hidden temporary image
files are excluded from discovery and removal. The dependency is pinned to reccy
`88a0b6b34e810cd86a93eb253c4b17131a9ef8e9` for these APIs.

All 22 findings from the completed issue review are addressed in source.
The resolved issue list has been removed; its history remains in Git.
The operator guide is [streamo.md](streamo.md); README only links into it.

Issue 1 was committed as `7cf183f` and audio recovery/lifecycle fixes as
`9b04a71`. CLI-help dependencies are recorded separately in `97c8595`.
The remaining fixes, tests, module moves, and documentation were committed in
`402d0d6`. All implementation commits were pushed.

Verification: 253 tests pass, including image playback/approval, atomic image
visibility, retry timing, overlay layout/cues, incidents, recovery, preflight,
and a small rendered-video regression. Ruff, type checks, pyupgrade, and diff
checks pass.
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
