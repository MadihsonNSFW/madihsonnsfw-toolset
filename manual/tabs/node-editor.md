# Node Editor

A Blender-style node canvas for **texture baking**, driving Blender's own bake
operator.

---

## The six nodes

### Bake

Pick any material in your open scene from a searchable list — type any part of a
name, Enter takes the best match. A **Bake all slots** tickbox bakes every
material slot of that object in one run.

### Bulk bake

Bake many meshes at once.

**Selected to bake queue** takes everything selected in the viewport and queues
every material slot of every mesh. Anything that cannot bake — lights, cameras,
meshes with no material or no UVs — is ignored and counted.

Or switch to folder mode and point it at a collection.

### Collection

Every mesh inside a collection, all materials, all slots.

### Map set

Tick the maps you want and one press bakes a whole PBR set, each saved as
`<material>_<type>_baked`.

### Bake settings

Blender's Bake panel, option for option, in the panel's own order and with its
own visibility rules:

- **Type** — all twelve: Combined, Ambient Occlusion, Shadow, Position, Normal,
  UV, Roughness, Emission, Environment, Diffuse, Glossy, Transmission
- **View From**, and the per-type **Influence** block
- **Selected to Active** with the full cage family — Cage, Cage Object,
  Extrusion, Max Ray Distance
- **Target** — a file, or straight into the mesh's active Color Attribute
- **Clear Image**, **Margin** with both margin types, resolution presets, and an
  optional sample override

Everything else — samples, denoiser, render device — comes from your scene,
unchanged.

!!! note "This input takes several wires"
    A Bake node and a Collection can drive one press together. A material named
    by both is baked once.

### Output image

Where maps land, and the result shown on the canvas.

Leave the name empty and maps save as `<material>_baked` into the toolset's own
`baked` folder.

Two tickboxes:

- **Replace shader** wires the finished map back into the material it came from.
  Your shader network stays exactly where it is, just unplugged, so one Ctrl+Z
  restores it.
- **All slots** puts it into every material slot of the object.

When a material has more than one Material Output, the map goes to the one
matching the render engine your baked material was using — and if the slot has
no output for that engine, one is created.

---

## The canvas

- **Shift+A** adds a node where your cursor is; **Del** / **X** removes the
  selection.
- Wires are typed: a socket only connects to a socket of the same colour, and
  the status line says why a wire was refused.
- **Ctrl-drag** cuts wires.
- **Shift + right-drag** adds reroutes across every wire you cross, exactly like
  Blender.
- Middle-mouse pan, wheel and **Ctrl +/−** zoom, a proper dotted grid at every
  zoom level.
- A **?** on every node opens a plain-English panel explaining what it does.
- A progress bar under whichever node is working.
- Optional **remember node settings** (⚙ Library Settings) so nodes start with
  the values you last used.

---

## Two things it does that Blender's own bake does not

**A map that bakes empty tells you why** — no lights in the scene, a fully
transparent surface, or a material with more than one Material Output.

**A map whose bright areas are clipping says so**, with a note that naming the
file `.exr` keeps the real values.
