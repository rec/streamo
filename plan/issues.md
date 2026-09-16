# streamO issues

Source review of commit `29ec7d0`, 2026-09-16. All 22 findings have been
addressed in source. Each resolution below describes the change; the original
evidence is retained for context and can refer to renamed files or old behavior.

Issues 1–3 were committed separately. CLI-help dependencies are recorded in
`97c8595`, independently of the remaining implementation and documentation fixes.
The temporary Git write restriction has been resolved.

Verification: 197 pytest tests, Ruff, type checks for `streamo` and `scripts`,
pyupgrade, and `git diff --check`; PHP syntax and feed cursor/page boundaries
checked against 10,000 records. Tests include a small rendered-video regression.
No live streaming, provider calls, browser upload interaction, or HDMI/audio
hardware tests were performed. Hour-long render planning was measured; actual
long-render decoder/memory capacity still needs target-machine validation.

## Additional work beyond the prompt

None.

Priorities: **P1** can disrupt a show, expose credentials, or overwrite media;
**P2** is incorrect behavior or a significant operator trap; **P3** is
maintainability, naming, or an improvement needing measurement.

## Reliability and credentials

### 1. P1: FFmpeg stderr bypasses secret redaction

**Resolved:** failure reporting now sanitizes captured stderr as well as argv,
including raw, URL-encoded, and tee-escaped ingest URLs and secrets. Regression
coverage includes RTMP(S), SRT, HLS segment URLs, and Icecast credentials.

**Evidence:** `streamo/streamer.py:stream` passes a redacted command but the
original `ffmpeg_output` to `process.report_failed_process`. The reccy helper
prints the captured stderr unchanged. `test/test_streamer.py` checks command
redaction, not diagnostic output.

If an FFmpeg diagnostic contains its output URL, the stream key or Icecast
password can reach logs. This contradicts the documentation's promise about
failure diagnostics. Redact secrets from the diagnostic text as well as argv;
verify with synthetic stderr containing each supported secret-bearing URL.

### 2. P1: Audio capture depends on a blocking pipe write

**Resolved:** capture uses a bounded queue with oldest-block drops and a
nonblocking pipe writer in the control loop. Status exposes current/last audio
errors, error count, and dropped frames; unavailable or inactive capture is
retried. Tests exercise a genuinely full pipe, partial writes, and recovery.

**Evidence:** `streamo/streamer.py:_audio_callback` performs level calculations,
locking, allocations, and `process.stdin.write()` inside the audio callback.
Its `status` argument is ignored. The main loop watches FFmpeg exit, but does
not check capture activity or whether `last_audio_at` has stopped advancing.

When FFmpeg stops consuming input, the callback can block. A callback-side
write failure is not communicated explicitly to the controlling loop; the
outer `except BrokenPipeError` is not a callback-thread error channel. A live
FFmpeg process can therefore coexist with stalled audio. Separate bounded
audio transfer from the callback and make overflow, callback failure, and
capture inactivity visible to the main loop. Reproduce using a stalled writer
before deciding queue capacity and shutdown behavior.

### 3. P1: Startup failures can escape resource cleanup

**Resolved:** an ExitStack owns process, pipe, capture, display, and service
cleanup from acquisition onward, including non-OSError failures. Regression
tests cover preview command failure and failed service preparation.

**Evidence:** `streamo/streamer.py:stream` prepares the remote service before
its cleanup scopes. Process setup catches only `OSError`. Image frame-producer
construction and stderr-capture setup happen after FFmpeg starts but before
the main `try/finally`.

For example, malformed `video_resolution` raises `ValueError` while constructing
overlay inputs after preview FFplay and image pipes may already exist. An
image-producer construction failure can leave FFmpeg running. Put acquisition
and cleanup under one lifecycle scope, validate local settings before remote
preparation, and ensure cleanup continues if an earlier cleanup step fails.

### 4. P2: Successful stop and uninstall report failure

**Resolved:** Daemon exit codes now test the requested end state, including stopped and uninstalled services.

**Evidence:** `streamo/daemon.py:run` returns 1 whenever `result.running is False`,
independently of the requested action. A stopped process is the intended result
of `stop`; an uninstalled service is also not running. Existing daemon tests
cover an install result with `running=True`, not these successful outcomes.

Use action-specific success conditions so shell scripts can distinguish a
successful stop/uninstall from a failed start.

### 5. P2: Local status depends on a successful remote health request

**Resolved:** Provider health is cached by a background poller every 30 seconds. Status returns immediately with a separate health error and update timestamp.

