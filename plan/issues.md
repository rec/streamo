# streamO issues

Static review of `f5dd1e5` on 2026-09-29. I read the tracked source, scripts,
configuration, examples, documentation, and test inventory, and compared the
relevant process, file, retry, and RPC helpers with reccy. I did not start a
stream, contact providers, run media tools, or exercise venue hardware. Items
describing failure consequences from unusual timing or external responses need
focused reproduction before implementation.

**P1** can show the wrong image, lose media, or compromise a live show. **P2** is
incorrect behavior or a significant operator trap. **P3** is a smaller usability
or maintainability problem. Each item gives a concrete trigger and a direction;
it is not an instruction to refactor adjacent code.

## P1: show and data reliability

### 1. Approval decisions do not identify images across directories

`ImageScheduler.next_image()` scans every configured directory, but
`ImageApproval` is constructed with only the first one. The scheduler initially
lets secondary paths through its approval filter, while
`ImageFrameProducer.eligible()` later looks up approval by **basename** for all
paths. The queue, preview, review, and `image_next` RPCs can address only the
first directory (`streamo/images.py:150-157,279-285`,
`streamo/moderation.py:45-109`, `streamo/overlays.py:73-83,140-142`).

With approval required, a secondary image cannot normally be approved or
displayed. If an approved primary image has the same filename, an unreviewed
secondary image inherits that decision and can be displayed. Without required
approval, rejecting a primary image can suppress a same-named secondary image.
Status also reports only the filename, so a controller cannot distinguish the
two. Give every selectable image a directory-aware identity and apply one
consistent approval rule across discovery, queue, preview, selection, and status.
Test duplicate names in different directories under both approval modes.

### 2. A stalled audio pipe retains the oldest pending samples

When the nonblocking FFmpeg pipe raises `BlockingIOError`,
`AudioCapture.update()` reports that it is discarding old audio after a second,
but leaves `self.pending` untouched (`streamo/audio.py:107-143`). New blocks
overflow the queue; once the pipe recovers, the old pending block is written
first. This creates delayed audio at exactly the point where the code promises
to shed latency. The existing stalled-pipe test writes a constant signal, so
it cannot distinguish old from fresh samples (`test/test_audio.py:13-50`).
Drop pending data after the stall threshold and verify recovery with distinct
successive sample values at 48 kHz.

### 3. Concurrent image RPCs can overwrite each other's uploads

reccy's RPC server runs requests on separate threads, up to 16 concurrently.
`store_images()` chooses a filename with an existence check, then publishes with
`Path.replace()` (`streamo/control.py:192-205,269-278`). Two requests with the
same source name can both choose the same free target; the later replace
silently destroys the first image while both callers receive success. Reserve
or publish a unique destination atomically, or serialize publication within
the image directory. Add a two-request collision test.

### 4. Long media jobs accumulate complete FFmpeg output in memory

The render, loop, preview, and re-encode commands call reccy's `run_silent`,
which uses `subprocess.run(..., capture_output=True)` without a size limit
(`scripts/render.py:106-115`, `scripts/loop_videos.py:25-35`,
`scripts/preview_loops.py:99-105`, `scripts/reencode_videos.py:63-75`,
`reccy/runtime/process.py:77-89`). FFmpeg's long-running stderr can therefore
grow with the job, even though success discards it. Stream bounded diagnostics
to a tail or file while preserving actionable failure output; measure a
representative long render and failure before changing the shared helper.

### 5. An overlay worker can die while FFmpeg keeps publishing

The overlay thread calls `producer.frame()` and catches only `BrokenPipeError`
(`streamo/overlays.py:210-227`). An oversized or otherwise pathological image
from a watched folder can make Pillow raise `Image.DecompressionBombError`,
which `ImageFrameProducer.next_frame_image()` does not catch
(`streamo/images.py:288-308,334-341`). The worker then closes its pipe;
FFmpeg's overlay filter uses `eof_action=pass` (`streamo/composition.py:133-140`),
so video can continue without overlays and no controller-visible worker error.
Classify invalid image failures as skippable, and report any fatal worker exit
to the main loop and status. Test both cases without depending on timing luck.

## P2: recovery, user errors, and operational traps

