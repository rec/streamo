# streamO open issues

This file tracks findings from the 2026-09-29 project review that remain open.
The resolved P1/P2 findings and their tests are in commits `ff2f129`,
`5d5d406`, `45d5156`, and `684644d`. The media-log item is deferred at the
operator's request. No live stream, venue hardware, or provider endpoint was
exercised.

## Deferred reliability work

### 4. Long media jobs accumulate complete FFmpeg output in memory

The render, loop, preview, and re-encode tools use reccy's `run_silent`, which
captures complete subprocess output. FFmpeg stderr can grow with a long job.
Bound diagnostics after measuring a representative long render and failure.
Deferred: log size is not a priority for this pass.

## Reccy and layout notes

streamO already uses reccy's service lifecycle, RPC, atomic output, retry
schedules, and process termination. Its FFmpeg stderr reader overlaps with
`reccy.runtime.process.capture_stderr`; defer this while reccy is changing.
`scripts/media_output.py` and `streamo/credentials.py` have separate contracts
and multiple callers, so their small size alone does not justify inlining.
The feed transport has moved out of `streamo/images.py`; the remaining provider
and render code is coupled closely enough that a split would add navigation
without a clear ownership benefit.

## Additional work beyond the prompt

None.
