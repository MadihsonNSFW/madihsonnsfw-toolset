# Captured node-group specs

Machine-readable captures of geometry node groups, so they can be **rebuilt from
code** instead of shipped as a `.blend` anyone can open.

⚠ **These JSON files ARE the thing being protected.** They are plain text here
because this is Marty's own machine. Nothing in this folder may ship readable —
where the spec finally lives (compiled into the licensed exe, or downloaded from
the licence server on unlock) is an open decision, see the session notes.

## `madi_affector_deform.v1.json`
The **MADI Affector Deform** donut rig — colliders in an `Affectors` collection
dent and bulge a torus. Background, architecture and every settled decision:
`..\..\AFFECTOR_DEFORM_HANDOFF.md`.

| | |
|---|---|
| Captured | 2026-08-03, from `T:\Blender Work 2026\test\wrap to d geonode_06.blend` |
| Blender | 5.2.0 LTS |
| Size | 97 nodes, 142 links, 10 interface items, 74.8 KB |
| Exposed inputs | Affectors (Collection) · Thickness · Bulge Width · Smooth (int) · Keep Width · Lock Position · Subdivide (int) · Affector Expand |

**This is the LIVE 97/142 build, captured as-is on Marty's instruction.**
⚠ `AFFECTOR_DEFORM_HANDOFF.md` documents a later **115 node / 174 link** version
carrying a *non-penetration clamp* (`CLD *` + `CL *`) that stopped the affector
sinking through at side offsets. **That is not in this capture** — the live file
did not have it. If it is wanted, it is separate work, then a re-capture.

## Why the capture is trustworthy
Verified headless against the real `.blend` (read-only, never saved): the spec
was rebuilt into a fresh group, hung on a duplicate torus with the same input
values, and both were evaluated and compared **vertex for vertex**.

```
builder: every property, socket and link applied cleanly (0 problems)
structure: 97 == 97 nodes, 142 == 142 links
interface: identical sockets, same order and types
properties: every node property and socket default matches (0 diffs)
links: identical wiring
evaluated: IDENTICAL geometry, worst vertex delta 0.000e+00
```

**Node-count and property equality are NOT sufficient** — a tree can differ in
ways that look harmless (one wrong enum, one dropped default) and still deform
completely differently. Only the evaluated-geometry comparison actually proves
it, so any future re-capture must repeat that step.

## What made this group easy, and what to watch for next time
This one is the simple case: **17 node types, all plain** (Math, Vector Math,
Attribute Statistic, Raycast, Proximity, Sample Nearest Surface, Blur Attribute,
Set Position, Subdivision Surface, Collection Info, Realize Instances, the input
primitives). Crucially it has **no** Capture Attribute, simulation/repeat zones,
menu or index switches, nested groups, frames, reroutes, or multi-input links —
and **no baked ID pointers** (the collection arrives through a group input).

A group containing any of those needs exporter work first: zones are paired
nodes with their own item collections, Capture Attribute and the switches carry
`*_items` collections, and multi-input sockets are order-sensitive. The current
exporter would silently drop all of it — and the geometry comparison is what
would catch that.

## Gotchas the capture hit
- **Node properties must be applied BEFORE socket defaults.** `data_type`,
  `domain` and `mode` rebuild the socket list, so anything written first is
  thrown away.
- **A rebuilt group gets its OWN `Socket_N` identifiers.** They will not match
  the original's, so modifier values must be carried across **by socket name**,
  never by identifier — otherwise values land on the wrong inputs silently.
- **Blender 5.2 modifier inputs are `mod.properties.inputs[id]["value"]`**, not
  `mod[id]` — the latter raises *"this type doesn't support IDProperties"*.
  Already in `AFFECTOR_DEFORM_HANDOFF.md`; it bit again anyway.
- The interface must be built **before** the nodes, or the Group Input/Output
  nodes come up with no sockets to link.
