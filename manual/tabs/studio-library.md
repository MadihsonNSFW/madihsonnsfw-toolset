# Studio Library

A shared library for anything you reuse, browsable as a grid of thumbnails.

## The ten item types

All saved and applied the same way.

| Type | What it holds |
|---|---|
| **Pose** | A pose on the selected bones, or the whole rig |
| **Animation** | A frame range, with options — see below |
| **Selection set** | A named set of bones, re-selected in one click |
| **Mirror table** | For moving animation between sides |
| **Bone remap** | For moving animation between rigs |
| **Shape keys** | Exactly which keys you pick, with filters |
| **Alembic cache** | An `.abc` export with the full option set |
| **Vertex groups** | Chosen groups, or one item per group in a single pass |
| **Bone picker layout** | See [Bone picker](bone-picker.md) |
| **Render preset** | See [Rendering](rendering.md) |

### Saving an animation

The options dialog carries:

- **Frame range** — pre-filled from your scene
- **Bake every frame**
- **Keep F-curve modifiers** so Noise and Cycles survive the round trip
- **Inherit every bone property** so IK/FK and space switches come back set the
  way they were

### Saving shape keys

Pick exactly which keys to save from a searchable checklist. Driven keys are
excluded by default — which matters on a DAZ figure, where nearly every key is
driven.

### Saving an Alembic cache

The dialog carries Blender's full Alembic option set: frame range, selected
only, flatten hierarchy, instancing, UVs, normals, colour attributes, generated
coordinates, face sets, custom properties, curves as mesh, subdivision, scale,
triangulation, hair and particles, render-versus-viewport evaluation, and
sub-frame sampling. What was actually used is written into the item.

---

## Browsing and organising

**Thumbnails** are a viewport capture, a playblast, or — for vertex groups — an
automatic weight-paint render. Bone picker layouts draw their actual buttons
onto the reference picture.

- Hover a multi-item tile and it plays through what it holds. Multi-item items
  carry a stack badge and a count; animation tiles carry small marks for
  baked / kept F-modifiers / stored bone properties.
- Folders nest as deep as you like.
- Tags, colour labels, a search box and a type filter run down the left side.
  **Press and drag down the filter list** to tick or untick a run of them in one
  gesture.
- With nothing selected, the details panel gets out of the way instead of
  showing you a column of dashes.

### Versions

Overwriting an item keeps the old one. Nothing is lost.

### Zip for sharing

Select any number of items, right-click, and get an archive that unzips straight
back into someone else's library.

### Import

Hand it a zip, a folder, loose items, or a pile of `.abc` / `.mp4` files. It
shows you what it found before copying anything, recreates folder structure,
brings thumbnails, previews, tags, colours and versions with it, and **never
overwrites** — a name already taken gets a number.

Works with Blender closed.

### Previews

Captured with overlays **off** — no bones, wires, gizmos or grid floor in your
thumbnails — and your viewport is put back exactly as it was.

### Playblasts

Record one from the tab, in the background if you like, defaulting to your
scene's own output folder and the active camera. **▶** plays the newest one;
there is a matching **Watch last render** button in Blender's own panel.

---

## Performance

The grid is built to stay responsive at library scale.

- A search filter **hides** rows rather than rebuilding the grid, so typing
  costs a hide-walk rather than a full rebuild.
- Thumbnails decode lazily, in slices, so opening a large folder does not stall
  on disk.
- Decoded images and their compressed bytes are cached under byte budgets, so
  zooming a folder you have already looked at reads nothing from disk.

!!! note "The item count is the library size"
    Because filtering hides rows rather than removing them, the grid's item
    count is always the size of the library — not the number of tiles you can
    currently see.
