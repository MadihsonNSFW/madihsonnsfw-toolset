# Troubleshooting

## Connection

**"Not connected", with Blender open**

1. Start the bridge: Blender ▸ **N** ▸ **MADI** ▸ **Start bridge**. It does not
   start itself.
2. Check no other Blender is holding it — one port, one holder.
3. Stop and start it again.

**It connected to the wrong Blender**

The status bar names the open `.blend`. If it is not the file you are looking
at, stop the bridge in the instance that has it and start it in the one you
want. This happens most often right after an add-on update, because installing
the extension reloads it and frees the port for a moment.

---

## The add-on

**A tool says it needs a newer add-on**

**⚙ Settings ▸ Update add-on**. The app carries the exact version it expects.

**The update seemed to work, but the version did not change**

Check **ⓘ About** for the add-on version actually connected — that is read from
the running extension. Two things cause this:

- **You have more than one Blender version.** Extensions install per version.
  The update went to the one that was connected.
- **The bridge moved during the install.** The reload frees the port; another
  waiting Blender can take it. Restart the bridge in the instance you want.

**A Blender panel says something is locked**

It cannot, any more — there are no locks. If you see wording like that, you are
running an add-on older than 0.47.0 alongside a newer app. Press
**⚙ Settings ▸ Update add-on**.

---

## Performance

**The library grid is slow with a lot of items**

It should not be — filtering hides rows rather than rebuilding, and thumbnails
decode lazily under byte-capped caches. If it is slow, note the item count and
raise an issue; this path has measured budgets and a regression in it is a bug.

**Blender stutters while a tool runs**

Long commands hold Blender's main thread by design — that is what "Blender is
busy" means. Optimiser passes, bakes and Alembic exports all do this. The
progress bar keeps updating because that one read deliberately skips the queue.

---

## Rendering

**A queued render used an old version of my file**

Use **Save & Queue** rather than adding the file by hand. The queue renders what
is on disk; Save & Queue saves your open file first.

**A render preset did not apply everything**

Applying reports what changed, what already matched, and what this Blender
refused. One refused setting never stops the rest. A setting that does not exist
in your Blender or engine is reported rather than forced.

---

## Baking

**A baked map came out empty**

The Node Editor tells you why rather than handing you a blank file — no lights
in the scene, a fully transparent surface, or a material with more than one
Material Output.

**The bright areas look wrong**

If the map is clipping, the tool says so. Naming the output `.exr` keeps the real
values.

---

## Optimisation

**Restore cannot find a texture**

It warns in red and names it. Restore survives a **Save As**, but not a texture
that has been moved or deleted outside the app.

**I switched texture sets and the files were gone**

The cache is watched — if a set's files are cleared or moved, you are told
before you switch to it. **Clear cache folder** restores first, then deletes only
files the Toolset wrote.

---

## Still stuck

The app has a developer console with its own log, and a **Discord** link under
**ⓘ About**. Attach the log, the Blender version, and the add-on version that
**ⓘ About** reports as connected.
