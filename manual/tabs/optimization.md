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
- **Select mesh** takes whatever is selected in Blender right now, for when you
  change selection while the tool is already open.
- **Your original is kept and hidden**, unless you choose to replace it.
- **Preserve rig data** brings the rig with it — see below.
- **Blender stays usable while it runs** — the remeshing happens outside
  Blender's main thread. Big meshes take minutes and take them in the background.
- **You can stop it.** Cancel ends the run cleanly, leaving your scene exactly as
  it was.
- **It tells you the real size of the job first** — the count shown is what the
  engine actually receives, with your modifiers applied, not the face count in
  the file, which can be a hundred times smaller.

### Preserve rig data

A retopologised character with its weights gone is a curiosity, not a tool. Tick
**Preserve rig data** and the result comes back wearing the original's rig:

- its **deform modifiers**, Armature included, pointing at the same armature
- its **vertex groups**, resampled onto the new topology
- its **materials**, with every face on the slot it should be on
- its **constraints** and **custom properties**

**Shape keys are baked in, not carried.** The result is the shape you were
looking at when you pressed the button — so set the frame you want first. The
new mesh has no shape keys of its own, exactly the way Quad Remesher works.

The **pose**, on the other hand, is not baked: the deform modifiers are switched
off for the read and put back afterwards, so a posed character comes back at rest
and its armature drives it properly rather than the pose being applied twice.

Before you run, the panel counts what is there to carry — *"1 deform modifier,
170 vertex groups, 775 shape keys"* — and afterwards it reports what really
landed, including any modifier it deliberately left behind.

!!! warning "Weights are resampled, so they are close, not exact"
    The new mesh has entirely different vertices, so weights have to be sampled
    onto it rather than copied across. On a character, expect to want a weight
    cleanup pass. A modifier that was already applied to the mesh the
    engine saw — a Subdivision, say — is deliberately **not** copied, because
    that would apply it twice; the report names it.

!!! warning "UV maps are not carried over"
    A retopologised mesh normally wants unwrapping again anyway. The tool says
    so in the panel rather than staying quiet about it.

### Fix concave faces

On by default, under **Clean up**. Blender's **Surface Deform** refuses to bind
to a mesh that contains a concave face, and a quad remesh produces a few every
time around the points where edge loops merge. This splits them, so the result
can be used to drive another mesh.

It costs a handful of quads — splitting a concave quad leaves two triangles —
and the report tells you exactly how many. On a real character it was 2 faces
out of 6,373.

!!! tip "Leave it on unless you need a strictly all-quad mesh"
    If the result is going to drive anything with a Surface Deform, you need
    this. If you are retopologising purely for clean topology to sculpt on, you
    can turn it off.

## Bake to shape keys

Sits directly under Quadify, because it is the second half of the same job.

If a mesh is being moved by a modifier, this **freezes that motion into one
shape key per frame** over the scene's frame range and switches the modifier
off. Shape keys are evaluated *before* modifiers, so the mesh still moves
exactly as it did — but the whole modifier stack is now free, which is what you
need to put **Cloth** on it.

- **It uses Blender's own frame range**, shown in the panel, so there is
  nothing here that can disagree with your scene.
- **It tells you the cost before you press it** — how many keys, and roughly
  how much memory, because each key holds a full copy of the mesh. Bake every
  2nd or 5th frame if that number is uncomfortable; the frames in between
  become a blend of their neighbours.
- **The modifiers are switched off, not deleted.** Turn them back on and bake
  again with a different range whenever you like. There is a tickbox if you
  want them gone for good.

### The cage workflow

This is what the two tools are for together:

1. Move to the frame you want and **Quadify** the character, with **Preserve
   rig data OFF**. The result is clean geometry matching exactly what you see.
2. Add a **Surface Deform** to the result, target the original, and **Bind**.
   The cage now follows the original perfectly — armature, morphs and all —
   because it is reading the finished surface rather than a copy of the parts.
3. **Bake to shape keys**, which frees the stack.
4. Add **Cloth**.

!!! tip "Nothing is transferred, and that is why it works"
    Copying weights and morphs onto new topology goes wrong where a body folds
    back on itself — a point just outside a fold is physically nearest to the
    surface facing it, not the one it belongs to. Letting the cage read the
    finished surface avoids the question entirely. On a real character this
    tracked the original with a fixed 0.4 % gap that did not drift, and no
    stretched faces at all.

!!! warning "It refuses if a modifier changes the vertex count"
    A shape key can only hold different *positions* for the same vertices, so a
    Subdivision or Mirror in the stack cannot be baked this way. Apply it first,
    or move it below the modifier you are baking.

### Delete all shape keys

Under **Shape keys on this mesh**, with the count beside it. It removes every
key on the mesh, Basis included, along with the keyframes driving them.

This is the way back out of a bake you are not happy with — a range that was
too short, or every 5th frame when you wanted every frame. Delete the keys,
switch the modifier back on, and bake again. Baking a second time *without*
deleting stacks a new set of keys on top of the old ones.

- **It asks first**, naming the mesh and the number of keys.
- **It stays available on a just-baked mesh.** The bake switches the modifiers
  off, so there is nothing left to bake — but that is exactly when you are most
  likely to want the keys gone.
- **Object mode only.** Blender refuses to remove shape keys in edit mode and
  so does this.

!!! warning "The mesh returns to its base shape"
    Basis goes with the rest, so the mesh snaps back to the shape it had before
    it had any shape keys at all. After a bake that is the shape you
    retopologised — nothing moves. On a mesh whose Basis you sculpted, it is a
    visible jump.

!!! warning "Shape keys belong to the mesh, not the object"
    If two objects share the same mesh, both lose their keys. The panel says so
    before you press it, and again in the confirmation.

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
