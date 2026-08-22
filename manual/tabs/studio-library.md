# Studio Library

A shared library for anything you reuse, browsable as a grid of thumbnails.

The tab has two halves, switched in the top left:

- **Items** — poses, animations, sets and the rest, saved out of your rig.
- **Assets** — Blender objects, collections, materials and node groups. This
  half **is a Blender asset library**: what you store here appears in Blender's
  own Asset Browser. See [Assets](#assets) below.

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

## Assets

Switch the toolbar to **Assets** and the library becomes a Blender asset
library. The **Folders** tree is replaced by Blender's **Catalogs**, the
**Type** filter becomes **Kind**, and the Save buttons become one
**Mark selected**.

| Kind | What comes with it |
|---|---|
| **Object** | Its materials and modifiers |
| **Collection** | Everything in it |
| **Material** | The material, with its node tree |
| **Node group** | Geometry-node and shader groups |

### Storing one

Select what you want in Blender, choose the **Kind**, give it a name, pick a
**Catalog** — or type a new one, which is added to the library's catalog file
exactly as Blender would — and press **Mark selected**.

This also marks the datablock **in your open file**, which is the point: that
is what puts it in Blender's Asset Browser. If your selection offers several
of the chosen kind, it asks which one, and stores one per press.

Blender renders previews for objects and materials but not for node groups or
collections. Those get the grid's placeholder tile, and the status bar says so.

### Using one

Double-click a tile. The dropdown beside the search box decides what that does
— the same three choices Blender's own Asset Browser offers:

| | |
|---|---|
| **Append** | A copy you own, reusing materials already in the file rather than making `Wet skin.001` |
| **Append (new copy)** | A copy of everything, even where it duplicates |
| **Link** | It stays owned by the library file; edits happen there |

A material or a node group is put **in the file, not in the scene** — pick it
from a material slot or a modifier dropdown. Objects and collections are linked
into the scene so you can see them.

The status bar reports the name that actually landed, which is not always the
one you asked for: an append that had to make a copy says so.

### What else works on them

Folders, tags, colour labels, search, versions, **Zip for sharing** and
**Import** all treat an asset as an ordinary item. Overwriting one versions the
old one, `.blend` and all.

Browsing works with Blender closed, like the rest of the tab. Storing and
applying need it.

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
