# The bridge protocol

The app drives Blender by sending commands to the add-on over a TCP socket on
**port 9877**, bound to the loopback interface.

There are **166 commands**, in families named after the tab that uses
them — `al_*` (Anim Layers), `opt_*` (Optimization), `quad_*` (Quadify),
`jiggle_*` (Physics), `marker_*`, `madiref_*`, `picker_*`, `tex_*` (Texture
Maps), `sets_*` (Organize ▸ Isolate) and `rig_props_*` (Organize ▸ Rig
properties), plus the library and rendering commands, which
predate the convention and are named for what they do.

---

## Wire format

One JSON object per line, in both directions.

**Request**

```json
{"cmd": "status", "params": {}}
```

**Reply**

```json
{"ok": true, "result": {"blend": "shot_040.blend", "frame": 118}}
```

or

```json
{"ok": false, "error": "no armature selected"}
```

!!! danger "Every line must start with `{`"
    Anything else closes the connection. This is a deliberate hardening measure,
    not a parser limitation — see [Security](#security) below.

!!! warning "Replies are wrapped in `result`"
    Reading the top level of the reply instead of `result` makes a perfectly
    healthy add-on look dead: you get `unknown command: ''`, which reads exactly
    like a feature being refused. If a command seems to be returning nothing,
    check that you are unwrapping.

One command carries an `auth` field — see below.

---

## Capabilities

The add-on reports what it can do, and the app uses that to decide which tools
are available.

The capability list is **derived from the dispatcher's own source** at runtime,
by pulling every `cmd == "name"` out of it. A hand-maintained list is the thing
that goes stale, and a stale one makes the app hide a feature that works
perfectly.

!!! note "A command that grows a *parameter* cannot be capability-checked"
    The command name has not changed, so the capability list looks identical
    whether or not the new parameter is understood. Commands that gain a
    parameter echo it back in the reply, so the caller can tell.

---

## Command families

| Prefix | Count | What it covers |
|---|---|---|
| `anim_layers_*` | 29 | Layers, baking, keying, shared settings |
| `opt_*` | 16 | Scene optimisation |
| `picker_*` | 15 | Bone picker layouts and buttons |
| `jiggle_*` | 12 | Bone jiggle |
| `marker_*` | 11 | Timeline marker notes and tags |
| `save_*` / `apply_*` | 16 | Library item round trips |
| `madiref_*` | 7 | Video reference |
| `list_*` | 7 | Scene reads |
| `quad_*` | 6 | Quad remeshing |
| `render_preset_*` | 3 | Render presets |

---

## The main-thread queue

Requests are drained by a `bpy.app.timers` callback on **Blender's main
thread**.

!!! warning "A long command blocks every other command"
    While an optimiser pass, a bake or an Alembic export is running, that
    callback is not running, and anything else sent in the meantime waits. This
    is not a bug — it is what "Blender is busy" means.

The timer interval is the per-command latency floor, so it is **adaptive**: hot
while commands are flowing (bursts and drags run at roughly 200/s), idle when
nothing has arrived for a while. A flat interval made every click cost about
50 ms, of which the actual work was about 1 ms.

One command — the optimiser's progress read — deliberately skips the queue, so
you can watch a long job that is itself holding the queue.

---

## Polled commands are pure reads

Several `*_status` commands are polled continuously by the app, and so are
`marker_list`, `sets_list` and `rig_props_list` while their tabs are on
screen. **They must never write.**

The reason is concrete: a write from a polled command dirties the user's open
`.blend` simply because the app is running. A status read that quietly creates a
default picker tab, or mints an id, will mark a file unsaved that the user never
touched.

Each polled read answers with a cheap `revision` — a hash of everything the
app draws from it — so the app can compare one integer and rebuild nothing at
all when the answer has not moved, which is nearly always.

---

## Security

The socket is on loopback, but loopback is not private: **a web page in your
browser can open a connection to a local port.** The bridge is hardened
accordingly.

- Every line must start with `{`, which rejects HTTP preflight and stray text.
- Commands that write carry an `auth` token, shared through a file only local
  processes can read.
- Add-on updates must come from the app, and are refused otherwise.

!!! danger "Read `docs/security.md` in the repository before touching a route"
    If you are adding a command that writes to the scene, to disk, or to
    preferences, the threat model matters and it is written down.

---

## Adding a command

1. Add the handler to `server.py`'s dispatcher as `cmd == "your_name"`.
2. The capability list picks it up automatically — nothing to register.
3. If it writes, put it behind the auth check.
4. If it can run long, remember it will block the queue while it does.
5. Add a suite under `tests/` — see [Running the tests](testing.md).

!!! warning "The version handshake"
    The app declares the add-on version it expects. If you add a command the app
    depends on, bump the add-on version and the app's expectation together, or
    the app will hide the feature rather than call something that is not there.