### 6. Shutdown may leave provider or feed work running against closed state

`HealthPoller.close()` waits one second, although each provider request may
wait ten seconds and OAuth refresh may take longer (`streamo/control.py:148-185`,
`streamo/youtube_api.py:44-55,66-79`). `ImageFeedPoller.stop()` waits 15 seconds,
but an outstanding response can outlast its per-operation network timeout
(`streamo/images.py:80-116,353-390`). Both threads are daemons. In
`streamo/streamer.py:119-153`, the health close callback precedes
`service.finish`, but the short join does not guarantee the worker has stopped.
It may still use the service while `finish()` runs; a feed may still write after
streamO closes. Define cancellation and completion semantics, then test shutdown
during a blocked request. If an in-flight request cannot be cancelled, avoid
concurrent teardown of resources it still uses.

### 7. Secondary image directories are absent from preflight

`check_storage()` checks writability only for `primary_image_dir`
(`streamo/preflight.py:302-328`). The scheduler reads every directory each
interval (`streamo/images.py:150-151`), so a missing, unreadable, or non-directory
secondary path can yield an apparently ready report but no images from that
weighted source, or fail later during a scan. Report the primary writable
destination and each additional readable source separately; distinguish an
intentionally empty directory from one that cannot be scanned.

### 8. Image control filesystem failures do not produce useful RPC errors

`handle_request('remove_last_image')` calls `stat()` and `unlink()` without
handling races or permission errors (`streamo/control.py:103-108,249-257`).
`store_images()` also creates the directory and staging area before its
`ImageStoreError` catch (`streamo/control.py:112-125,192-205`). reccy's request
handler catches value/type errors but not `OSError` (`reccy/protocol/rpc.py`,
`Server._serve_control`), so a full disk, revoked permission, or concurrent
deletion can terminate the request thread with no structured answer. Convert
expected filesystem failures into an `ipc.Error`, keeping the original batch
state clear. Test publication failure and deletion during removal.

### 9. A malformed successful provider response escapes the API error boundary

Twitch and YouTube call `json.loads()` on successful responses without catching
decode errors (`streamo/twitch_api.py:199-209`,
`streamo/youtube_api.py:252-274`). Kick already converts invalid JSON into
`KickApiError` (`streamo/kick_api.py:259-267`). A proxy error page or truncated
body can therefore escape controller and health-poller catches; the latter
dies, leaving only a later stale-health warning (`streamo/control.py:164-185`).
Normalize invalid responses to provider errors and test a 200 response with
invalid JSON for both request paths.

### 10. Unknown configuration keys are mostly ignored

`Streamo` and several nested service/ingest models use Pydantic's default
extra-field behavior (`streamo/config.py:23-58,236`,
`streamo/provider_config.py:119-174,197-229`). A misspelling such as
`recover_publsh` or the former `image_dirs` key can load successfully while
the requested behavior stays disabled. RTMP ingest explicitly forbids extras,
but SRT, HLS, and Icecast do not, so unsupported backup fields may be silently
accepted despite the guide's blanket statement that backup settings are
rejected. Reject unknown configuration keys throughout the operator-facing
schema and test near-miss spellings and backup fields on each ingest variant.

### 11. Overlay controls hold the frame lock during file I/O and decoding

`LiveOverlays.frame()` holds one lock while scanning directories, loading and
decoding photos, and building a full output frame
(`streamo/overlays.py:166-203`, `streamo/images.py:150-194,288-307`).
`image_next` decodes an image under the same lock
(`streamo/overlays.py:132-150`, `streamo/images.py:264-269`). A slow volume or
large image can block both the frame producer and status/title/image RPCs long
enough to exceed reccy's default one-second client timeout. Move slow work
outside the state lock or bound it while retaining atomic cue snapshots; test
control responsiveness with deliberately delayed image reads.

### 12. Capture and overlay dimensions have no practical resource bounds

