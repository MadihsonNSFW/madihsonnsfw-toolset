"""Main-thread sampling profiler for a Blender that gradually slows to a halt.

WHY THIS EXISTS
    Marty's second Blender instance degrades over a session and eventually
    stops responding. It has been measured at ~90-96% of one core on its MAIN
    THREAD with nothing of ours running in it. Seven bpy timers are registered
    there (five BlenderKit, incremental_auto_save, mcp) and none of them are
    ours — but "which of those seven" was never answered, because the usual way
    to answer it is to disable add-ons one at a time, and these are add-ons that
    are needed.

WHAT IT DOES INSTEAD
    Samples the MAIN THREAD's Python stack 20 times a second from a background
    thread, tallies what it finds, and writes a report to disk every 15 seconds.
    It disables nothing, unregisters nothing and touches no scene data.

    Read-only: `sys._current_frames()` hands back the topmost frame of every
    thread. Looking at it costs nothing and cannot perturb what it is watching
    the way wrapping each timer function would.

THE THREE ANSWERS IT CAN GIVE
    1. "hot, and the Python is in <add-on>"  -> that add-on's timer is the cost,
       and its own preferences are where to look for a way to calm it down.
    2. "hot, and there is NO Python on the main thread" -> it is not an add-on's
       Python at all. It is Blender's own C code (depsgraph, drawing, undo), and
       the scene is the thing to look at.
    3. "not hot" -> the report window missed it. Leave it running longer.

    Answer 2 is the one worth waiting for: it is the difference between blaming
    an add-on and blaming an 8.6 GB scene, and nothing else distinguishes them.

⚠ IT WRITES THE REPORT AS IT GOES, every 15 seconds, rather than at the end.
  A hang usually ends in a Task-Manager kill, and a profiler that only prints
  its findings on a clean exit would lose exactly the run that mattered.

HOW TO USE IT
    In Blender: Scripting workspace -> Open -> this file -> Run Script.
    It starts immediately and says so in the console. Then work normally, and
    when it next slows down, send me the report file.

    To stop it early, run this in Blender's Python console:
        import madi_mainthread_profiler as p; p.stop()
"""

import os
import sys
import threading
import time
from collections import Counter

REPORT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "blender_hang_report.txt")
INTERVAL = 0.05          # 20 samples a second — invisible next to a hang
FLUSH_EVERY = 15.0       # seconds between report rewrites
TOP_N = 12

_state = {"thread": None, "stop": False}


def _addon_of(filename):
    """Which extension/add-on a source file belongs to, or None.

    Blender 4.2+ extensions live under `...\\extensions\\<repo>\\<name>\\`, and
    legacy add-ons under `...\\addons\\<name>\\`. Taking the component AFTER the
    marker is what makes the report say "blenderkit" rather than a path nobody
    can scan at a glance.
    """
    parts = os.path.normpath(filename).split(os.sep)
    for marker in ("extensions", "addons"):
        if marker in parts:
            i = parts.index(marker)
            tail = parts[i + 1:]
            if marker == "extensions" and len(tail) >= 2:
                return tail[1]          # skip the repo folder (blender_org/…)
            if tail:
                return tail[0]
    return None


