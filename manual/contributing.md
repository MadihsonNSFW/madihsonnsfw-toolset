# Contributing

## Before you start

- Open an issue first for anything larger than a fix. The two halves of this
  project have to stay in step, and it is worth agreeing where a change belongs
  before it is written.
- Run the [test suite](testing.md) before and after. If your change breaks a
  check, the check is usually right.

## The shape of the project

| Half | Where | Language |
|---|---|---|
| Desktop app | `app/` | Python 3.10, PySide6 |
| Blender extension | `blender_addon/madi_anim_library/` | Python, `bpy` |

They talk over [the bridge](bridge.md). A change to one half often needs a
change to the other, and the **version handshake** is what keeps a mismatched
pair from misbehaving quietly: bump the add-on version and the app's expectation
together.

## House rules

**Add a test with the change.** Every module here has a suite; find the one that
covers what you touched.

**A tool must degrade, not break.** If an add-on is older than the app expects,
the affected tool should be unavailable with a clear reason — never a crash and
never a silent no-op. Capability checks, not hard version gates.

**Nothing user-owned goes in the program folder.** Libraries, queues, presets and
baked maps live outside it, so an update can never eat them.

**Watch out for the polled reads.** Anything named `*_status` is called
continuously. It must not write to the scene.

## Style

Match the file you are editing. The codebase leans on:

- Explanatory comments where the reason is not obvious from the code — especially
  where a simpler-looking approach is wrong. Those comments are load-bearing.
- `⚠` in a comment marks something that has already caused a real bug.

## Licence

This project is **GPL-3.0**. Contributions are accepted under the same licence.

The Blender half imports `bpy` and so needs a GPL-compatible licence regardless;
one licence for the whole repository keeps it simple.

## Reporting a bug

The app has a **Discord** link under **ⓘ About**, and a developer console with
the app's own log — that log is the useful thing to attach.

For a Blender-side problem, say which Blender version, and which add-on version
**ⓘ About** reports as connected. Those two are the first thing anyone will ask.
