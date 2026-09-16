# Feature suggestions for streamO

Implementation has been requested in the order below. Completed work is marked
in each section; unresolved architectural choices still require review.
They build on the current stereo capture, visual composition, participant-image
feed, local preview, and RPC control. No proposal requires another streaming
provider. Priorities reflect usefulness during a show; effort is a rough relative
estimate, not a delivery promise.

## Suggested order

| Priority | Feature | Operator benefit | Effort |
| --- | --- | --- | --- |
| 1 | Preflight report | Find setup problems before going live | Medium |
| 2 | Recover publishing after failure | Resume a broadcast without manual restart | Large |
| 3 | Actionable health and incident history | Tell the controller what failed and whether it recovered | Medium |
| 4 | Live titles and an intermission slate | Change the on-screen message during a show | Large |
| 5 | Participant-image moderation and control | Decide what appears and when | Medium |
| 6 | Audio/video alignment | Correct a measured timing offset | Medium |
| 7 | Repeatable rehearsal | Test a show without its venue equipment | Medium |
| 8 | Visual-bed planning preview | Review selection and timing before a long render | Medium |

Start with preflight. Design publishing recovery and incident reporting together:
keeping the process alive is useful only if the controlling application can see
the actual state. Then choose live titles or image moderation according to the
next show's needs.

## 1. Preflight report

