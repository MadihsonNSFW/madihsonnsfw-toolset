# MadihsonNSFW Toolset

**A desktop companion for Blender.** A pose and animation library, a render
queue that works with Blender closed, a 2D bone picker, animation layers, video
reference in the viewport, scene optimisation, texture baking and rig tools — in
one window that talks to Blender over a local connection.

![app](https://img.shields.io/badge/app-1.18.1-2E6F6A)
![add-on](https://img.shields.io/badge/add--on-0.46.0-B4651A)
![blender](https://img.shields.io/badge/blender-5.x-E87D0D)
![python](https://img.shields.io/badge/python-3.10-3776AB)
![licence](https://img.shields.io/badge/licence-GPL--3.0-B4487A)

### 📖 **[Read the manual →](https://madihsonnsfw.github.io/madihsonnsfw-toolset/)**

**Every tool is free.**

---

## What it is

Two halves that talk to each other over TCP on port 9877, on your own machine
and nowhere else:

| Half | What it is |
|---|---|
| **The app** (`app/`) | A PySide6 desktop application — thumbnails, folders, queues, settings |
| **The add-on** (`blender_addon/`) | A Blender extension that listens on a local port and does the Blender-side work |

Blender's interface runs on one thread, and everything you do there competes
with it. Putting the interface in its own process means a library grid can
scroll while a 461-bone rig evaluates next door.

## The tabs

| Tab | What it does |
|---|---|
| **Studio Library** | Ten kinds of reusable item — poses, animations, selection sets, mirror tables, bone remaps, shape keys, Alembic caches, vertex groups, picker layouts, render presets — in one searchable grid |
| **Rendering** | A render queue that survives a crash, a one-button denoising compositor, and render presets covering 164 settings |
| **Bone picker** | Trace clickable buttons over a reference picture, drawn inside Blender's own Image Editor |
| **Anim Layers** | Layered animation on Blender's NLA, plus notes and tags on timeline markers |
| **Node Setup** | Relink a node's outgoing wires; set up an image sequence without typing into file fields |
| **Node Editor** | A node canvas for texture baking, driving Blender's own bake operator |
| **MadiRef** | Video reference playing in the viewport and in the app at once, frame-matched, with drawn notes |
| **Optimization** | Camera-measured texture shrinking, managed decimation, quad remeshing, and what is making your `.blend` so big |
| **Physics** | Spring-driven jiggle on bones, with collision, wind and baking |
| **NSFW Tools** | Ready-made geometry-node rigs, built into your scene in one click |

## Quick start

1. Download a build, unzip it anywhere, run `MadihsonNSFW Toolset.exe`.
2. **⚙ Settings ▸ Update add-on** — installs the Blender half for you.
3. In Blender: **N** ▸ **MADI** ▸ **Start bridge**.

Full instructions: **[Install](https://madihsonnsfw.github.io/madihsonnsfw-toolset/install/)**.

## Build from source

```powershell
git clone https://github.com/MadihsonNSFW/madihsonnsfw-toolset.git
cd madihsonnsfw-toolset
python -m venv app\.venv
app\.venv\Scripts\pip install -r app\requirements.txt
app\.venv\Scripts\python app\main.py
```

Four runtime dependencies: PySide6, psutil, `nvidia-ml-py`, zstandard. The
Blender half has none — it uses Blender's own bundled Python.

See **[Building from source](https://madihsonnsfw.github.io/madihsonnsfw-toolset/building/)**.

## Tests

```powershell
powershell -ExecutionPolicy Bypass -File tests\run_all.ps1
```

82 suites, roughly 4,850 checks. Set `$env:MADI_BLENDER` if your Blender is not
where the runner expects. See
**[Running the tests](https://madihsonnsfw.github.io/madihsonnsfw-toolset/testing/)**.

## Requirements

- **Blender 5.x** (developed against 5.2.0 LTS)
- **Windows** — the app uses Win32 APIs for its window chrome and focus handling
- **Python 3.10** to run from source

## Contributing

Issues and pull requests are welcome — please read
**[Contributing](https://madihsonnsfw.github.io/madihsonnsfw-toolset/contributing/)**
first. The two halves have to stay in step, and the version handshake between
them is what keeps a mismatched pair from misbehaving quietly.

## Links

- **[Discord](https://discord.gg/EPcgrRkdhe)** — report a bug
- **[Patreon](https://www.patreon.com/c/MadihsonNSFW)**

## Licence

**GPL-3.0** — see [LICENSE](LICENSE).

The Blender half imports `bpy` and needs a GPL-compatible licence regardless;
one licence for the whole repository keeps it simple.

### Third-party components

The Quadify tool runs **QuadWild-BiMDF** (GPL-3.0) as a subprocess. Its binaries
are redistributed in `blender_addon/madi_anim_library/engine/` — see
[ATTRIBUTION.md](blender_addon/madi_anim_library/engine/ATTRIBUTION.md) for the
upstream project, version and licence. The remeshing maths is theirs, not ours.
