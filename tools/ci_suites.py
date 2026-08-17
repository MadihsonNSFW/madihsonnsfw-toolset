"""Run the app-side suites, on any platform, and fail loudly.

    python tools/ci_suites.py

`tests\\run_all.ps1` is the full fleet and it is Windows-only by construction —
it launches Blender for the add-on suites. The CI matrix cannot do that (no
Blender on the runners) and does not need to: the Blender half is unchanged by
a port, and the half that matters is whether the APP runs on a real Linux and a
real Mac.

⚠⚠ **A SUITE THAT CANNOT IMPORT DOES NOT FAIL LOUDLY.** It prints a traceback
and nothing else — no summary line — and a runner that only sums the numbers it
finds reports a healthy total while three suites never ran. That has happened
here (2026-08-15, three suites died on a module-level import and only the
runner's suite list gave it away). So a missing summary is treated as a
FAILURE, not as zero.

⚠ The suite list is read from `run_all.ps1` rather than duplicated, so a suite
added there is picked up here without anyone remembering to.
"""
import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")

# Suites that need something a runner has not got. Keep this list SHORT and
# say why for each — it is the obvious place for coverage to quietly drain.
SKIP = {
    # ⚠ Polices the INTERNAL engineering journal — `docs\`, `HANDOFF.md`,
    # `RESUME_PROMPT.md`, `*_PLAN.md` — every one of which is gitignored by
    # choice. A runner checks out the PUBLIC tree, so those files are not
    # there and never will be: the suite died on `read(HANDOFF)` at import,
    # which reports as "no summary (crashed)" and reads like a broken port.
    # It stays a hard suite in `run_all.ps1`, where the journal exists —
    # which is the only place it can mean anything.
    "docs_test.py": "internal docs are not in a public checkout",
}


def app_suites():
    """The app-side list, straight out of run_all.ps1."""
    text = io.open(os.path.join(TESTS, "run_all.ps1"),
                   encoding="utf-8", errors="replace").read()
    block = text.split("$appSuites = @(", 1)[1]
    out = []
    for line in block.splitlines():
        if line.strip() == ")":
            break
        out += re.findall(r'"([^"]+\.py)"', line)
    return out


def real_config_fingerprint():
    """The real `app\\config.json` as bytes, or None if there is not one.

    ⚠⚠ **NO SUITE MAY WRITE THE REAL CONFIG**, and on 2026-08-17 one did:
    `app_madiref_test.py` was the only app suite that never redirected
    `CONFIG_PATH`, and `madiref\\tab.py` calls `config.save()` to remember the
    last clip — so every run replaced Marty's config with `{}`.

    ⚠ It hid for a long time because FROM SOURCE the damage is invisible:
    `config.load()` merges DEFAULTS and the default library from source is the
    repo's own `library\\`, which exists and is full. Only a FROZEN build shows
    it, because its default library is `dist\\library`, which does not exist.
    A guard on the file itself is the only thing that catches it either way.
    """
    path = os.path.join(ROOT, "app", "config.json")
    try:
        return io.open(path, "rb").read()
    except OSError:
        return None


def main():
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    before_cfg = real_config_fingerprint()

    total = failed = 0
    bad = []
    suites = app_suites()
    print("running %d app-side suites on %s\n" % (len(suites), sys.platform),
          flush=True)

    for name in suites:
        if name in SKIP:
            print("skip %-30s %s" % (name, SKIP[name]), flush=True)
            continue
        proc = subprocess.run([sys.executable, os.path.join(TESTS, name)],
                              capture_output=True, text=True, env=env,
                              encoding="utf-8", errors="replace")
        hit = re.search(r"^(\d+) passed,\s*(\d+) failed",
                        proc.stdout or "", re.M)
        if not hit:
            bad.append((name, "no summary - the suite crashed"))
            print("FAIL %-30s no summary (crashed)" % name, flush=True)
            tail = (proc.stdout or "").splitlines()[-15:] + \
                   (proc.stderr or "").splitlines()[-15:]
            for line in tail:
                print("       %s" % line, flush=True)
            continue
        passed, fails = int(hit.group(1)), int(hit.group(2))
        total += passed
        failed += fails
        if fails:
            bad.append((name, "%d failed" % fails))
            print("FAIL %-30s %d passed, %d failed" % (name, passed, fails),
                  flush=True)
            for line in (proc.stdout or "").splitlines():
                if line.startswith("FAIL"):
                    print("       %s" % line, flush=True)
        else:
            print("ok   %-30s %d passed" % (name, passed), flush=True)

    print("\nTOTAL: %d passed, %d failed" % (total, failed), flush=True)

    if real_config_fingerprint() != before_cfg:
        bad.append(("(the suite run itself)",
                    "app/config.json was MODIFIED - a suite is writing the "
                    "real config instead of a temp one"))
        print("FAIL a suite wrote the REAL app/config.json. Every suite must "
              "point config.CONFIG_PATH (and DATA_DIR) at a temp dir before "
              "importing anything that can save.", flush=True)

    if bad:
        print("Suites with problems: %s"
              % ", ".join("%s (%s)" % b for b in bad), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
