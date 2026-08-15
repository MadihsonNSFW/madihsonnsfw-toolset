# Anim Layers

Animate in layers, on top of Blender's own NLA. The tab also holds the timeline
marker tools.

---

## Layers

Stack additive passes over a base animation. Each layer has its own **influence**
and **blend mode**.

- **Shape-key layers** as well as bone layers.
- **Set Keyframe / Remove Keyframe** buttons that do exactly what **I** and
  **Alt+I** do in the viewport, using your active keying set — or your
  Preferences default channels if you have none.
- **Merge / Bake** back down when you are happy, right under the layer list.

!!! note "Layers cannot drift apart from Blender's"
    Both sides read and write the same NLA tracks. There is no second copy of
    the state to fall out of step.

There is a **panel inside Blender's N-panel** too, mirroring the same three
settings, so you can work without switching windows. When the two disagree on
first contact, the app's values win.

---

## Markers

Notes and tags attached to timeline markers — in the app, and in a Dope Sheet
sidebar panel inside Blender.

- Write a note on a marker and it is readable from either side.
- Tag markers and filter by tag.
- Marker **layers**, and saved marker sets.

!!! warning "Marker names are not unique"
    Blender lets two markers share a name, and lets one move frames. Neither the
    name nor name-plus-frame is a stable identity, so notes are keyed on
    something that survives both.

---

## Baking

Merge or bake collapses layers back into a single action. Bake gives you the
frame range and preroll; merge keeps the result live.

Both work through the same NLA tracks the tab reads, so what you see in the
layer list is what gets baked.
