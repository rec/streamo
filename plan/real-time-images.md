# Real-time participant images

## Goal

Allow Streamo to discover and display participant images added after streaming
has started without restarting FFmpeg or interrupting the Twitch connection.

Every valid image currently in `image_dir` must be shown once per cycle. No
image may repeat until every other image in that cycle has been shown. Images
first discovered after playback starts have priority over the existing shuffled
queue and appear in the next available image slots.

## Behavior

- Keep `image_interval`, `image_duration`, and `image_fade` as the timing
  controls. An interval of zero disables participant images.
- Remove `image_chance`. When participant images are enabled and at least one
  valid image exists, every interval gets an image.
- Scan `image_dir` before selecting an image for each interval.
- Treat supported regular files not previously observed by this process as new.
  Shuffle a batch of simultaneously discovered images, but place the entire
  batch ahead of images already waiting in the current cycle.
- Do not interrupt an image already visible. A newly discovered image receives
  priority beginning with the next interval.
- Track images already shown and images still queued. Once all currently known
  images have been shown, shuffle all current images to begin a new cycle.
- Remove deleted paths from the queue and cycle state. A path recreated after
  deletion is new when it is observed again.
- If an image cannot be decoded, log the failure, count it as attempted for that
  cycle, and continue to another image rather than stopping the stream.
- Publish files received by the `image` RPC atomically so the playback scanner
  cannot observe a partially copied or downloaded image.

## Runtime design

Replace the fixed participant-image FFmpeg inputs with one permanent raw RGBA
video input. Streamo will create an OS pipe before starting FFmpeg and pass the
read descriptor as a numbered `pipe:` input. The existing audio stream remains
on standard input.

A producer thread will write one full-size RGBA frame for each configured video
frame. It will:

1. Ask the image scheduler for the next image at each interval boundary.
2. Load and scale that image once, preserving its aspect ratio and centering it
   on a transparent canvas matching `video_resolution`.
3. Apply `image_fade` by scaling the source alpha channel for each visible
   frame.
4. Write transparent frames for the remainder of the interval or whenever no
   valid image is available.
5. Exit cleanly when FFmpeg closes the pipe.

FFmpeg will overlay this continuously changing RGBA stream after the existing
title-card overlay. Its filter graph and output connection remain fixed for the
life of the stream.

Keep scheduling and frame production separate from process startup. The
scheduler should accept a `random.Random` instance so tests can make shuffle
order deterministic without changing production behavior.

## Local preview

Add `preview` to the existing daemon action. It will run the same audio capture,
controller, RPC server, image scheduler, frame producer, FFmpeg inputs, codecs,
and filter graph as normal streaming. The only output difference is that FFmpeg
will write a NUT stream to standard output and Streamo will feed that stream to
`ffplay` instead of sending FLV to Twitch.

On the target Mac, run:

```bash
uv run streamo daemon preview --config ~/.config/streamo/config.toml
```

The `ffplay` window should show the complete outgoing composition. While it is
running, copy image files into the configured `image_dir` or send the existing
`image` RPC. Newly added images should appear in the next slots without either
process restarting. Closing the preview or sending `stop` should terminate both
FFmpeg and ffplay.

Document this workflow in `README.md`. Preview still uses the configured audio
device, but it does not connect to the configured streaming service.

## Implementation steps

1. Add an image scheduler and RGBA frame producer, with directory rescanning,
   shuffle-cycle state, new-image priority, scaling, and alpha fades.
2. Make RPC image publication atomic.
3. Replace the fixed random image input and looping filter graph with the live
   raw-video pipe while retaining the title-card behavior.
4. Add preview process setup and teardown, the `preview` daemon action, and the
   local verification documentation.
5. Remove `image_chance` and its obsolete validation and tests.

## Verification

- Unit-test that each image appears exactly once before any repeat.
- Unit-test that images discovered during a cycle are selected before queued
  older images.
- Unit-test deletion and re-creation behavior.
- Unit-test scaling, transparent idle frames, and fade alpha values using small
  image fixtures.
- Test that the FFmpeg command consumes the numbered RGBA pipe and overlays it
  after the title card.
- Test that preview selects NUT on standard output and the normal path remains
  FLV to the configured Twitch URL.
- Test that the daemon dispatches `preview` without changing service-install
  arguments.
- Run the full pytest, Ruff, formatting, type-checking, pyupgrade, and diff-check
  workflow.
- On the target Mac, perform the preview command above and add enough images to
  observe a complete cycle, a no-repeat boundary, and new-image priority.

The final manual preview is the runtime acceptance test. Automated command and
frame tests do not prove that the installed FFmpeg build, ffplay display, audio
device, and live timing work together on the target machine.