def _sampler(main_id, started):
    no_python = 0
    by_addon = Counter()
    by_frame = Counter()
    by_stack = Counter()
    samples = 0
    last_flush = time.monotonic()
    last_cpu = time.process_time()
    last_wall = time.monotonic()
    windows = []             # (clock, cpu% of one core, samples, top addon)
    win_addon = Counter()
    win_samples = 0
    win_nopython = 0

    while not _state["stop"]:
        time.sleep(INTERVAL)
        samples += 1
        win_samples += 1
        frame = sys._current_frames().get(main_id)
        if frame is None:
            # No Python frame on the main thread: it is inside Blender's own C
            # code. That is a FINDING, not a gap in the data.
            no_python += 1
            win_nopython += 1
            continue

        stack = []
        outermost_addon = None
        f = frame
        while f is not None:
            fn = f.f_code.co_filename
            stack.append("%s:%d %s" % (os.path.basename(fn), f.f_lineno,
                                       f.f_code.co_name))
            addon = _addon_of(fn)
            if addon:
                outermost_addon = addon      # keep walking: outermost wins
            f = f.f_back

        by_frame[stack[0]] += 1
        by_stack[" <- ".join(stack[:6])] += 1
        label = outermost_addon or "(not an add-on)"
        by_addon[label] += 1
        win_addon[label] += 1

        now = time.monotonic()
        if now - last_flush >= FLUSH_EVERY:
            cpu = time.process_time()
            wall = now
            pct = (cpu - last_cpu) / max(1e-9, wall - last_wall) * 100.0
            top = win_addon.most_common(1)
            windows.append((time.strftime("%H:%M:%S"), pct, win_samples,
                            win_nopython,
                            top[0][0] if top else "-"))
            last_cpu, last_wall, last_flush = cpu, wall, now
            win_addon.clear()
            win_samples = 0
            win_nopython = 0
            _write(started, samples, no_python, by_addon, by_frame, by_stack,
                   windows)

    _write(started, samples, no_python, by_addon, by_frame, by_stack, windows,
           final=True)


def _write(started, samples, no_python, by_addon, by_frame, by_stack, windows,
           final=False):
    mins = (time.time() - started) / 60.0
    out = []
    out.append("Blender main-thread profile")
    out.append("=" * 64)
    out.append("pid %d   file %s" % (os.getpid(), sys.modules["bpy"].data.filepath))
    out.append("running %.1f min   %d samples at %d/s%s"
               % (mins, samples, round(1 / INTERVAL),
                  "   [STOPPED]" if final else ""))
    if samples:
        out.append("main thread had NO Python frame in %.1f%% of samples "
                   "(that share is Blender's own C code)"
                   % (100.0 * no_python / samples))
    out.append("")
    out.append("-- process CPU per %ds window (%% of ONE core) ---------------"
               % int(FLUSH_EVERY))
    out.append("  time      cpu%%   samples  no-py  busiest")
    for clock, pct, n, nopy, top in windows[-40:]:
        out.append("  %s  %6.1f  %7d  %5d  %s" % (clock, pct, n, nopy, top))
    out.append("")
    out.append("-- where the main thread's Python was, by add-on ------------")
    for name, n in by_addon.most_common(TOP_N):
        out.append("  %6.2f%%  %s" % (100.0 * n / max(1, samples), name))
    out.append("")
    out.append("-- hottest single frames (self time) ------------------------")
    for label, n in by_frame.most_common(TOP_N):
        out.append("  %6.2f%%  %s" % (100.0 * n / max(1, samples), label))
    out.append("")
    out.append("-- hottest stacks -------------------------------------------")
    for label, n in by_stack.most_common(6):
        out.append("  %6.2f%%  %s" % (100.0 * n / max(1, samples), label))
    out.append("")
    try:
        tmp = REPORT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out))
        os.replace(tmp, REPORT_PATH)
    except OSError:
        pass        # a report we cannot write must never take Blender with it


def start():
    if _state["thread"] is not None and _state["thread"].is_alive():
        print("[profiler] already running -> %s" % REPORT_PATH)
        return
    _state["stop"] = False
    main_id = threading.main_thread().ident
    t = threading.Thread(target=_sampler, args=(main_id, time.time()),
                         name="madi_mainthread_profiler", daemon=True)
    _state["thread"] = t
    t.start()
    print("[profiler] sampling the main thread %d/s -> %s"
          % (round(1 / INTERVAL), REPORT_PATH))
    print("[profiler] stop with: import madi_mainthread_profiler as p; p.stop()")


def stop():
    _state["stop"] = True
    t = _state["thread"]
    if t is not None:
        t.join(timeout=2.0)
    _state["thread"] = None
    print("[profiler] stopped -> %s" % REPORT_PATH)


# Registered under a stable name so `stop()` is reachable from the console even
# when this was run as a one-off script (where __name__ is "__main__").
sys.modules.setdefault("madi_mainthread_profiler", sys.modules[__name__])

if __name__ == "__main__":
    start()
