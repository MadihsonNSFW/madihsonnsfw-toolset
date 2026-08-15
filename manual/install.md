# Install

## 1. Run the app

There is no installer. Unzip the build anywhere you like and run
**`MadihsonNSFW Toolset.exe`**.

!!! tip "Put it somewhere you can write to"
    The app keeps its settings next to itself. `Program Files` works, but a
    folder in your user profile or on a data drive avoids permission prompts.

Only one copy runs at a time. Launching it again — from a shortcut, or from
Blender's **Open Toolset App** button — brings the window you already have to
the front rather than opening a second one onto the same library.

## 2. Install the Blender add-on

**⚙ Settings ▸ Update add-on** installs it straight into Blender for you.

The app carries the matching add-on version inside itself, so the two can never
end up out of step — press the button after every app update and you are done.

If you would rather do it by hand, install `madi_anim_library-*.zip` through
Blender's **Preferences ▸ Add-ons**.

!!! warning "Blender installs extensions per version"
    Blender 5.1 and 5.2 keep separate extension folders. Installing into one
    does nothing for the other. If you run more than one Blender, install into
    each — and see [Connecting Blender](connecting-blender.md#more-than-one-blender)
    for what happens when two of them are open at once.

## 3. Start the bridge

In Blender's 3D viewport press **N**, open the **MADI** panel, and press
**Start bridge**.

That panel is the *connection*, not a second copy of the app: start and stop the
bridge, open the Toolset, watch your last render. Saving and applying happen in
the app, where the thumbnails and folders are.

## 4. Check the status bar

The bar at the bottom of the app tells you what it is connected to, **including
which .blend** — worth a glance whenever two Blenders are open, since only one
can hold the bridge at a time.

---

## Where your files live

| What | Default | Change it in |
|---|---|---|
| Library | a `library` folder next to the app | ⚙ Settings |
| Render queue | `render_queue` next to the app | — |
| Render presets | `render_presets` next to the app | — |
| Baked maps | `baked` next to the app | Node Editor's Output node |

Nothing you save lives inside the app's program folder except by choice, so an
update never touches your library, your queue, your presets or your baked maps.

!!! note "Point the library anywhere"
    Set it in the app's ⚙ Settings, and — if you also use the panel inside
    Blender — in the add-on preferences. Left alone, both use the `library`
    folder next to the app.

---

## Updating

**The app does not update itself, and makes no network connections at all.**

New releases are published on GitHub:

1. Download the latest build from the
   [Releases page](https://github.com/MadihsonNSFW/madihsonnsfw-toolset/releases).
2. Close the app.
3. Unzip it over your existing folder.
4. Start it, then press **⚙ Settings ▸ Update add-on** so the Blender half
   matches.

!!! success "Your own files are not in there"
    The library, render queue, presets and baked maps live outside the program
    folder, so replacing it never touches them.

!!! info "Why there is no auto-update"
    Every tool is free and the source is public, so there is nothing to gate and
    nobody to check in with. Removing the updater removed the last reason for
    this app to open a socket to anywhere but your own Blender.

---

## Building it yourself

See **[Building from source](building.md)**.
