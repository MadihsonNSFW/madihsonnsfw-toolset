# Optimization

Make a heavy scene fit in memory before you render it, remesh geometry, and find
out what is making your `.blend` so big.

!!! success "No original file is ever modified"
    Every texture the tool shrinks is a copy. One click puts everything back.

---

## Fixed size

Shrink chosen textures to a size you pick.

Every run is remembered as a named **texture set**, so one scene can sit at one
resolution and another at another; switch between them from the list.

Queue several jobs with **Add to queue** — each becomes its own set, with its own
objects and its own name. Double-click a queue row to rename it.

!!! warning "A queued SELECTED job resolves its targets when it runs"
    Not when you queue it. If the selection changes in between, the job acts on
    the new one.

## Adaptive

The interesting one. Each texture is shrunk to the size it **actually needs**,
measured from how large its object lands in the render camera.

A wall at the back of the shot does not need a 4K map.

## Meshes

A managed Decimate on distant meshes, with the ratio driven by camera distance.

## Quadify

Pick a mesh, press **Retopologize**, and get all-quad topology back — the kind
you can animate, subdivide and sculpt on.

- **It shows you what it made**: faces before and after, how many are quads, how
  long it took, and it names anything that is not a quad rather than quietly
  rounding to "done".
- **Density, sharp edges and symmetry** — set how big the quads should be, let
  it find hard edges by angle, and mirror on X, Y or Z. The half you keep is
  remeshed and a Mirror modifier rebuilds the rest.
- **Your original is kept and hidden**, unless you choose to replace it.
- **Blender stays usable while it runs** — the remeshing happens outside
  Blender's main thread. Big meshes take minutes and take them in the background.
- **You can stop it.** Cancel ends the run cleanly, leaving your scene exactly as
  it was.
- **It tells you the real size of the job first** — the count shown is what the
  engine actually receives, with your modifiers applied, not the face count in
  the file, which can be a hundred times smaller.

!!! warning "UVs, vertex groups and materials are not carried over yet"
    The result is clean geometry. The tool says so in the panel.

!!! tip "On a rough, organic mesh, turn off Detect under Sharp edges"
    A crusty or noisy surface reads as a hard edge almost everywhere, and the
    engine then has to fit its quads around every one — the difference between
    seconds and a very long wait.

!!! info "The remeshing engine is third-party"
    Quadify does not implement the remeshing maths. It prepares your mesh, runs
    the **QuadWild-BiMDF** programs (GPL-3.0, SIGGRAPH 2021/2023) as a separate
    process, and reads the result back. Running it out of process is also why a
    mesh the engine cannot handle can no longer take Blender down with it.

## Restore

Put every original back.

- Survives a **Save As**.
- Warns in red about a texture it genuinely cannot find.
- **Clear cache folder** restores first, then deletes only the files the Toolset
  wrote.

## Memory report

What each datablock costs, plus a **VRAM estimate**: roughly what a render needs
from the command line versus from inside Blender, and what the difference is made
of.

## File size

**What is really inside your `.blend`, biggest first.**

Every mesh, image, shape key and object **by name** — and, opening one up, what
that datablock is actually made of: shape keys, vertex groups, Surface Deform
bind data, packed images, animation curves.

- **These are exact figures, not estimates.** They are read from the file itself
  rather than guessed at from the scene, so an uncompressed `.blend` adds up to
  precisely its size on disk.
- **It sees things the Memory report cannot** — including datablocks nothing in
  your scene is using any more.
- **Blender does not need to be running.** It reads the file off disk, and
  **Choose a file…** will measure any `.blend` on your drive.
- Compressed files are read directly — no unpacking, no temporary copy. A 650 MB
  file takes a few seconds, with a progress bar, and the rest of the app stays
  usable.

!!! note "The sizes inside add up to more than the file in Explorer"
    Blender compresses `.blend` files by default. Both totals are shown side by
    side.

---

## While it runs

Everything here runs off the interface thread with a real progress bar counting
actual textures, and the cache is watched — if a set's files are cleared or moved
you are told before you switch to it.
