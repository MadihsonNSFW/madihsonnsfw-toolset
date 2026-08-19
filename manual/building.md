# Building from source

## Requirements

- **Python 3.10** (the app's virtual environment targets it)
- **Blender 5.x** — only needed to run the Blender-side test suites
- **Windows** — the app uses Win32 APIs for window chrome and focus handling

## Run it from a checkout

```powershell
git clone https://github.com/MadihsonNSFW/madihsonnsfw-toolset.git
cd madihsonnsfw-toolset
python -m venv app\.venv
app\.venv\Scripts\pip install -r app\requirements.txt
app\.venv\Scripts\python app\main.py
```

There are only four runtime dependencies — PySide6, psutil, `nvidia-ml-py` and
zstandard. To build the executable as well, use `app\requirements-dev.txt`, which
adds PyInstaller.

The Blender half has **no** pip dependencies: it runs inside Blender and uses
only Blender's own bundled Python.

Running from source is the normal development loop. You do **not** need to build
the executable to test a change.

!!! warning "A source run does not carry an add-on-side fix"
    The app and the Blender extension are separate halves. If your change is in
    `blender_addon\`, push it into Blender with **⚙ Settings ▸ Update add-on**
    as well — running the app from source does not do that for you.

## Build the executable

```powershell
app\build_exe.ps1
```

This produces a one-file build under `app\dist\`.

!!! danger "A rebuild wipes `dist\`"
    If you have been running the built app, save anything you care about out of
    `dist\` first — `config.json`, `render_queue\`, `render_presets\`,
    `_preview_cache\`. They live next to the executable and the build removes
    them.

!!! warning "Do not run the build from inside `dist\`"
    Any shell whose working directory is inside `dist\` will hold a handle on it
    and the build fails with a permission error. This bites most often right
    after you have gone in there to rescue your config — come back out first.

### Verifying a build

```powershell
app\.venv\Scripts\python tools\verify_exe.py
```

This inspects the frozen build for markers proving each module made it in —
including modules that are only imported lazily, which a plain smoke test would
not catch, and whose absence produces a build that starts perfectly and is
quietly missing a feature.

## Layout

| Folder | What is in it |
|---|---|
| `app/` | The desktop application |
| `blender_addon/madi_anim_library/` | The Blender extension |
| `manual/` | This documentation |
| `tests/` | 89 suites — see [Running the tests](testing.md) |
| `tools/` | Build, verification and packaging helpers |
| `specs/` | Asset specifications compiled into the app |