Configuration requires positive dimensions and rates but no reasonable upper
bound (`streamo/config.py:37-40,115-122,141-146`,
`streamo/provider_config.py:54-67`). The overlay producer allocates an RGBA
frame at encoded output resolution on every frame, plus working images and
copies (`streamo/overlays.py:48-68,166-203`,
`streamo/images.py:232-257`). A typo such as an extra zero in a resolution can
exhaust memory before a useful operator error appears. Validate a documented
supported envelope or compute and report the estimated memory/pipe rate before
starting; measure target hardware before choosing limits.

### 13. Unexpected startup and worker errors can be reported as a stop

`streamer.stream()` only translates `EncoderLaunchError` and
`KeyboardInterrupt` into explicit outcomes. For other failures while opening
FFplay, starting a thread, reading an image directory, or updating the local
display, `finally` clears `publish_requested` and sets a nonfailed state to
`stopped` (`streamo/streamer.py:108-220,227-307`). The exception may be logged
by the service manager, but the controller-facing state does not retain the
reason. Record a failed status/incident for unexpected runtime exceptions after
cleanup, without turning an intentional stop into failure. Exercise one
resource-exhaustion error and one local I/O error.

### 14. A failed PHP manifest append can poison the next upload/feed

On upload, `foto.php` moves the JPEG and appends its ID to `images.jsonl`.
If `fwrite()` writes only part of a line, the code unlinks the JPEG but leaves
the partial manifest tail (`web/foto.php:75-109`). The next upload appends after
that partial record, so the feed can emit invalid JSON or lose ID continuity.
Restore the pre-append file size while holding the lock on a failed write, and
test short writes or a disk-full simulation. Keep the image/manifest commit
order explicit.

### 15. Large render plans can exhaust FFmpeg inputs and decoders

`scripts/render.py:262-354` creates one FFmpeg input per scene and title event,
even when scenes reuse the same source. The filter graph is moved to a file, but
the input argv and simultaneously open decoders still grow with duration and
short scene length. A long plan can hit process argument, descriptor, memory,
or decoder limits after planning successfully. Measure realistic one-hour and
worst-case short-scene plans; if a limit is reached, render bounded segments
from the saved plan while preserving its exact timing.

### 16. Kick authorization can wait forever for the callback

`receive_kick_code()` calls `HTTPServer.handle_request()` without a timeout
(`streamo/auth.py:166-188`). Closing the browser, a redirect failure, or a
callback port that cannot be reached leaves the command waiting indefinitely;
an unrelated first GET can also end it with a state error. Set a clear timeout
and allow a mismatched request to be rejected without consuming the entire
authorization attempt. Verify timeout, invalid request, and interruption
cleanup.

### 17. Kick token rotation is vulnerable to a credential-file write failure

`KickAccessTokenProvider.refresh()` receives a new token pair, then writes the
rotated refresh token before updating its in-memory credentials
(`streamo/kick_api.py:114-139`). If storage fills or permissions change after
the provider rotates the token, the old token remains on disk and the new one
is lost when the exception unwinds. This may require reauthorization rather
than a transient retry. Decide how to surface and recover this irreversible
state; test the post-response write-failure path with a fake transport.

### 18. Media tools expose raw failures for ordinary bad inputs

The three duration probes assume parseable positive ffprobe output and call
`float()` or `int()` directly (`scripts/render.py:145-171`,
`scripts/preview_loops.py:151-165`, `scripts/reencode_videos.py:104-128`,
`scripts/loop_videos.py:43-60`). For a file without a video stream, `N/A`, an
empty response, or zero duration, operators can get a traceback or division
error instead of a named file and remedy. Centralize the shared duration/frame
probe only if that reduces code, and return a concise diagnostic. Also make
`preview_loops.py:42-63` treat EOF and Ctrl-C as a clean skip/stop.

### 19. Filename heuristics silently skip videos that are not loops

`auto_loop.py:55-56` and `preview_loops.py:66-67` treat any filename containing
`looped` as already looped. `unlooped.mov` or a venue name containing that text
therefore bypasses endpoint inspection; preview mode can move the file directly
to `loops/`. Match the actual generated `-looped` suffix or require an explicit
operator decision before moving it. Test misleading names.

### 20. Local-display failure is visible only in logs

