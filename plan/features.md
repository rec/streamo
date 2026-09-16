# Remaining features for streamO

Implementation has been requested in the order below. Completed features have
been removed; original numbering is retained. Unresolved architectural choices
still require review. Effort is a rough estimate, not a delivery promise.

## Implementation order

| Priority | Feature | Operator benefit | Effort |
| --- | --- | --- | --- |
| 6 | Audio/video alignment | Correct a measured timing offset | Medium |
| 7 | Repeatable rehearsal | Test a show without its venue equipment | Medium |
| 8 | Visual-bed planning preview | Review selection and timing before a long render | Medium |

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

Implementation stays within streamO.
Shared transport or service-lifecycle work belongs in reccy if needed, with
coordination before touching that repository. Operator UI and cross-application
cue scheduling belong in showCo. Recording and post-production should build on
recs rather than introducing another general recording subsystem here.

The existing [operator guide](../doc/streamo.md) describes implemented behavior.
The features listed here are not yet implemented.

## Additional work beyond the prompt

None. Implementation stays within the requested features and this repository.
