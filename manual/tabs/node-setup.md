# Node Setup

Two node-editor helpers, driven from here rather than from a shelf inside
Blender.

---

## Relink

**Move a wired node's outgoing links onto an unconnected one**, in any node
tree.

- Sockets are matched by name and type.
- Multi-input sockets keep both connections.
- There is a **copy inputs** option.

The common case: you have built a replacement node beside the one in use, and
you want everything downstream to point at the new one without dragging a dozen
wires by hand.

---

## Image Sequence Setup

Point it at a compositor **Image** node and it:

1. counts the frames on disk,
2. sets the sequence properties,
3. sets the scene frame range,
4. builds the shot output path.

No typing into file fields.

---

!!! tip "The same logic ships as a standalone add-on"
    If you want these two tools inside Blender without the app, they also exist
    as a separate node-editor add-on. The copies have diverged since they were
    split, so treat this tab as the current one.
