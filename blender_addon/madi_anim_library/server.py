# MADI Anim Library — local bridge server.
# Newline-delimited JSON over TCP on localhost. The external library app connects
# here to drive Blender. Requests run on Blender's MAIN thread via bpy.app.timers.
#
# Protocol:  {"cmd": "<name>", "params": {...}}\n   ->   {"ok": true, "result": ...}\n
#                                                        {"ok": false, "error": "..."}\n

import bpy
import inspect
import json
import queue
import re
import socket
import threading
import time
import traceback

from . import bridgeauth
from . import core
from . import jiggle

DEFAULT_PORT = 9877  # 9876 is the Blender MCP bridge

# Pulls every `cmd == "name"` out of the dispatcher's own source — see
# BridgeServer.capabilities(). Source-derived on purpose: a hand-maintained
# list is the thing that goes stale and makes the app hide a working feature.
_CMD_RE = re.compile(r'cmd\s*==\s*["\']([a-z0-9_]+)["\']')

# The request queue is drained by a bpy timer, so the timer's interval IS the
# per-command latency floor — a flat 0.05 made every click cost ~50 ms and
# capped live blend drags at 20 updates/s (measured 2026-08-02: status,
# anim_layers_status and node_tools_status all took ~50 ms of which the actual
# work was ~1 ms). So the interval is now adaptive:
#   HOT  while commands are flowing — bursts and drags run at ~200/s
#   IDLE when nothing has arrived for HOT_WINDOW seconds — the first click
#        after a pause pays this once, then the queue goes hot again.
# Both are cheap: an idle tick is one queue.get_nowait() on an empty queue.
_TICK_HOT = 0.005
_TICK_IDLE = 0.03
_HOT_WINDOW = 3.0
# Must cover the SLOWEST main-thread request (alembic export / preview capture
# of a long range) — the app's per-command client timeouts are the real limit.
_REQUEST_TIMEOUT = 600.0

# The biggest single request we will buffer before giving up on finding a
# newline. Picker tab images are the largest real payload by a wide margin and
# are nowhere near this; the number exists so an unbounded write cannot take
# Blender's memory with it.
_MAX_LINE = 64 * 1024 * 1024