**Implemented:** `streamo preflight` and the `preflight` RPC return a structured
readiness report. Device-opening and supported provider-health probes require
explicit CLI options. See [the operator guide](../doc/streamo.md#service-operation)
for behavior and limits. Ten focused tests cover reporting and probe boundaries.

**Problem:** configuration validation catches structural errors, but an operator
still has to discover device, encoder, media, and permission problems piecemeal.

**First version:** provide one explicitly invoked check that reports the selected
input device and stereo pair, audio-format support, required FFmpeg encoders,
media properties, writable image storage, and local-display prerequisites.
Separate failures, warnings, and checks that were not performed. Return the same
structured report to a CLI and a controlling application.

The normal check must not publish, update provider metadata, or start a daemon.
Device-opening tests and remote credential checks need explicit probe options;
the report should explain what those probes do. A successful preflight is not
proof that the destination is receiving a broadcast.

**Completion example:** a configuration selecting channels 17–18 on a device
with only two inputs names that mismatch before streaming starts. An unavailable
device is distinguished from an unsupported sample rate.

## 2. Recover publishing after failure

**Implemented following user approval.** `recover_publish` enables session-owned
encoder recovery. Tests cover cleanup, stop during backoff, launch errors,
progress stability, mute preservation, and fresh audio after reconnection.
Approved implementation boundary:

- streamO owns encoder recovery inside one running session. The operating-system
  service manager only restarts the whole application if that application exits;
  encoder failure alone does not exit it when recovery is enabled.
- Recovery is opt-in. After successful provider preparation, an unexpected
  FFmpeg exit, including a zero exit code, starts recovery while the operator's
  intent is still to publish. Explicit stop and preview-window closure never
  restart publishing. Preview keeps its current finite lifecycle.
- Retry indefinitely with delays of 1, 2, 5, 10, then 30 seconds, capped at
  30 seconds. Reset the delay only after 60 seconds of output progress, so a
  process that repeatedly starts and dies does not create a rapid restart loop.
- Session resources are the RPC endpoint, control state, capture, image feed,
  image-selection history, provider adapter, and prepared provider destination.
  Attempt resources are FFmpeg, its pipes, stderr reader, and frame writer.
  Replacing an attempt must not reset mute state, counters, or image history.
- Capture continues while the encoder is unavailable, discarding samples that
  cannot be delivered. Reattaching a pipe starts on an audio-frame boundary;
  stale audio is not replayed after reconnection. Dropped frames remain visible.
- Prepare the provider once per session. Reuse that destination when reconnecting
  instead of repeating metadata updates or creating a remote broadcast. Call
  final cleanup once when the session ends. Remote auto-stop can end a broadcast
  during a disconnect; this feature must report that limitation and must not
  silently change auto-stop settings or claim that an ended broadcast resumed.
- Expose requested publishing state, actual encoder state, cumulative attempt
  count, next retry time, and the latest sanitized failure. Receiving FFmpeg
  progress marks local encoding as resumed; provider delivery remains a separate
  health observation. Feature 3 will retain incident/recovery history.
- Configuration, local-media, and initial authorization failures retain their
  existing failure behavior. This first version retries an already prepared
  publishing session; it does not repeatedly replay failed setup operations.
  Child-launch errors need classification: unavailable binaries are terminal;
  temporary process-resource errors use the same delayed retry policy.
- Stop interrupts both the active attempt and the retry wait immediately, then
  closes session resources. Tests use fake processes and a controlled clock to
  verify attempt cleanup, stop during backoff, preserved state, and secret-free
  failure reporting without contacting a provider.

Provider reconnection and remote auto-stop behavior still need live validation.

**Problem:** audio capture retries and the HDMI player recovers, but an FFmpeg
exit still ends the streaming run.

**First version:** let the operator opt into restarting the publishing process
after an unexpected exit. Keep the control endpoint available, preserve mute
state and participant-image history, and expose the failure, attempt count,
next retry time, and recovery result. Space out repeated attempts. An explicit
stop must cancel recovery immediately.

Do not silently substitute another destination or revive rejected backup-ingest
settings. Before implementation, decide which failures are retryable, how a
provider session survives reconnection, and which component owns restarting the
process so daemon supervision and streamO do not compete. This requires a
reviewed lifecycle design; a retry loop around the existing entry point is not
enough. Recovery may introduce a visible gap and cannot promise continuity.

**Completion example:** after a simulated encoder exit, the controller remains
responsive, observes recovery attempts, and sees successful resumption. Pressing
stop while waiting prevents any subsequent restart.

## 3. Actionable health and incident history

**Implemented:** the `incidents` RPC provides a bounded, sequenced history with
explicit cursor-expiry reporting. Status exposes active warnings. Configurable
silence, clipping, and output-stall warnings run alongside provider-health
freshness checks. Tests cover warning/recovery transitions, mute suppression,
expired history, and repeated polls. History is process-local, not persistent.

**Problem:** current status exposes counters and recent errors, but an operator
cannot easily distinguish an old incident from an ongoing failure or reconstruct
what happened between status polls.

**First version:** expose a bounded history of incident and recovery events with
sequence numbers, timestamps, affected component, and a concise explanation.
Clients can request events since their last sequence number and are told when
older events have expired. Build on existing audio errors, dropped frames,
FFmpeg progress, and provider-health freshness.

Add configurable warnings for sustained silence, clipping, and missing output
progress. Muted audio must not trigger a silence warning; naturally quiet shows
need to be able to disable it. Avoid equating local encoder progress with
confirmed delivery to viewers. Keep secrets out of incident details.

**Completion example:** showCo can display that audio stalled, how many frames
were dropped, and when capture recovered, even if it missed the original status
change. Repeated polls do not create duplicate incidents.

## 4. Live titles and an intermission slate

**Next step: composition design approval.** Proposed implementation:

- Replace the startup-only title loop and separate participant-image overlay
  with one session-owned live compositor feeding a persistent RGBA input to
  FFmpeg. Video streams keep this input attached even when it is transparent,
  so a cue changes frame content without restarting or rebuilding the encoder.
- Compose a full output-sized canvas at `video_frame_rate`. Prepare title/photo
  assets using the existing working resolution, preserving their centering,
  aspect ratio, and displayed size. The visual bed retains its encoded resolution
  and frame rate. A slate fills the whole canvas with an opaque background.
- Keep configured `title_card` images and their automatic schedule. Add a `title`
  RPC with optional replacement plain text and a required visibility of `auto`,
  `show`, or `hide`. `show` holds the title until another cue; `auto` returns it
  to its configured schedule. Text rendering happens before accepting the cue;
  a failed update leaves the current title intact.
- Add a `slate` RPC with required `visible` and optional replacement plain text.
  The initial text is “Intermission.” A visible slate covers the base video,
  pauses photo rotation and automatic title timing, and hides those overlays.
  Hiding it resumes them. Neither title nor slate commands change audio mute.
- Track requested and applied visual revisions in status. Applied means that
  a complete frame containing the change reached the encoder pipe, not that a
  remote viewer has seen it. During recovery, retain the latest requested cue
  and apply it to the new encoder. Reject visual commands for audio-only streams.
- Keep cue timing and operator UI in showCo. This implementation adds streamO
  rendering and RPC controls only, with no changes to sibling repositories.
- The full-sized RGBA pipe costs more bandwidth than today's small photo plane.
  Keep frame production bounded, verify layout with small image/video fixtures,
  and measure target-machine performance before claiming Pi show readiness.

This replaces the composition mechanism rather than adding a second title path.
No live-compositor implementation has been made yet.

**Problem:** the current title card and visual bed are configured before startup.
Changing a performer name or putting up an intermission message requires more
than an ordinary control command.

**First version:** allow the controller to update a title, show it immediately,
hide it, or select an intermission slate while publishing continues. Keep the
initial design to a title and a slate rather than a general scene editor.
Make audio behavior explicit: showing a slate must not silently mute or unmute
the show. Report both the requested and applied visual state.

This needs a composition design review because the current FFmpeg graph is
built at startup. Validate replacement assets before applying them; a bad title
must leave the current picture intact. showCo should own cue timing, while
streamO owns applying the visual change.

**Completion example:** an operator changes the performer name, shows it for an
introduction, then hides it without restarting the encoder or interrupting audio.

## 5. Participant-image moderation and control

**Problem:** new images automatically enter rotation, and removing the newest
file does not necessarily remove the image currently on screen.

**First version:** offer an optional approval queue with previews and explicit
approve/reject actions. Add controls to skip the visible photo, temporarily pause
rotation, and choose a specific approved photo to show next. Report the visible
image and upcoming selection so the controller can present accurate controls.

Keep automatic acceptance available for trusted rooms. Moderation decisions
must survive restart and must not cause rejected feed entries to download again.
Choose stable image identifiers and define whether skipping is temporary or
excludes an image from future rotation before defining the RPC commands.

**Completion example:** a participant upload stays off screen until approved.
An operator can remove an unsuitable visible photo immediately without deleting
an unrelated, more recently uploaded image.

## 6. Audio/video alignment

**Problem:** capture and visual sources can have different delays, but there is
no explicit operator control for correcting a measured offset.

**First version:** support a startup setting with an unambiguous sign convention,
such as “delay audio by 150 ms.” State the supported range and latency cost.
Use the same alignment in preview and published output. Keep live adjustment
out of the first version unless a show requires it.

Provide a flash-and-click calibration fixture so operators can measure the
result. Account for local-display and destination playback latency separately;
one fixed offset cannot remove variable network delay.

**Completion example:** a fixture with a known offset produces aligned events
after correction, and help makes it clear which signal the setting delays.

## 7. Repeatable rehearsal

**Problem:** local preview avoids provider authorization, but still needs an
audio device and may poll the configured participant-image feed.

**First version:** add an explicit rehearsal mode using a supplied audio file
and local image folder, with provider and remote image-feed access disabled.
Allow a seed for repeatable image selection. Exercise the production composition
and control paths rather than creating a separate mock renderer.

Keep rehearsal visibly identified in status so a controller cannot mistake it
for a live broadcast. Define what happens at the end of the audio file. A small
fixture should be sufficient; do not bundle a large media collection.

**Completion example:** a saved rehearsal reproduces the same title and photo
sequence on a laptop without the venue's mixer, credentials, or network access.

## 8. Visual-bed planning preview

**Problem:** a saved render plan is reproducible, but TOML and a textual schedule
are awkward ways to judge visual variety before committing to a long render.

**First version:** generate a contact sheet and timeline from the saved plan,
showing source names, scene lengths, crossfades, title appearances, and total
screen time per source. Highlight inputs that never appear. Generate a short,
low-resolution preview from that same plan when requested.

An optional complete-cycle selection policy could follow if operators want
guaranteed coverage. Keep the current random selection clearly named and make
the choice explicit when generating a plan. Do not change a saved plan's scene
order when previewing or rendering it.

**Completion example:** an operator notices that a source is absent or a title
overlaps an important scene, edits the plan, and checks the result before the
full render. Large-render capacity remains a separate measurement task.

## Ownership and limits

These suggestions change streamO only when explicitly selected for implementation.
Shared transport or service-lifecycle work belongs in reccy if needed, with
coordination before touching that repository. Operator UI and cross-application
cue scheduling belong in showCo. Recording and post-production should build on
recs rather than introducing another general recording subsystem here.

The existing [operator guide](../doc/streamo.md) describes implemented behavior.
Only features marked implemented should be advertised as available.

## Additional work beyond the prompt

None. Implementation stays within the requested features and this repository.