**Evidence:** `streamo/control.py:ControlController.handle_request` calls
`self.service.health()` synchronously before returning the local snapshot.
Unlike service-command handling, this branch does not catch provider errors.
Provider transports use network requests with ten-second timeouts and may
refresh tokens or retry authorization.

A provider outage can delay or prevent access to otherwise available local
audio levels and FFmpeg status. Return local status even if remote health is
unavailable; represent the health failure separately and define its refresh
cadence rather than making every status request an API poll.

### 6. P2: HDMI player failure requires a cable state change to recover

**Resolved:** A connected HDMI display retries a failed player after five seconds and retains its stderr for failure logging.

**Evidence:** `streamo/streamer.py:LocalDisplayController.update` clears an exited
player but returns immediately when connector state still equals `connected`.
`start_player` also leaves `connected=True` after a launch failure.

A transient FFplay failure leaves a connected display blank until an unplug/
replug. Decide and document whether bounded recovery is intended; retain the
player's error output so the operator can diagnose a blank display. Current
tests exercise recovery following a connector transition, not a continuously
connected display.

## Configuration and advertised behavior

### 7. P2: Backup ingest configuration is accepted but unused

**Resolved:** By explicit user decision, backup settings are rejected at validation and the unsupported capability has been removed.

**Evidence:** `streamo/services.py:RtmpIngest` validates backup URL/key pairs and
`GenericServiceAdapter` advertises `BACKUP_INGEST` when supplied. However,
`ingest_output` creates only the primary destination and never reads the backup
fields.

An operator can believe redundancy is enabled when only the primary is used.
Either implement the intended backup semantics or reject/remove those fields
and the capability until supported. Specify whether backup means simultaneous
publishing or failover before implementation.

### 8. P2: Enabling an overlay reduces the entire base video's quality

**Resolved:** Base video scaling and frame rate now follow the output encoding profile. Overlay working settings no longer reduce base-video quality.

**Evidence:** `streamo/streamer.py:overlay_filter` scales the base video to
`video_resolution` and converts it to `video_frame_rate`. Defaults are 640x360
at 10 fps. Output encoding can subsequently scale it to 1280x720 at 30 fps,
as in the README example. Without overlays the base skips that working-format
conversion.

Adding a title card or participant images therefore changes the base video's
effective resolution and motion, even when the encoding profile is unchanged.
Avoid degrading the base to the overlay format, or make the tradeoff explicit
and align defaults/examples with it.

### 9. P2: Relative media paths depend on launch directory

**Resolved:** Configuration media, image directories, and credential paths resolve relative to the TOML file, with home expansion. Generated render plans use absolute paths; hand-written relative paths resolve from the plan file.

**Evidence:** `streamo/daemon.py:load_config` expands only the configuration
filename. `video`, `title_card`, and `image_dir` remain relative paths. Daemon
installation resolves the config path but does not resolve its contents.

The same TOML can select different media or image directories when run from a
shell versus a service. `~` in these media paths is also not expanded here.
Define one path base, apply it consistently, and document it. Apply the same
decision to paths stored in render plans.

### 10. P2: Configuration accepts values that fail only during streaming

**Resolved:** Dimensions and finite timings are validated, including feed polling. Readable local media and decodable title cards are checked before provider preparation. Unsupported video/audio-only container combinations are rejected.

**Evidence:** `streamo/config.py` does not validate `video_resolution`, checks
only presence of `video`, and checks `title_card.exists()` rather than a usable
image file. Time validators use comparisons that do not reject NaN or positive
infinity. `streamo/services.py:EncodingProfile.validate_codecs` checks only
audio/container compatibility, allowing a custom video configuration with an
audio-only container such as MP3.

Validate working dimensions, finite timings, local media readability, and
unambiguously invalid video/container combinations before starting processes
or updating provider state. Keep runtime capability checks separate from
structural configuration validation.

### 11. P2: Preview still requires provider credential initialization

**Resolved:** Preview uses local ingest composition without constructing a credential-backed provider adapter or polling provider health. A configured participant feed remains active, as documented.

**Evidence:** `streamo/config.py:Streamo.run` always constructs `adapter_for`
before passing `preview=True`. YouTube and Kick adapter constructors load their
configured credential files. The preview flag suppresses prepare/publish/finish,
but not credential initialization; status requests still use adapter health.

Local preview can fail because remote credentials are absent or invalid, and
a preview status request can contact the provider. Decouple local composition
preview from provider initialization and make the documented preview boundary
match the implementation.

## Participant images and uploads

### 12. P2: One bad feed image blocks all later uploads

**Resolved:** Missing (404/410), oversized, or invalid feed images are logged and skipped with cursor advancement. Transient failures remain retryable and do not advance the cursor.

