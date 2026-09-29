# streamO open issues

This file tracks findings from the 2026-09-29 project review that remain open.
The resolved P1/P2 findings and their tests are in commits `ff2f129`,
`5d5d406`, and `45d5156`. The media-log item is deferred at the operator's
request. No live stream, venue hardware, or provider endpoint was exercised.

## P1: show and data reliability

### 1. Approval decisions do not identify images across directories

`ImageScheduler` scans every configured image directory, but `ImageApproval`
uses only the first and records decisions by basename. A secondary image with
the same filename as an approved primary image can inherit that approval. The
queue, preview, review, and `image_next` RPCs address only the first directory;
status IDs are ambiguous. Apply one directory-aware identity and approval rule
to discovery, moderation, selection, and status, with duplicate-name tests.
This changes the image-control API or restricts an existing configuration, so
the choice is pending operator direction.

### 4. Long media jobs accumulate complete FFmpeg output in memory

The render, loop, preview, and re-encode tools use reccy's `run_silent`, which
captures complete subprocess output. FFmpeg stderr can grow with a long job.
Bound diagnostics after measuring a representative long render and failure.
Deferred: log size is not a priority for this pass.

## P2: recovery and operational traps

### 11. Overlay controls hold the frame lock during file I/O and decoding

`LiveOverlays.frame()` holds the state lock during directory scans, photo
loading, and full-frame composition. `image_next` decodes under that lock.
A slow volume can block status and controls beyond reccy's one-second RPC
timeout. Keep file I/O and decoding off the state lock while preserving cue
ordering and applied revisions. Verify control responsiveness with delayed
image reads. This needs a scheduling change; operator direction is pending.

### 15. Large render plans can exhaust FFmpeg inputs and decoders

`scripts/render.py` creates one FFmpeg input for every scene and title event,
including repeated sources. A long plan can exhaust process arguments,
descriptors, memory, or decoders. Measure realistic one-hour and worst-case
short-scene plans. If a limit is reached, render bounded segments from the
saved plan while preserving exact timing. Segmented execution is a structural
change; operator direction is pending.

## P3: API and project clarity

### 21. `video_resolution` and `video_frame_rate` describe overlay work, not output

These top-level names sound like the published format, but that format comes
from `streaming_service.encoding.video`. Consider `overlay_resolution` and
`overlay_frame_rate` when a deliberate config change is acceptable, with a
clear transition for existing show files.

### 22. Service model fields suggest capabilities that adapters do not provide

Facebook, Vimeo, LinkedIn, and Icecast models expose provider IDs, tokens, or
admin settings while their adapters publish ingest only. Remove unused fields
or label them as informational. Do not advertise capabilities based on fields
that have no implementation.

### 23. A few files combine unrelated reasons to change

`streamo/images.py` combines feed transport, scheduling, rendering, and
validation. `streamo/providers.py` combines adapter policy, destinations, and
FFmpeg tee escaping. `scripts/render.py` combines plan I/O, execution, and
filter graphs. Split along these boundaries when changing the affected area,
not as a standalone reshuffle. The long operator guide could become two or
three task-based documents.

### 24. Remaining reliability boundaries lack focused tests

The unresolved directory-approval collision, delayed image reads, large render
plans, and PHP manifest append rollback need focused failure tests with their
fixes. Existing render and service tests mostly cover distinct behavior; there
is no clear cluster of redundant tests to remove.

### 25. Shell deployment helpers are easy to misuse

`scripts/copy-server.sh` targets a hard-coded production host with no shebang,
dry-run, or target display. `scripts/set-secret.sh` accepts the room token as a
command-line argument, exposing it to shell history and process listings.
Make the target explicit and read the secret from a protected prompt or file
if these remain supported workflows.

## Reccy and layout notes

streamO already uses reccy's service lifecycle, RPC, atomic output, retry
schedules, and process termination. Its FFmpeg stderr reader overlaps with
`reccy.runtime.process.capture_stderr`; assess the shared helper when changing
the output worker. `scripts/media_output.py` and `streamo/credentials.py` have
separate contracts and multiple callers, so their small size alone does not
justify inlining. No directory count or lock cycle warrants a broad move.

## Additional work beyond the prompt

None.
