# Rig properties

The second page of the **Organize** tab. Click a rig and every custom property
it carries — Daz morphs, controllers, switches — is listed as a channel: its
value, and where its keyframes sit on the timeline.

A Daz character carries around **775** of these. Finding the four you want,
and keying them without leaving the app, is what this page is for.

## The short version

1. Open **Organize** and pick **Rig properties** on the left.
2. Click a rig in Blender — the page follows it.
3. Type in the filter box, or press **Animated** to see only what has keys.
4. Drag a value, or double-click it to type one. The rig moves in Blender
   as you go.
5. Press the diamond at the end of a row to key it on the current frame.

## The row

| Column | |
|---|---|
| **Property** | The property's name. Hover it for the friendly name a Daz rig gives it ("Breasts Up-Down"). |
| **Value** | Drag it left or right, or **double-click to type an exact number**. Switches show a tickbox; whole numbers stay whole. |
| **Keys** | Every keyframe this property has, drawn across the scene's frame range, with the current frame marked. **Click a diamond to jump to that frame.** |
| **◇** | Insert a key on the current frame — or, if there is already one there, delete it. |

Colours are Blender's own: **green** means the property is animated, **yellow**
means it is keyed on the frame you are on.

## Finding things

- **The filter box** matches the property's name or its friendly name.
- **Animated** — only properties that have keyframes.
- **Keyed here** — only properties keyed on the current frame.
- **Non-zero** — only properties that are not at their default.
- **Sort** by animated-first, by name, or by value.

## Working on several at once

Click a row, then Shift- or Ctrl-click others. The buttons above act on
everything selected:

| Button | |
|---|---|
| **Key** | Insert a keyframe on every selected property. |
| **Delete key** | Delete the key on the current frame, where there is one. |
| **Delete all keys** | Remove every keyframe. **The values stay where they are** — this un-keys, it does not undo. |
| **Reset** | Back to the property's default value. |
| **◀ ▶** | Step to the previous or next keyframe. With rows selected it steps through theirs; with nothing selected, through every animated property. |

Dragging a value does **not** clear your selection, so you can select two rows,
adjust a third, and still key the two.

## The rig picker

**Follow active** is on by default: the page shows whichever armature you last
clicked in Blender. Clicking a *mesh* does not change it — a character's
meshes are what you click most, and following them would empty the page.

Turn Follow active off to pin one rig with the dropdown.

## Refresh

The page keeps itself up to date on its own. The one change it cannot see is
**editing a property's minimum or maximum** in Blender's Edit Property dialog —
press **Refresh** after doing that.

## Typing a value

Double-click any value and type it.

**You can type outside the slider's range.** Dragging stops at the range the
rig gives the property; typing does not — which is how you push a morph past
1.0 when it is built to go there. Blender's own fields work the same way, and
if the property really does have a hard limit, Blender clamps it and the app
shows you what actually landed.

## Things worth knowing

- **Properties that are not a number or a switch** — groups, arrays, text — are
  not shown. The line under the table says how many were left out.
- **A property with no range set** still drags; it just has no filled bar,
  because there is no range to fill.
- Everything here writes into the same undo stack as Blender. **Ctrl+Z** in
  Blender undoes it.
