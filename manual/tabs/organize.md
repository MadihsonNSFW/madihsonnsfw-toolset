# Organize

The Organize tab has two pages, listed on the left: **Isolate** — this one —
and **[Rig properties](rig-properties.md)**, where a rig's morphs and switches
live.

## Isolate

Group the objects in your scene into named **sets** — a rig with its meshes,
the lights for a shot, the props on a table — and show just one of them with a
single button.

The sets are saved **inside your `.blend`**. Rename the file, rename the
objects, hand it to someone else: the sets travel with it. Nothing about them
is stored in the app.

## The short version

1. Select some objects in Blender.
2. Press **New set from selection**. Give it a name with **Rename**.
3. Press **Isolate** (or the star on its row) and the viewport shows only that
   set.
4. Press it again to put your scene back exactly as it was.

## Making and changing sets

| Button | What it does |
|---|---|
| **New set from selection** | Makes a set from whatever is selected in Blender. With nothing selected you get an empty set to fill later. |
| **Rename** / **Delete** | Deleting a set never touches the objects themselves. |
| **▲ ▼** | Reorder the list. |
| **Add selected** | Adds what is selected in Blender to the open set. |
| **Remove** | Removes the members you have picked in the list on the right. The objects are not deleted. |
| **Select in Blender** | Selects the whole set in the viewport, and makes its rig the active object so you can start posing. |

A set can hold anything — meshes, an armature, lights, cameras, empties — in
any mix. The same object can be in as many sets as you like.

## Isolate

**Isolate is Blender's Local View** — exactly the same thing as selecting the
set and pressing **/**. The viewport shows only that set; press it again to
come back out.

Because it is Local View and not hiding:

- **Nothing about your scene changes.** No objects are hidden, no collections
  are switched off, and your renders are unaffected. It is only what that
  viewport is showing.
- **Isolating a set selects it**, the same way pressing `/` works on whatever
  you have selected — so you can start posing straight away.
- **Only one set at a time.** Pressing a different set's star switches to it
  without re-framing the view.
- **Add a member while a set is isolated and it appears immediately.** Remove
  one and it goes.
- **If you leave Local View yourself** with `/`, the app notices — pressing
  Isolate on the same set takes you back in rather than doing nothing.

## In Blender

The same list is in the 3D viewport sidebar — press **N**, then the
**MadihsonNSFW** tab, then **Organize**. It has the same star per row, the
same Select / Add / Remove buttons, the same big Isolate, and a **Members**
panel underneath.

There is no syncing to do or think about: both the app and the sidebar are
reading the same list out of your `.blend`. Change it in either and the other
follows within a second or two.

## When an object goes away

Delete an object that belongs to a set and the set does not quietly forget it.
The row shows a ⚠ and the member is listed as **(deleted)**, so you can see
what happened. **Clean missing** drops those entries when you are ready.

Renaming an object is not a problem at all — a set follows its objects through
renames.

## Needs the Blender add-on

Organize is entirely a Blender feature — the sets live in your file and Isolate
changes what Blender shows — so it needs the **0.51.0** add-on or newer. On an
older one the tab greys out and tells you. Update it from ⚙ **Settings ▸
Update add-on**.