**Evidence:** `streamo/images.py:ImageFeedPoller.poll` advances its cursor only
after downloading and validating each image. An error aborts the iteration;
`run` retries from the unchanged cursor.

A permanently missing or invalid early image is retried forever, preventing
later valid photos from reaching the show. Distinguish retryable failures from
permanent rejected entries and define an explicit skip/quarantine policy with
an operator-visible error.

### 13. P2: Feed backlog can exceed a hard client limit permanently

**Resolved:** Feed pages are capped at 1000 IDs. Cursor lookup uses binary search over the append-only manifest; uploads read only its tail. PHP boundary checks cover 10,000 records.

**Evidence:** `web/foto.php:send_feed` emits every entry after the cursor without
pagination. `streamo/images.py:fetch_feed_items` rejects responses larger than
256 KiB without processing any entries or advancing the cursor.

A sufficiently large backlog cannot be caught up by restarting the client.
Bound server responses and let the cursor drain successive batches. The PHP
feed and upload paths also scan the entire manifest for every poll/upload, so
their work grows with total show history, not just new entries.

### 14. P2: The image RPC has weaker guarantees than the remote feed

**Resolved:** RPC images have 8 MiB and 2048-pixel limits and must decode as supported formats. The entire batch validates in staging before individual atomic publication; a publication failure rolls back that batch.

**Evidence:** `streamo/control.py:store_image` reads entire local files or HTTP
responses without a size limit and publishes bytes without image validation.
By contrast, the feed enforces a byte limit, dimensions, and JPEG decoding.
`store_images` publishes sequentially, so a later failure leaves earlier files
installed despite returning an error for the request.

Large or invalid input can consume memory/disk, and retrying a partially failed
batch creates duplicates. Define and enforce byte/decoding limits and report
partial success, or validate the batch before publishing it. Do not claim the
RPC publishes only valid images while it merely copies bytes.

### 15. P2: An earlier upload completion overwrites a newer selection's UI

**Resolved:** Upload success and failure handlers capture the selection generation and ignore stale completions after the participant selects a different photo.

**Evidence:** `web/foto.php` guards asynchronous photo preparation with
`currentSelection`, but its upload click handler does not capture/check that
generation and does not disable the photo picker.

Select photo B while photo A uploads: A's completion can display "Your photo
was sent!" while B is shown and has not been sent. Guard upload completion
against selection changes or prevent changes while an upload is active.

## Media preparation

### 16. P1: Media tools silently overwrite existing outputs

**Resolved:** Media renderers reject existing outputs and duplicate stems, preflight archive collisions, and publish completed output from temporary files without replacement. Tests cover failed renders and concurrent destination creation.

**Evidence:** `scripts/reencode_videos.py:reencode_files` maps every input to
`output_directory / (video.stem + '.mp4')` and its FFmpeg command uses `-y`.
Two inputs with the same stem overwrite one another. `scripts/auto_tester.py`
also uses `-y` before checking the archive destination in `move_original`;
`scripts/loop_tester.py:accept_loop` checks the archive but not the loop output.

Preflight output collisions and archive destinations before rendering/moving.
Require explicit overwrite intent and publish finished media atomically so a
failed render does not replace a previously usable output.

### 17. P2: Saved render plans lack timeline invariants

**Resolved:** Saved plans require finite positive durations, nonempty scenes, exactly one transition per adjacent pair, sufficient timeline length, valid overlap sums, and title events within the output duration.

**Evidence:** `scripts/render.py:RenderPlan` validates field types but permits
empty scenes, negative durations, and any transition count. `render_plan_file`
validates only `plan.render`; `filter_graph` indexes the first scene and later
scenes from transition indices.

A syntactically valid edited TOML plan can produce an `IndexError`, omit scenes
from the connected graph, or generate invalid filter arguments. Validate finite
positive scene durations, exactly one transition per adjacent scene pair, and
valid overlap/title bounds before rendering. Also validate
`start_black_duration`, which `validate_config` currently omits.

### 18. P3: Long renders build one growing graph and repeatedly scan the plan

**Resolved:** Planning maintains an incremental duration. A one-hour plan of ten-second clips produced 720 scenes in about 2 ms; two hours took about 4 ms. The one-hour graph was 179,200 bytes, so execution now passes the graph via a temporary file. Decoder/memory limits have not been measured; no speculative segmented renderer was added.

**Evidence:** `scripts/render.py:build_plan` recomputes `timeline_duration` and
calls `stretch_scenes_for_transitions` across all accumulated scenes on every
iteration, making planning quadratic in scene count. `ffmpeg_command` adds a
separate input for every scene and title event, including repeated media.

