# Bone picker

An AnimSchool-style 2D picker that draws **inside Blender's own Image Editor**.

Trace buttons over a reference picture; click one to select the bone under it.

---

## Button types

| Button | Shape | What it does |
|---|---|---|
| **Bone** | Square | Selects a bone |
| **Group** | Round | Selects a whole set of bones |
| **Shape key** | Wide slider | Scrubs a shape key right from the picker |

Buttons live in canvas space, so they pan and zoom with the picture, and they
may **overlap freely** — nothing blocks a move or a resize.

---

## Working with layouts

- **Multiple tabs per rig** — face, body, hands.
- **Scale one button on its own**: pick its row in the button list and use the
  Scale slider under it. The brush above works on the selection, which is what
  you want for many buttons at once and not what you want for a single group
  handle.
- **Bones & Extras** hides bones, empties and cameras in every 3D viewport in
  one press, so you can pick from the picture with the character clear of
  controls. Press again to bring them back.

!!! note "Layouts are stored on the armature"
    They save inside the `.blend` and travel with the rig.

Save a layout to the Studio Library and **retarget it onto another rig by bone
name**.

---

## The split

The app tab is the manager — tabs and rig, buttons, presets and appearance.
Everything is *drawn* by Blender, so the two can never drift apart.

!!! tip "Label sizes are capped on purpose"
    Blender's font rendering will happily accept a size large enough to bring
    the whole application down at deep zoom. The picker caps them.