`local_display` defaults to true (`streamo/config.py:43`), but on a non-Linux
host `drm_connected()` sees no connector and does nothing
(`streamo/streamer.py:43-59,392-399`). On Linux, FFplay launch/exit failures are
logged and retried but are not included in `RuntimeState.snapshot()`
(`streamo/streamer.py:43-79`, `streamo/runtime.py:74-109`). A controller sees a
healthy stream while the venue screen is blank. Report requested/connected/
playing/error display state separately from remote publishing, and make the
platform limitation obvious when enabling the setting.

## P3: API and project clarity

### 21. `video_resolution` and `video_frame_rate` describe overlay work, not output

These top-level names sound like the published format, but the published
resolution/rate come from `streaming_service.encoding.video`; the top-level
values size and pace overlay assets (`streamo/config.py:37-40`,
`streamo/overlays.py:51-58`, `streamo/composition.py:53-79`). The guide explains
the distinction, but it remains an easy configuration trap. Consider names
such as `overlay_resolution` and `overlay_frame_rate` when a deliberate config
change is acceptable, with a clear transition for existing show files.

### 22. Service model fields suggest capabilities that adapters do not provide

Facebook, Vimeo, LinkedIn, and Icecast service models expose provider IDs,
tokens, or admin settings (`streamo/provider_config.py:277-283,306-326`), while
their adapters inherit ingest-only behavior (`streamo/providers.py:250-252,
303-312`). The guide states this, yet an operator may reasonably expect those
fields to enable API control or health checks. Remove unused fields until the
behavior exists, or label them explicitly as informational/unused in the
configuration guide. Do not advertise a capability based on storing a field.

### 23. A few files combine unrelated reasons to change

`streamo/images.py` (440 lines) contains feed transport, scheduling, frame
rendering, and image validation. `streamo/providers.py` (461) combines adapter
policy, destination construction, and FFmpeg tee escaping. `scripts/render.py`
(390) combines plan-file I/O, execution, and filter graph generation. These
sizes are manageable, but each file spans independently changing concerns and
increases review risk. Split along those existing boundaries only when changing
one of the areas; avoid a standalone reshuffle. The 709-line operator guide
also mixes show operation with PHP deployment and development instructions;
two or three task-based documents could shorten the path to common commands.

### 24. Some reliability boundaries are not covered by the current tests

Tests cover normal weighted image selection, primary-directory moderation,
recovery schedules, and fake provider success. They do not cover the
cross-directory approval collision (issue 1), old-versus-new samples after a
stall (issue 2), concurrent uploads (issue 3), malformed 200 provider responses
(issue 9), blocked shutdown (issue 6), or PHP append failure (issue 14).
`test/test_render.py` and `test/test_services.py` are large (564 and 498 lines),
but their cases mostly exercise distinct behavior; I found no clear cluster of
redundant tests worth deleting. Add focused tests with the corresponding fixes
instead of broad new suites or tests that mirror implementation details.

### 25. Shell deployment helpers are easy to misuse

`scripts/copy-server.sh` is a one-line `rsync` to a hard-coded production host
with no shebang, dry-run, or target display. `scripts/set-secret.sh` takes the
room token as a command-line argument (`scripts/set-secret.sh:1-14`), exposing it
to shell history and process listings during setup. Make the deployment target
explicit and read the secret from a protected prompt or file if these scripts
remain the supported workflow. This is separate from the PHP page's normal
token-in-URL capability model.

## Reccy and layout notes

streamO already uses reccy's service lifecycle, JSON Lines RPC, atomic output,
retry schedules, and process termination. The custom FFmpeg stderr reader in
`streamo/streamer.py:322-331` overlaps with
`reccy.runtime.process.capture_stderr`, which already bounds individual reads
and retains a tail. Check whether its progress callback can replace the local
reader when addressing issue 5 or another process-worker change. The small
`scripts/media_output.py` has a distinct no-replace hard-link contract and is
used by three tools; `streamo/credentials.py` preserves private credential
permissions for two callers. Their size alone is not a reason to inline them.
No directory has an unusual number of tracked entries (22 in `streamo/`, 38
under `test/` including fixtures), and no broad directory move is indicated.
I found no confirmed cyclic lock ordering or deadlock; the observed contention
path is issue 11.

## Additional work beyond the prompt

None. This document records findings only.