class BridgeServer:
    def __init__(self, port=DEFAULT_PORT):
        self.port = port
        self._sock = None
        self._accept_thread = None
        self._queue = queue.Queue()
        self._running = False
        self._blend = None  # active live-blend session
        # the bridge token THIS instance issued, if it won the port (0.25.0)
        self._token = None
        # monotonic stamp of the last request seen; drives the adaptive tick
        self._last_activity = 0.0
        # ⚠ WHY the bridge is not listening, for the N-panel to say out loud.
        # "off" and "another Blender already has the port" look identical from
        # a bare running flag, and they need opposite reactions: one is "press
        # Start", the other is "the app is talking to your OTHER Blender, go
        # and stop it there". Marty hit exactly that with two instances open
        # (2026-08-05).
        # ⚠ The third state was called `waiting` until 0.39.0, when the retry
        # behind that name was deleted (see `start`). It is `blocked` now
        # because nothing waits any more, and a state that says it is waiting
        # while nothing is would be worse than no state at all.
        self.state = "stopped"          # stopped | listening | blocked

    # ------------------------------------------------ lifecycle (main thread)

    def start(self):
        """Bind and listen. **Returns True only when this instance owns the port.**

        ⚠ **IT NEVER QUEUES, AND THAT IS THE POINT** (0.39.0). A losing
        instance used to retry every 5 s and take the port the moment the
        holder let go — so stopping the bridge in one Blender silently handed
        it to another, with nobody pressing anything and no way to tell from
        either window. Marty, 2026-08-12: *"when one is active do not let them
        start it on another blender instance unless they stop first"*. A
        refusal is final until somebody presses Start again.

        ⚠ **The return value is the only honest signal.** The bind failure is
        caught here, so the caller's own `except OSError` can never fire —
        `MADILIB_OT_server_toggle` reported "Bridge listening on port 9877"
        over a bind that had just been refused, for as long as the retry
        existed to paper over it.
        """
        if self._running:
            return True
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Windows: EXCLUSIVE bind. SO_REUSEADDR would let a second Blender
        # instance silently bind the same port and shadow this bridge (old
        # add-on answering new app = 'unknown command' confusion). With an
        # exclusive bind the loser knows, and retries until the port frees up.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", self.port))
        except OSError as exc:
            sock.close()
            self.state = "blocked"
            print("[MadihsonNSFW] port %d is already held by another Blender "
                  "(%s) — stop the bridge there first" % (self.port, exc))
            return False
        sock.listen(4)
        sock.settimeout(1.0)
        self._sock = sock
        self._running = True
        self.state = "listening"
        # Only the instance that WON the port mints a token, so a second Blender
        # cannot overwrite the live one's secret with its own. ⚠ Keep what we
        # were given: `stop()` may only clear a token IT issued, and `_TOKEN` is
        # a module global that every instance in this module can see (0.25.0 —
        # a stranded instance deleting the live token file is exactly how
        # `addon_update` started refusing the real app).
        self._token = bridgeauth.issue()
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        if not bpy.app.timers.is_registered(self._process_queue):
            bpy.app.timers.register(self._process_queue, persistent=True)
        print("[MadihsonNSFW] bridge listening on 127.0.0.1:%d" % self.port)
        return True

    def stop(self):
        self._running = False
        # ⚠ Stopping FREES THE PORT AND NOTHING ELSE CLAIMS IT. `_retry_start`
        # lived here until 0.39.0 and is gone: it was the mechanism by which a
        # second Blender took the bridge over on its own, which is exactly what
        # Marty asked to stop. The other instance's panel keeps saying "in use"
        # until someone presses Start there — a stale label, but an honest one,
        # and pressing Start is now the only way the bridge ever moves.
        self.state = "stopped"
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if bpy.app.timers.is_registered(self._process_queue):
            bpy.app.timers.unregister(self._process_queue)
        bridgeauth.clear(self._token)
        self._token = None
        print("[MadihsonNSFW] bridge stopped")

    @property
    def running(self):
        return self._running

    # ------------------------------------------------ socket threads

    def _accept_loop(self):
        while self._running:
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._client_loop, args=(conn,),
                             daemon=True).start()

    def _client_loop(self, conn):
        buf = b""
        conn.settimeout(300.0)
        try:
            while self._running:
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                # ⚠ A LINE THAT NEVER ARRIVES IS A LINE THAT GROWS FOR EVER.
                # Without this, anything that connects and streams bytes with no
                # newline pushes Blender's memory up until it dies. The cap is
                # far above the largest real command (picker tab images are the
                # big ones) and far below "a problem".
                if len(buf) > _MAX_LINE:
                    conn.sendall(b'{"ok": false, "error": "request too large"}\n')
                    break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    # ⚠⚠ THIS `break` IS A SECURITY BOUNDARY, NOT TIDINESS.
                    #
                    # This loop used to answer "bad json" and CARRY ON READING,
                    # which made the bridge speak a second protocol by accident:
                    # a browser's fetch() POST is a handful of header lines
                    # (each rejected, connection kept) followed by a body - and
                    # the body is a line like any other, so it was dispatched.
                    # Proven against the live bridge on 2026-08-06: an HTTP POST
                    # from a web page ran `ping` on Marty's Blender, which means
                    # any page he visited could reach `addon_update` and install
                    # an extension. A web page cannot be trusted to be a client,
                    # so a client that does not speak our protocol on its FIRST
                    # line is not a client. Hang up.
                    if not line.startswith(b"{"):
                        conn.sendall(b'{"ok": false, "error": '
                                     b'"expected newline-delimited JSON"}\n')
                        return
                    response = self._dispatch(line)
                    conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, raw):
        """Runs on a CLIENT thread: parse, hand to main thread, wait for result."""
        try:
            request = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return {"ok": False, "error": "bad json: %s" % exc}

        # ⚠ ANSWERED HERE, ON THIS SOCKET THREAD, WITHOUT THE MAIN-THREAD QUEUE
        # — and that bypass is the whole point of the command. A Scene Optimizer
        # run holds the main thread for as long as it takes, so `_process_queue`
        # is not running and ANY queued request waits until the run is over. A
        # progress request that only arrives once the work has finished is not
        # progress. So this one is served straight from optimizer.py's counter
        # record, which is plain ints and strings and touches no bpy at all
        # (`optimizer.opt_progress` — read the warning there before adding a
        # second command to this list).
        #
        # Ungated for the same reason `opt_status` is: it reads a counter about
        # work the caller had to be licensed to start, so withholding it
        # protects nothing and would just break the progress bar mid-run.
        if request.get("cmd") == "opt_progress":
            try:
                from . import optimizer
                return {"ok": True, "result": optimizer.opt_progress()}
            except Exception as exc:            # noqa: BLE001
                return {"ok": False, "error": str(exc)}

        done = threading.Event()
        holder = {}
        # Stamp BEFORE queueing (this is a client thread; a float write is
        # atomic under the GIL) so the very next tick is already hot — a burst
        # only pays the idle interval on its first command, not on each one.
        self._last_activity = time.monotonic()
        self._queue.put((request, done, holder))
        if not done.wait(_REQUEST_TIMEOUT):
            return {"ok": False, "error": "timeout waiting for Blender main thread"}
        return holder["response"]

    # ------------------------------------------------ main thread

    def _process_queue(self):
        while True:
            try:
                request, done, holder = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                result = self._handle(request)
                holder["response"] = {"ok": True, "result": result}
            except Exception as exc:
                traceback.print_exc()
                holder["response"] = {"ok": False, "error": str(exc)}
            # A long request (bake, capture) may have taken seconds; re-stamp
            # so the reply and any follow-up command stay on the fast path.
            self._last_activity = time.monotonic()
            done.set()
        return self._tick_interval()

    def _tick_interval(self):
        """Fast while commands are flowing, relaxed when idle."""
        if time.monotonic() - self._last_activity < _HOT_WINDOW:
            return _TICK_HOT
        return _TICK_IDLE

    _capabilities = None

    @classmethod
    def capabilities(cls):
        """Every command name this build's dispatcher answers.

        DERIVED FROM THE SOURCE of `_handle` rather than hand-listed, because a
        hand-written list is exactly the thing that silently goes stale: someone
        adds a command, forgets the list, and the app decides the feature isn't
        available. Parsing the dispatcher can't drift — if the `if cmd == "x"`
        exists, "x" is advertised.

        This is what lets an app and an add-on of different ages work together:
        the app asks what's here instead of assuming, so a NEW app against an
        OLD add-on disables the one affected feature with a clear reason
        instead of erroring, and an OLD app against a NEW add-on is unaffected
        (extra names it never asks about).
        """
        if cls._capabilities is None:
            try:
                src = inspect.getsource(cls._handle)
                cls._capabilities = sorted(set(_CMD_RE.findall(src)))
            except (OSError, TypeError):
                # Source unavailable (frozen/optimised): advertise nothing
                # rather than lie. The app then falls back to version compare.
                cls._capabilities = []
        return cls._capabilities

    def _handle(self, request):
        cmd = request.get("cmd", "")
        p = request.get("params", {}) or {}
        if cmd == "ping":
            return {"pong": True, "blender": bpy.app.version_string,
                    "version": core.ADDON_VERSION,
                    "capabilities": self.capabilities()}
        if cmd == "status":
            ob = bpy.context.active_object
            sel_bones = 0
            if ob is not None and ob.type == 'ARMATURE':
                sel_bones = sum(1 for pb in ob.pose.bones if core.bone_is_selected(pb))
            return {
                # Reported so the app can spot an add-on older than itself.
                "version": core.ADDON_VERSION,
                # …and what this build can actually DO, so the app adapts to a
                # version gap instead of breaking on it.
                "capabilities": self.capabilities(),
                "file": bpy.data.filepath,
                "active_object": ob.name if ob else None,
                "is_armature": bool(ob is not None and ob.type == 'ARMATURE'),
                "mode": bpy.context.mode,
                "selected_bones": sel_bones,
                "frame": bpy.context.scene.frame_current,
                "frame_start": bpy.context.scene.frame_start,
                "frame_end": bpy.context.scene.frame_end,
                # Where THIS scene's renders go, so a playblast can default to
                # the same folder. ⚠ A FIELD, not a command: an add-on too old
                # to send it simply doesn't, and the app keeps its own default
                # — no capability entry needed for something that degrades to
                # "I don't know".
                "output_dir": core.scene_output_dir(),
            }
        if cmd == "save_blend":
            # Saves the user's OPEN FILE. Deliberately parameterless: it saves
            # what is open, where it already lives. A "save as" over the bridge
            # would let a caller write a .blend anywhere on disk, which is a
            # different thing entirely (docs\security.md).
            return core.save_blend()
        if cmd == "list_items":
            return core.list_items(p["library_root"])
        if cmd == "save_pose":
            return core.save_pose(
                p["library_root"], p.get("folder", ""), p["name"],
                use_selected=p.get("use_selected", True),
                description=p.get("description", ""),
                overwrite=p.get("overwrite", False))
        if cmd == "apply_pose":
            return core.apply_pose(
                p["path"], selected_only=p.get("selected_only", False),
                blend=p.get("blend", 1.0), key=p.get("key", False),
                mirror=p.get("mirror", False),
                mirror_table=p.get("mirror_table"),
                remap_table=p.get("remap_table"))
        if cmd == "save_mirror":
            return core.save_mirror(
                p["library_root"], p.get("folder", ""), p["name"],
                description=p.get("description", ""),
                overwrite=p.get("overwrite", False))
        if cmd == "save_set":
            return core.save_set(
                p["library_root"], p.get("folder", ""), p["name"],
                description=p.get("description", ""),
                overwrite=p.get("overwrite", False))
        if cmd == "apply_set":
            return core.apply_set(p["path"], extend=p.get("extend", False))
        if cmd == "capture_preview":
            return core.capture_preview(
                p["path"], width=p.get("width", 256), height=p.get("height", 256),
                frames=tuple(p["frames"]) if p.get("frames") else None,
                shape_steps=p.get("shape_steps"))
        # ⚠ Its OWN command, not a mode on capture_preview. The app's gate works
        # off command NAMES, so a parameter added to an existing command is
        # invisible to it — an older add-on would take the request, ignore the
        # new argument and hand back an ordinary grey viewport shot as though it
        # were the weight paint (the same trap `save_abc`'s options hit).
        if cmd == "capture_vgroup_preview":
            return core.capture_vgroup_preview(
                p["path"], width=p.get("width", 256),
                height=p.get("height", 256),
                max_groups=p.get("max_groups", 24))
        if cmd == "save_anim":
            return core.save_anim(
                p["library_root"], p.get("folder", ""), p["name"],
                frame_start=p.get("frame_start"), frame_end=p.get("frame_end"),
                use_selected=p.get("use_selected", True),
                description=p.get("description", ""),
                overwrite=p.get("overwrite", False),
                bake=p.get("bake", False),
                keep_modifiers=p.get("keep_modifiers", True),
                include_props=p.get("include_props", False))
        if cmd == "apply_anim":
            return core.apply_anim(
                p["path"], mode=p.get("mode", "replace"),
                start_at=p.get("start_at", "current"),
                selected_only=p.get("selected_only", False),
                mirror=p.get("mirror", False),
                mirror_table=p.get("mirror_table"),
                remap_table=p.get("remap_table"),
                blend=p.get("blend", 1.0))
        if cmd == "list_armatures":
            return [{"name": ob.name, "bones": len(ob.pose.bones)}
                    for ob in bpy.context.scene.objects if ob.type == 'ARMATURE']
        if cmd == "build_remap":
            src_names = p.get("source_names")
            if p.get("source_object"):
                src = bpy.data.objects.get(p["source_object"])
                if src is None or src.type != 'ARMATURE':
                    raise RuntimeError("source armature not found: %s"
                                       % p["source_object"])
                src_names = [pb.name for pb in src.pose.bones]
            if not src_names:
                raise RuntimeError("build_remap needs source_names or source_object")
            tgt = core.get_armature()  # active object = TARGET rig
            targets = [pb.name for pb in tgt.pose.bones]
            mapping, unmatched = core.build_remap(src_names, targets,
                                                  p.get("rules"))
            return {"map": mapping, "unmatched": unmatched,
                    "target_bones": targets, "target_armature": tgt.name}
        if cmd == "save_remap":
            return core.save_remap(
                p["library_root"], p.get("folder", ""), p["name"],
                rules=p.get("rules"), mapping=p.get("map"),
                unmatched=p.get("unmatched"), source=p.get("source", ""),
                description=p.get("description", ""),
                overwrite=p.get("overwrite", False))
        if cmd == "list_shape_keys":
            return core.list_shape_keys(p.get("objects"))
        if cmd == "save_shapes":
            return core.save_shapes(
                p["library_root"], p.get("folder", ""), p["name"],
                objects=p.get("objects"), keys=p.get("keys"),
                delete_after=p.get("delete_after", False),
                description=p.get("description", ""),
                overwrite=p.get("overwrite", False))
        if cmd == "apply_shapes":
            return core.apply_shapes(p["path"], mode=p.get("mode", "replace"),
                                     force=p.get("force", False),
                                     to_active=p.get("to_active", False),
                                     blend=p.get("blend", 1.0))
        # --- vertex groups. Two APPLY MODES on purpose: an index-based restore
        # (lossless, needs a matching vertex count) and a spatial transfer
        # (any topology, approximate). They are never the same button.
        if cmd == "list_vertex_groups":
            return core.list_vertex_groups(p.get("objects"))
        if cmd == "save_vgroups":
            return core.save_vgroups(
                p["library_root"], p.get("folder", ""), p["name"],
                objects=p.get("objects"), groups=p.get("groups"),
                description=p.get("description", ""),
                overwrite=p.get("overwrite", False))
        if cmd == "apply_vgroups":
            return core.apply_vgroups(
                p["path"], mode=p.get("mode", "EXACT"),
                to_active=p.get("to_active", False),
                replace=p.get("replace", True),
                source_object=p.get("source_object"))
        if cmd == "delete_shape_keys":
            return core.delete_shape_keys(p["object"], p["keys"])
        if cmd == "save_abc":
            return core.save_abc(
                p["library_root"], p.get("folder", ""), p["name"],
                frame_start=p.get("frame_start"), frame_end=p.get("frame_end"),
                description=p.get("description", ""),
                overwrite=p.get("overwrite", False),
                # Absent = every Blender default (plus selected-only). An older
                # app simply does not send this and gets what it always got.
                options=p.get("options"))
        if cmd == "apply_abc":
            return core.apply_abc(p["path"])
        if cmd == "setup_denoise":
            return core.setup_denoise(
                view_layers=p.get("view_layers"),
                disable_render_denoise=p.get("disable_render_denoise", True),
                combine=p.get("combine", "ALPHA_OVER"),
                split=p.get("split", "PASSES"))
        if cmd == "clear_denoise":
            return core.clear_denoise(
                restore_passes=p.get("restore_passes", True))
        if cmd == "list_view_layers":
            sc = bpy.context.scene
            return {"engine": sc.render.engine,
                    "layers": [{"name": vl.name, "use": vl.use,
                                "denoise_passes": bool(
                                    getattr(vl, "cycles", None)
                                    and vl.cycles.denoising_store_passes)}
                               for vl in sc.view_layers]}
        # Render presets (0.17.0). Three commands rather than one with a mode,
        # because the app asks for the catalogue before it has anything to
        # capture — the save dialog's tick list IS the catalogue.
        if cmd == "render_preset_schema":
            from . import renderpresets
            return renderpresets.schema()
        if cmd == "render_preset_capture":
            from . import renderpresets
            return renderpresets.capture(groups=p.get("groups"))
        # ⚠ `data` here came off a JSON file on the user's disk, so
        # `renderpresets.apply` writes ONLY paths its own catalogue names and
        # reports anything else as rejected. Do not "simplify" that away — this
        # route is a setattr loop reachable from a socket.
        if cmd == "render_preset_apply":
            from . import renderpresets
            return renderpresets.apply(p.get("data") or {},
                                       groups=p.get("groups"))
        if cmd == "node_tools_status":
            return core.node_tools_status(
                output_folder=p.get("output_folder", "exr_composited"),
                output_suffix=p.get("output_suffix", "_exr_composited_"))
        if cmd == "relink_nodes":
            return core.relink_nodes(
                match_mode=p.get("match_mode", "NAME"),
                index_fallback=p.get("index_fallback", False),
                copy_inputs=p.get("copy_inputs", False))
        if cmd == "setup_image_sequence":
            return core.setup_image_sequence(
                set_scene_range=p.get("set_scene_range", True),
                start_at_one=p.get("start_at_one", True),
                set_output=p.get("set_output", True),
                output_folder=p.get("output_folder", "exr_composited"),
                output_suffix=p.get("output_suffix", "_exr_composited_"))
        if cmd == "anim_layers_status":
            status = core.anim_layers_status(
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
            # The settings the N-panel and the app both own ride along here
            # rather than in a command of their own: status is ALREADY polled,
            # so a change made in Blender reaches the app without anything
            # having to notice it happened. See anim_layers_ui.py.
            from . import anim_layers_ui
            status["prefs"] = anim_layers_ui.shared_prefs()
            return status
        if cmd == "anim_layers_set_prefs":
            from . import anim_layers_ui
            return {"prefs": anim_layers_ui.apply_prefs(p.get("prefs") or {})}
        # --- Timeline markers. FREE, so no entitlement check here on purpose:
        # the app's Markers tool lives in the free Anim Layers tab, and a gate
        # on this side would lock a tab the app leaves open (docs\markers.md).
        #
        # ⚠ `marker_list` IS POLLED AND MUST STAY A PURE READ. It is the one
        # that carries `revision`, which is the whole point: the app compares
        # that value and only rebuilds its list when Blender actually changed
        # something. Anything that writes from here — even minting an id —
        # dirties the user's open file just because the app is running.
        if cmd == "marker_list":
            from . import markers
            return markers.marker_list()
        if cmd == "marker_add":
            from . import markers
            return markers.marker_add(p.get("name", "Marker"), p.get("frame"),
                                      note=p.get("note", ""),
                                      tags=p.get("tags"),
                                      layer=p.get("layer", ""))
        if cmd == "marker_set":
            from . import markers
            # ⚠ `camera` uses a SENTINEL, not None: None is a real value here
            # (it clears the binding), so "absent" and "clear it" cannot be the
            # same thing. Sending every field on every edit is what would let a
            # stale app copy overwrite a note typed in Blender.
            return markers.marker_set(
                p.get("ref") or {}, name=p.get("name"), frame=p.get("frame"),
                note=p.get("note"), tags=p.get("tags"), layer=p.get("layer"),
                camera=p["camera"] if "camera" in p else ...)
        if cmd == "marker_remove":
            from . import markers
            return markers.marker_remove(p.get("ref") or {})
        if cmd == "marker_goto":
            from . import markers
            return markers.marker_goto(p.get("ref") or {})
        if cmd == "marker_bind_by_name":
            from . import markers
            return markers.marker_bind_by_name(exact=p.get("exact", True))
        if cmd == "marker_show_layer":
            # ⚠ THIS ONE MOVES REAL MARKERS. Showing a layer takes the others
            # out of `scene.timeline_markers` and parks them on the scene, which
            # is the only way to clear Blender's timeline strip — it always
            # draws every marker it has (docs\markers.md).
            from . import markers
            return markers.show_layer(p.get("layer", ""))
        if cmd == "marker_set_save":
            from . import markers
            return markers.marker_set_save(p.get("name", ""))
        if cmd == "marker_set_load":
            from . import markers
            return markers.marker_set_load(p.get("name", ""))
        if cmd == "marker_set_delete":
            from . import markers
            return markers.marker_set_delete(p.get("name", ""))
        if cmd == "marker_rename":
            from . import markers
            return markers.marker_rename(
                find=p.get("find", ""), replace=p.get("replace", ""),
                prefix=p.get("prefix", ""), suffix=p.get("suffix", ""),
                only=p.get("only"))
        # --- MadiRef: the video reference overlay. These carry NO pixels — the
        # frames travel through a shared-memory segment whose name arrives here
        # over the authenticated bridge and nowhere else, so an attacker who
        # cannot call this cannot guess it either (docs\security.md).
        # ⚠ The segment is mapped READ-ONLY in spirit: this side writes only the
        # scene frame/fps it publishes back, never pixels.
        #
        # --- MEMBERS ONLY since 2026-08-11 (Marty: "Make MadiRef paywalled").
        # ⚠ THE APP'S LOCK IS NOT THIS SIDE'S LOCK. The bridge is a listening
        # socket and these routes answer anything that reaches it, which is
        # why a PAID tab always needs a prefix gate HERE as well as in the
        # app. ⚠ The madiref_* prefix gate (0.38.0) was REMOVED 2026-08-14:
        # every tab went free — the paid thing is premium packs, and their
        # gate lives in the app's licence SERVER, which refuses the download,
        # not in Blender. If a tab is ever gated again the gate returns as:
        # prefix check -> entitlement.unlocked() -> refuse, with EXEMPTIONS
        # for whatever undoes or reports the feature (madiref_close/_status
        # were exempt here; docs\licensing.md keeps the reasoning).
        if cmd == "madiref_open":
            from . import madiref
            return madiref.madiref_open(
                p["name"], plane_object=p.get("plane_object") or "",
                sync_framedrop=p.get("sync_framedrop"))
        if cmd == "madiref_close":
            from . import madiref
            return madiref.madiref_close()
        if cmd == "madiref_config":
            # ⚠ EVERY parameter must be forwarded here. `occlude` and
            # `occlude_distance` were added to madiref_config() and NOT to this
            # call, so the app's depth setting was silently dropped and the
            # slider did nothing. Nothing catches that but the echoed reply —
            # which is exactly why this command echoes its whole state back.
            from . import madiref
            return madiref.madiref_config(
                plane_object=p.get("plane_object"),
                sync_framedrop=p.get("sync_framedrop"),
                occlude=p.get("occlude"),
                occlude_distance=p.get("occlude_distance"),
                locked=p.get("locked"))
        if cmd == "madiref_pin":
            from . import madiref
            return madiref.madiref_pin(p.get("mode") or "viewport")
        if cmd == "madiref_make_plane":
            from . import madiref
            return madiref.madiref_make_plane(
                name=p.get("name") or "MADI_Reference",
                distance=float(p.get("distance", 5.0)),
                height=float(p.get("height", 2.0)))
        if cmd == "madiref_reset_view":
            from . import madiref
            return madiref.madiref_reset_view()
        if cmd == "madiref_status":
            from . import madiref
            return madiref.madiref_status()
        # ⚠ THE THREE `license_*` COMMANDS WERE REMOVED IN ADD-ON 0.47.0,
        # with `entitlement.py` and the whole app-side licensing package. There
        # is no entitlement to unlock, no signed blob to verify and no
        # `licensed` field on the status poll any more. An older app calling
        # them gets the ordinary "unknown command" answer, which is what its own
        # capability check is there to prevent.
        # --- Bone picker. The layout itself lives on the armature and the tabs
        # on the Scene, so these read and write the SAME data the Image Editor
        # panel does - there is no second copy to keep in step. See picker.py.
        #
        # ⚠ THE BRIDGE IS A SECOND WAY IN, AND IT NEEDS THE GATE TOO. The
        # operators are gated in picker.py, but these routes call the picker's
        # API functions DIRECTLY - so without this check a ten-line socket
        # client on 127.0.0.1:9877 would drive the whole picker with no licence,
        # which is easier than editing the add-on and would make the operator
        # gate decorative. Checked here rather than on each function so a picker
        # command added later is covered without anyone remembering.
        #
        # `picker_status` is deliberately EXEMPT: it is a pure read of the
        # user's own scene, the app needs it to show anything at all, and
        # withholding it protects nothing.
        # ⚠ NO GATE ON `picker_*` ANY MORE — the Bone picker is free
        # (2026-08-06). Every write used to refuse here unless the app had
        # handed over a signed licence. `opt_*` below is still gated; do not
        # copy this block's absence onto that one.
        if cmd == "picker_status":
            from . import picker
            return picker.picker_status()
        if cmd == "picker_set_tab":
            from . import picker
            return picker.picker_set_tab(p["index"])
        if cmd == "picker_add_tab":
            from . import picker
            return picker.picker_add_tab(name=p.get("name"))
        if cmd == "picker_remove_tab":
            from . import picker
            return picker.picker_remove_tab(index=p.get("index"))
        if cmd == "picker_rename_tab":
            from . import picker
            return picker.picker_rename_tab(p["name"], index=p.get("index"))
        if cmd == "picker_set_tab_rig":
            from . import picker
            return picker.picker_set_tab_rig(p.get("object"),
                                             index=p.get("index"))
        if cmd == "picker_set_tab_image":
            from . import picker
            return picker.picker_set_tab_image(p.get("image"),
                                               index=p.get("index"))
        if cmd == "picker_set_button":
            from . import picker
            fields = {k: v for k, v in p.items() if k != "index"}
            return picker.picker_set_button(p["index"], **fields)
        if cmd == "picker_remove_buttons":
            from . import picker
            return picker.picker_remove_buttons(p.get("indices") or [])
        if cmd == "picker_set_brushes":
            from . import picker
            return picker.picker_set_brushes(**p)
        if cmd == "picker_set_prefs":
            from . import picker
            return picker.picker_set_prefs(p.get("prefs") or {})
        if cmd == "picker_start":
            from . import picker
            return picker.picker_start()
        if cmd == "picker_stop":
            from . import picker
            return picker.picker_stop()
        if cmd == "picker_save_item":
            from . import picker
            return picker.picker_save_item(
                p["library_root"], p.get("folder", ""), p["name"],
                overwrite=p.get("overwrite", False))
        if cmd == "picker_apply_item":
            from . import picker
            return picker.picker_apply_item(p["path"],
                                            replace=p.get("replace", True))
        # --- Scene Optimizer. ⚠ Its opt_* prefix gate was REMOVED
        # 2026-08-14 (all tabs free; the paid thing is premium packs).
        # The exemption list is the pattern to copy if a prefix gate ever
        # returns: opt_status / opt_progress / opt_revert_images /
        # opt_revert_meshes / opt_clear_cache —
        # "reporting, undoing and STOPPING our own work must never be what a
        # lapsed licence takes away" (docs\licensing.md).
        if cmd == "opt_status":
            from . import optimizer
            return optimizer.opt_status()
        # ⚠ This route is the one `capabilities()` can see, and that is why it
        # exists: the live path is the bypass in `_dispatch` above, which is
        # invisible to the source scan that builds the capability list. It also
        # keeps the command working the ordinary way when nothing is running.
        # Both paths call the same function, so they cannot answer differently.
        if cmd == "opt_progress":
            from . import optimizer
            return optimizer.opt_progress()
        if cmd == "opt_plan":
            from . import optimizer
            return optimizer.opt_plan(p)
        if cmd == "opt_resize":
            from . import optimizer
            return optimizer.opt_resize(p)
        # Named texture sets ("material groups"). A set records WHICH images at
        # WHICH sizes and owns no files, so deleting one can never cost anyone
        # their originals - those always come back through PROP_ORIGINAL.
        if cmd == "opt_group_apply":
            from . import optimizer
            return optimizer.opt_group_apply(p)
        if cmd == "opt_group_rename":
            from . import optimizer
            return optimizer.opt_group_rename(p)
        if cmd == "opt_group_delete":
            from . import optimizer
            return optimizer.opt_group_delete(p)
        if cmd == "opt_adaptive":
            from . import optimizer
            return optimizer.opt_adaptive(p)
        if cmd == "opt_decimate":
            from . import optimizer
            return optimizer.opt_decimate(p)
        if cmd == "opt_revert_images":
            from . import optimizer
            return optimizer.opt_revert_images(p)
        if cmd == "opt_revert_meshes":
            from . import optimizer
            return optimizer.opt_revert_meshes(p)
        if cmd == "opt_regenerate":
            from . import optimizer
            return optimizer.opt_regenerate(p)
        if cmd == "opt_clear_cache":
            from . import optimizer
            return optimizer.opt_clear_cache(p)
        if cmd == "opt_estimate":
            from . import optimizer
            return optimizer.opt_estimate(p)
        if cmd == "opt_preview_start":
            from . import optimizer
            return optimizer.opt_preview_start(p)
        if cmd == "opt_preview_stop":
            from . import optimizer
            return optimizer.opt_preview_stop(p)
        if cmd == "anim_layers_add":
            return core.al_add_layer(
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"), name=p.get("name"),
                blend_type=p.get("blend_type", "COMBINE"))
        if cmd == "anim_layers_delete":
            return core.al_delete_layer(
                p["index"], data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_duplicate":
            return core.al_duplicate_layer(
                p["index"], linked=p.get("linked", False),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_rename":
            return core.al_rename_layer(
                p["index"], p["name"], sync_action=p.get("sync_action", True),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_set_state":
            return core.al_set_layer_state(
                p["index"], mute=p.get("mute"), lock=p.get("lock"),
                blend_type=p.get("blend_type"), influence=p.get("influence"),
                key_influence=p.get("key_influence", False),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_influence_animated":
            return core.al_set_influence_animated(
                p["index"], p["animated"],
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_key_influence":
            return core.al_key_influence(
                p["index"], delete=p.get("delete", False),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        # Key / un-key the selection the way Blender's own I and Alt+I do,
        # from the app. The channels are BLENDER'S choice (active keying set,
        # else the user's Default Key Channels) — see core.al_key_selection for
        # why "what the cursor is over" cannot be honoured from a timer.
        if cmd == "anim_layers_key_selection":
            return core.al_key_selection(
                delete=p.get("delete", False),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_solo":
            return core.al_solo(
                p.get("index"), data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_select":
            return core.al_select_layer(
                p["index"], data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_set_action":
            return core.al_set_layer_action(
                p["index"], p["action"],
                auto_blend=p.get("auto_blend", False),
                sync_name=p.get("sync_name", False),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_actions":
            return core.al_list_actions()
        if cmd == "anim_layers_sync_names":
            renamed = core.al_sync_layer_names(
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
            return {"renamed": renamed}
        if cmd == "anim_layers_move":
            return core.al_move_layer(
                p["index"], p["direction"],
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_select_bones":
            return core.al_select_bones_in_layer(
                index=p.get("index"), extend=p.get("extend", False),
                channels=p.get("channels"), axes=p.get("axes"),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_reset":
            return core.al_reset_layer(
                index=p.get("index"),
                selected_only=p.get("selected_only", True),
                channels=p.get("channels"), axes=p.get("axes"),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_cyclic":
            return core.al_cyclic_fcurves(
                index=p.get("index"), enable=p.get("enable", True),
                selected_only=p.get("selected_only", True),
                channels=p.get("channels"), axes=p.get("axes"),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_inbetween":
            return core.al_inbetween(
                p["amount"], index=p.get("index"),
                selected_only=p.get("selected_only", True),
                channels=p.get("channels"), axes=p.get("axes"),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_extract_bones":
            return core.al_extract_bones(
                index=p.get("index"), name=p.get("name"),
                selected_only=p.get("selected_only", True),
                channels=p.get("channels"), axes=p.get("axes"),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_share_keys":
            return core.al_share_keys(
                p["source_index"], index=p.get("index"),
                selected_only=p.get("selected_only", True),
                channels=p.get("channels"), axes=p.get("axes"),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_extract_markers":
            return core.al_extract_markers(
                index=p.get("index"), name=p.get("name"),
                selected_only=p.get("selected_only", True),
                channels=p.get("channels"), axes=p.get("axes"),
                mute_source=p.get("mute_source", True),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_influence_keys":
            return core.al_influence_keys(
                index=p.get("index"), scope=p.get("scope", "LOCAL"),
                select=p.get("select"), hide=p.get("hide"),
                mute=p.get("mute"), lock=p.get("lock"),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_adopt_nla":
            return core.al_adopt_nla(
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_clear_nla":
            return core.al_clear_nla(
                confirm=p.get("confirm", False),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_frame_range":
            return core.al_set_frame_range(
                index=p.get("index"), custom=p.get("custom"),
                frame_start=p.get("frame_start"),
                frame_end=p.get("frame_end"),
                extrapolation=p.get("extrapolation"),
                reverse=p.get("reverse"), repeat=p.get("repeat"),
                scale=p.get("scale"), sync=p.get("sync", False),
                always_sync=p.get("always_sync"),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_multikey":
            return core.al_multikey(
                op=p.get("op", "OFFSET"), value=p.get("value", 0.0),
                index=p.get("index"),
                selected_only=p.get("selected_only", True),
                selected_keys=p.get("selected_keys", True),
                channels=p.get("channels"), axes=p.get("axes"),
                pivot=p.get("pivot", "AVERAGE"), seed=p.get("seed", 0),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "anim_layers_bake":
            return core.al_bake(
                mode=p.get("mode", "NEW"),
                direction=p.get("direction", "ALL"),
                index=p.get("index"),
                bake_type=p.get("bake_type", "AL"),
                smart=p.get("smart", False),
                steps=p.get("steps", 1),
                selected_only=p.get("selected_only", False),
                merge_modifiers=p.get("merge_modifiers", True),
                clear_constraints=p.get("clear_constraints", False),
                copy_original=p.get("copy_original", False),
                data_type=p.get("data_type", "OBJECT"),
                object_name=p.get("object"))
        if cmd == "playblast":
            return core.playblast(
                p["output"], frame_start=p.get("frame_start"),
                frame_end=p.get("frame_end"),
                use_camera=p.get("use_camera", False),
                resolution_percent=p.get("resolution_percent", 50),
                overlays=p.get("overlays", False))
        if cmd == "snapshot_blend":
            return core.snapshot_blend(p.get("path"))
        if cmd == "note_render":
            # The app telling us about a render THIS Blender never made: the
            # background playblast is rendered by a separate headless process,
            # so without this the N-panel's Watch button would only ever know
            # about the blocking ones.
            core.note_last_render(p["path"])
            return {"path": p["path"]}
        if cmd == "last_render":
            return {"path": core.last_render()}
        if cmd == "begin_blend":
            ob = core.get_armature()
            data = core.load_pose_file(p["path"])
            if data.get("type") != "pose":
                raise RuntimeError("Live blend works on pose items")
            base = core.snapshot_pose(ob, list(data["bones"].keys()))
            self._blend = {"ob_name": ob.name, "data": data, "base": base,
                           "selected_only": p.get("selected_only", False)}
            return {"bones": len(base)}
        if cmd == "set_blend":
            s = self._blend
            if s is None:
                raise RuntimeError("no active blend session (begin_blend first)")
            ob = bpy.data.objects.get(s["ob_name"])
            if ob is None:
                self._blend = None
                raise RuntimeError("armature vanished during blend")
            n = core.blend_pose(ob, s["data"], s["base"], p.get("blend", 1.0),
                                selected_only=s["selected_only"])
            return {"applied": n}
        if cmd == "end_blend":
            s = self._blend
            self._blend = None
            if s is None:
                return {"ended": False}
            ob = bpy.data.objects.get(s["ob_name"])
            if ob is None:
                return {"ended": True}
            if not p.get("keep", True):
                core.restore_pose(ob, s["base"])
                return {"ended": True, "restored": True}
            keyed = 0
            if p.get("key", False):
                keyed = core.key_current_pose(ob, list(s["base"].keys()))
            return {"ended": True, "keyed": keyed}
        # ⚠ The Proxy Cage commands (cage_* , 0.5.x, seven routes) were
        # REMOVED outright on 2026-08-14 with the whole feature — cage.py is
        # gone from the package. An old app asking for one now gets the plain
        # "unknown command" error, which its own capability check prevents.
        # ------------------------------------------------ bone jiggle (0.6.0)
        if cmd == "jiggle_status":
            return jiggle.status()
        if cmd == "jiggle_get":
            return jiggle.get_settings(armature=p.get("armature"),
                                       bones=p.get("bones"))
        if cmd == "jiggle_set":
            # Only the keys actually sent are written, so an app build that
            # predates a tunable still makes a valid request.
            return jiggle.set_settings(armature=p.get("armature"),
                                       bones=p.get("bones"),
                                       settings=p.get("settings"))
        if cmd == "jiggle_enable":
            return jiggle.set_enabled(armature=p.get("armature"),
                                      bones=p.get("bones"),
                                      tip=p.get("tip"), root=p.get("root"))
        if cmd == "jiggle_copy":
            return jiggle.copy_settings(armature=p.get("armature"),
                                        source=p.get("source"),
                                        bones=p.get("bones"))
        if cmd == "jiggle_list":
            return jiggle.list_bones(armature=p.get("armature"))
        if cmd == "jiggle_select":
            return jiggle.select_jiggle_bones(armature=p.get("armature"))
        if cmd == "jiggle_object":
            return jiggle.set_object_settings(armature=p.get("armature"),
                                              settings=p.get("settings"))
        if cmd == "jiggle_scene":
            return jiggle.set_scene_settings(settings=p.get("settings"))
        if cmd == "jiggle_reset":
            return {"reset": jiggle.reset_scene()}
        if cmd == "jiggle_bake":
            return jiggle.bake(
                ob=bpy.data.objects.get(p["armature"]) if p.get("armature")
                else None,
                frame_start=p.get("frame_start"), frame_end=p.get("frame_end"),
                preroll=p.get("preroll"),
                selected_only=bool(p.get("selected_only", False)),
                action_name=p.get("action"),
                overwrite=bool(p.get("overwrite", False)))
        if cmd == "jiggle_cache":
            if p.get("clear"):
                return {"cleared": jiggle.clear_cache()}
            return jiggle.build_cache(
                ob=bpy.data.objects.get(p["armature"]) if p.get("armature")
                else None,
                frame_start=p.get("frame_start"), frame_end=p.get("frame_end"))
        # ------------------------------------------- texture bake (0.24.0)
        # Never entitlement-gated: the Node Editor was only ever locked in
        # the app, and since 2026-08-14 no prefix gate exists on this side at
        # all (docs\addon-bridge.md).
        if cmd == "list_materials":
            from . import texbake
            return texbake.list_materials()
        if cmd == "bake_targets":
            # What a bulk / all-slots run would cover — a pure read (0.26.0).
            from . import texbake
            return texbake.bake_targets(p.get("mode"),
                                        material=p.get("material"),
                                        collection=p.get("collection"))
        if cmd == "list_collections":
            from . import texbake
            return texbake.list_collections()
        if cmd == "apply_baked_material":
            # The one texbake command that WRITES to the scene (0.27.0):
            # each baked slot becomes a material showing its own map. The
            # app calls it once, after the whole bake queue has drained.
            # ⚠ `all_slots` (0.30.0) is a GROWN parameter — an older app
            # simply does not send it, and the reply echoes it so a newer
            # app can tell an older ADD-ON ignored it.
            from . import texbake
            return texbake.apply_baked_material(
                p.get("items"), all_slots=bool(p.get("all_slots")))
        if cmd == "bake_texture":
            from . import texbake
            # ⚠ NATIVE since 0.29.0 — Blender's whole Bake panel, passed
            # through. All grown parameters, so the reply's `options` block
            # is what the app checks instead of a capability name. An old
            # app still sending `device`/`denoise` (0.28.x) is ignored
            # gracefully: those keys are simply not read any more.
            return texbake.bake_texture(
                material=p.get("material"),
                bake_type=p.get("bake_type"),
                width=p.get("width"), height=p.get("height"),
                out_path=p.get("out_path"),
                object_name=p.get("object"),
                samples=p.get("samples"),
                margin=p.get("margin", 16),
                margin_type=p.get("margin_type", "ADJACENT_FACES"),
                use_clear=p.get("use_clear", True),
                target=p.get("target", "IMAGE_TEXTURES"),
                pass_filter=p.get("pass_filter"),
                view_from=p.get("view_from", "ABOVE_SURFACE"),
                normal_space=p.get("normal_space", "TANGENT"),
                normal_swizzle=p.get("normal_swizzle"),
                use_selected_to_active=p.get("use_selected_to_active",
                                             False),
                use_cage=p.get("use_cage", False),
                cage_object=p.get("cage_object"),
                cage_extrusion=p.get("cage_extrusion", 0.0),
                max_ray_distance=p.get("max_ray_distance", 0.0),
                view_transform=p.get("view_transform", False))
        # --- assets: build a node group + object from a spec the APP sends.
        # The spec is the product; this add-on only follows it (assets.py).
        if cmd == "asset_build":
            from . import assets
            return assets.build_asset(p["spec"], collection=p.get("collection"))
        if cmd == "asset_status":
            from . import assets
            return assets.asset_status(p["object"], p["modifier"])
        if cmd == "addon_status":
            from . import selfupdate
            return selfupdate.status()
        if cmd == "addon_update":
            # ⚠ THE ONLY COMMAND THAT INSTALLS CODE, so it is the only one that
            # asks who is calling. A caller must prove it can read
            # bridgeauth.token_path(), which a web page cannot - and a web page
            # could reach this port until 2026-08-06 (see _client_loop).
            # Everything else here is still open to any local process, which is
            # the documented trade; installing an extension is not.
            if not bridgeauth.check(p.get("auth")):
                raise RuntimeError(
                    "add-on updates must come from the Toolset app. If this WAS "
                    "the app, restart Blender's bridge so it can re-read the "
                    "token, then try again.")
            # Verifies and SCHEDULES; the install happens on a timer about a
            # second later. It must not happen here: this add-on owns the socket
            # this reply is about to go out on, and reloading would cut it.
            # See selfupdate.py.
            from . import selfupdate
            return selfupdate.stage(p["path"], version=p.get("version"),
                                    sha256=p.get("sha256"))
        raise RuntimeError("unknown command: %r" % cmd)


# Module-level singleton
server = BridgeServer()