Long plans made from short clips grow command length, input count, and filter
resources. Measure a representative hour-long plan before choosing a remedy;
maintain an incremental timeline total and consider bounded render segments
if actual decoder/memory or command-length limits are reached.

### 19. P2: "Randomized cycles" is not the renderer's selection behavior

**Resolved:** Documentation now specifies random choice without immediate repeats, not shuffled cycles. The automatic loop tool describes differing sampled endpoints as a heuristic, not proof of a non-loop.

**Evidence:** README and `doc/streamo.md` describe randomized cycles.
`scripts/render.py:choose_media` samples randomly from all media except the
immediate predecessor; it does not consume a shuffled cycle.

An A/B/A/B sequence can repeatedly omit C. Decide whether variety means a
complete shuffled cycle or merely no immediate repeats, then align code and
documentation. The auto-loop tool similarly calls its endpoint heuristic
"definitely non-loop", although it samples 0.25 seconds before the end rather
than the true final frame. Use wording that reflects that heuristic.

## Structure, naming, and documentation

### 20. P3: Three large modules combine independently changing concerns

**Resolved:** Provider configuration moved to `provider_config.py`, adapters and ingest output to `providers.py`, live composition to `composition.py`, audio to `audio.py`, render planning to `scripts/render_plan.py`, and Markdown cards to `scripts/title_card.py`. Imports use the defining modules; no compatibility wrappers remain.

- `streamo/services.py` (846 lines): encoding and ingest models, validation,
  provider models, adapters, output serialization, and capability catalogs.
- `streamo/streamer.py` (636 lines): process lifecycle, HDMI management, filter
  generation, and audio capture/status calculations.
- `scripts/render.py` (666 lines): CLI, Markdown rendering, media probing,
  timeline planning, TOML serialization, and FFmpeg generation.

These boundaries make focused fixes and tests harder to inspect. Split along
those existing responsibilities when touching them, without inventing a
generic framework. The tracked package and test directories have about 14 and
17 Python files respectively; entry count alone does not justify subdivision.

### 21. P3: Names conceal scope or imply unsupported behavior

**Resolved:** Renamed `programs.py` to `ffmpeg_progress.py`, and the media tools to `auto_loop.py` and `preview_loops.py`. Their help states rendering/moving behavior. Channel and category descriptions distinguish audio and provider meanings. Provider capabilities now advertise implemented commands/health only. Public prose uses streamO, reccy, and showCo.

**Evidence and proposed direction:**

- `services.py` means streaming providers, while `service.toml` and `daemon.py`
  mean an operating-system service. Prefer a provider/ingest-specific module
  name if that module is reorganized.
- `programs.py` contains only FFmpeg bitrate parsing. A name tied to FFmpeg
  progress would make it discoverable.
- `auto_tester.py` and `loop_tester.py` are user tools that render and move
  media, not automated tests. Their CLI descriptions and names should expose
  those side effects. `ignored()` also sometimes means "move this file".
- `channel` selects the first channel of a stereo pair, while Kick's nested
  `channel` means a provider slug. Help should state both meanings explicitly.
- `category` means a name for Twitch but a numeric ID for YouTube/Kick.
  Provider-specific help/schema descriptions should make the distinction clear.
- `ServiceCapability.FINISH` is advertised for YouTube, but its adapter
  inherits the generic no-op `finish()`. Several other capability enum members
  have no public command mapping. Define lifecycle capabilities separately from
  callable commands and avoid promising operations the adapter does not do.
- Public prose and `service.toml` use `Streamo`/`Reccy`/`Showco`; the intended
  product names are streamO, reccy, and showCo. Keep executable names and paths
  unchanged when correcting prose.

### 22. P3: Documentation is duplicated and already disagrees with the backlog

**Resolved:** README is a short entry point to the authoritative operator guide. Handover links this review. Help exposes authorization and is recorded using reccy’s CLI-help fixture, including media tools. No pre-existing help-only tests were redundant; behavior tests remain.

**Evidence:** README is 547 lines and `doc/streamo.md` is another 320-line
operator guide with overlapping setup, configuration, OAuth, overlays, and
media-preparation instructions. `doc/handover.md` still says no source issues
are known. Top-level CLI routing exposes `auth`, but default help delegates to
the daemon parser and does not enumerate that route.

Choose one authoritative operator guide and use a short README entry point.
Link the issue backlog from handover, make authorization discoverable from
top-level help, and record help through reccy's CLI-help regression facility.
Preserve focused behavior tests: help snapshots do not validate process
cleanup, actual output routing, or hardware behavior.
