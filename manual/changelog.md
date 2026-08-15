# Release notes

The full, user-facing release notes ship **inside the build**, on the app's
**What's New** tab — readable with no internet, which is the one moment release
notes are worth anything.

The same file lives in the repository at
[`app/CHANGELOG.md`](https://github.com/MadihsonNSFW/madihsonnsfw-toolset/blob/main/app/CHANGELOG.md).

Tagged releases are on the
[Releases page](https://github.com/MadihsonNSFW/madihsonnsfw-toolset/releases).

---

## Two version numbers

The project ships two things that version independently:

| Thing | Where to see it |
|---|---|
| **The app** | Top of the What's New tab |
| **The Blender add-on** | **ⓘ About** in the status bar — this is the version *actually connected*, not the one the app hoped it installed |

When a release note says *"update the add-on for this one"*, it means a feature
in that release needs the Blender half as well. **⚙ Settings ▸ Update add-on**.

---

## Updating

**The app does not update itself.** Download a new release from GitHub and unzip
it over your folder — see [Install ▸ Updating](install.md#updating).

After updating the app, press **⚙ Settings ▸ Update add-on** so the Blender half
matches.
