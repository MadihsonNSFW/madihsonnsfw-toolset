# Rendering

Three tools: a render queue, a denoising compositor builder, and render presets.

---

## Render Queue

**Renders with Blender closed.**

Queue stills, animations and playblasts across scenes and `.blend` files, watch
progress frame by frame, pause and resume, and disable whole collections per
job. It drives its own headless Blender, so the queue is unaffected by what you
do in the open one.

**Save & Queue** takes the file you have open in Blender, saves it, and adds it.
The queue renders files on disk, so this is what stops it rendering the version
you saved an hour ago. Press it again after more work and it re-uses the same
row rather than stacking duplicates.

!!! tip "A render in progress is a resumable job"
    It is written down as it goes. Closing the app or losing power costs you the
    current frame and nothing else.

Live RAM and VRAM cards sit alongside — they stop sampling while you are looking
at another tab. Your machine is kept awake for the duration.

---

## Denoising setup

**One button builds the whole compositor tree.**

The default mode gives **every light pass its own Denoise node** — Diffuse,
Glossy, Transmission and Volume, with Direct and Indirect denoised separately,
each guided by the denoising Normal and Albedo passes — then rebuilds the beauty
from the denoised parts and restores the layer's real alpha.

Colour, Emission and Environment are never denoised, because they are already
clean.

A simpler one-node-per-view-layer mode is a tick away. Multiple view layers are
handled.

**Remove Setup** puts your compositor back exactly as it was, from a snapshot
taken before the first run — and only ever deletes trees this tool made.

!!! warning "Cycles only"
    It says so plainly on EEVEE rather than building something that cannot work.

---

## Render presets

**Save a look, apply it anywhere.**

**164 settings across 15 groups**: engine and device, Cycles sampling,
denoising, light paths, EEVEE, film and motion blur, performance, resolution and
frame rate, output format, video encoding, colour management, simplify, post
processing.

When you save one you tick which groups it keeps, with the current values in
front of you.

!!! note "Output path and frame range start unticked"
    Those belong to a shot rather than to a look.

Applying tells you what changed, what already matched, and anything this Blender
refused — and one refused setting never stops the rest from landing.

Presets are plain files you can copy between machines, and **Save to Studio
Library** files one away as a library item you can tag, version and zip for a
friend.

### Order matters inside a preset

Some settings invalidate others when written in the wrong sequence, so the
engine writes them in a fixed order: `render.engine` first, `media_type` before
`file_format`, and `display_device` → `view_transform` → `look`. Written the
other way round, a perfectly good preset raises — and the failure looks like a
bad preset rather than a bad loop.

### The whitelist

A preset file is plain JSON, which means it can be edited. Applying one only
ever writes properties from the tool's own catalogue, so an edited file cannot
reach anything else in your scene.
