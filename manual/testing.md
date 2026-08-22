# Running the tests

**94 suites, roughly 5,280 checks.**

```powershell
powershell -ExecutionPolicy Bypass -File tests\run_all.ps1
```

The runner needs two things:

| Needs | Default | Override |
|---|---|---|
| Blender | a hardcoded path | `$env:MADI_BLENDER` |
| The app venv | `app\.venv\Scripts\python.exe` | — |

```powershell
$env:MADI_BLENDER = "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
powershell -ExecutionPolicy Bypass -File tests\run_all.ps1
```

A suite whose file is missing is skipped with a note rather than failing the run.

---

## The two kinds of suite

**Blender-side** suites run inside a real Blender, started with
`--factory-startup` so nothing in your own configuration can influence a result.

**App-side** suites run under the app's venv with `QT_QPA_PLATFORM=offscreen`.

Neither kind touches your open `.blend`, your real `config.json` or your real
render queue. The Blender suites run in a separate instance; the app suites run
against stubs and temporary directories.

Run a single suite directly:

```powershell
app\.venv\Scripts\python tests\app_ui_test.py
```

---

## Conventions worth knowing

**Assert on work done, not on wall-clock time.** The performance suite counts
widgets built, event-filter invocations, full-item reads and modules imported.
A timing assertion fails on a busy machine and passes on a fast one regardless
of whether the code is right.

**Instrument the thing under test, not a copy of it.** A benchmark that installs
its own event filter measures its own filter.

**Test through the real entry point.** A guard that is correct in isolation can
still be bypassed by the signal that actually reaches it in the running app.

!!! tip "A new check that passes the first time has proven nothing"
    Make it fail on purpose once — break the thing it guards — and confirm it
    reports that. Otherwise you have written a check that cannot fail.

---

## Local-only suites

Some checks depend on a word list that is deliberately not in the public
repository. Those checks load it if it is present and **pass trivially if it is
not**, so a clone runs clean rather than failing something it could never
satisfy.

---

## What the numbers should be

The run ends with a total. If yours is lower than the number at the top of this
page, look for `SKIP` lines — the usual cause is Blender not being found, which
skips every Blender-side suite at once.
