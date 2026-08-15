# FULL-APP regression — every suite, Blender-side then app-side. Covers Anim
# Layers (the original regression) plus, since 2026-08-02: Studio Library
# blend, Denoising setup (both modes + undo), Node Setup tools, Render Queue
# and the playblast preview cache.
# Nothing here touches Marty's open .blend, the real config.json or the real
# render_queue\ data: the Blender suites run in a separate factory-startup
# instance, the app suites run offscreen against stubs/temp dirs.
#
#   powershell -ExecutionPolicy Bypass -File tests\run_all.ps1
#
# ASCII only (PS 5.1 reads .ps1 as ANSI - a UTF-8 dash kills the script).

$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $here

# Blender lives wherever you installed it - set $env:MADI_BLENDER to override.
$blender = $env:MADI_BLENDER
if (-not $blender) {
    $blender = "D:\Program Files\Blenderlauncher\stable\blender-5.2.0-lts.fbe6228777e7\blender.exe"
}
$python = Join-Path $root "app\.venv\Scripts\python.exe"

if (-not (Test-Path $blender)) { Write-Host "Blender not found: $blender"; exit 1 }
if (-not (Test-Path $python))  { Write-Host "app venv not found: $python"; exit 1 }

# batch order: 1 (status/CRUD/select/state), influence fix, 2 (bake),
# 3 (layer tools), 4 (multikey/range/NLA/influence keys), 5 (shape keys,
# settings)
$blenderSuites = @(
    "test_al_status.py", "test_al_crud.py", "test_al_select.py",
    "test_al_state.py", "al_infl_test.py",
    "al_bake_test6.py", "al_bake_test7.py", "al_bake_test8.py",
    "al_bake_test9.py",
    "al_tools_test11.py", "al_tools_test12.py", "al_tools_test13.py",
    "al_tools_test14.py",
    "al_test16_multikey.py", "al_test17_range.py", "al_test18_nla.py",
    "al_test19_influence.py",
    "al_test21_shapekeys.py", "al_test23_settings.py",
    # full-app suites (added 2026-08-02): library blend, denoise, node tools
    "sl_blend_test.py",
    "dn_layers_test.py", "dn_passes_test.py", "dn_undo_test.py",
    "nt_tools_test.py",
    # Proxy Cage: numpy maths, the pure-bmesh remesher, and the whole
    # pipeline end to end on a generated rig (builds, binds and SIMULATES).
    # Slowest suite here - roughly a minute.
    # Bone Jiggle: solver maths, analytic colliders, chain composition, every
    # tunable's visible effect, collision, lateral links, wind, cache
    # invalidation and the two-pass bake.
    "jiggle_solver_test.py",
    # bridge plumbing: adaptive tick + add-on/app version handshake.
    # NOTE: prints an intentional "RuntimeError: nope" traceback (the
    # failing-handler check) - that is expected, watch the counts.
    "bridge_version_test.py",
    # NSFW Tools: the Affector Torus rig built from the embedded spec, and
    # proven to actually deform (not just to have the right node count).
    "nsfw_asset_test.py",
    # Anim Layers inside Blender: the N-panel registers, every operator it
    # references exists, draw() writes nothing, and the settings it shares with
    # the app name the same keys on both sides.
    "al_panel_test.py",
    # Bone picker: registration as a package submodule (incl. the prefs lookup
    # that fails SILENTLY if it regresses), handler hygiene across a reload,
    # the ABSENCE of any licence gate (removed 2026-08-15), the picker_* API, and
    # the .picker library item round-trip.
    "picker_test.py",
    # Scene Optimizer: real files through real OpenImageIO - the stand-in
    # cache, sequences and UDIM tiles, never-upscale and never-copy-a-copy,
    # revert, tamper detection, the camera projection maths, the decimate
    # stale sweep, the linked-library veto, and the gate's ABSENCE on the bridge.
    "optimizer_test.py",
    # Vertex groups: store, exact restore (and its refusal on a vertex-count
    # mismatch), and the spatial transfer onto a different mesh.
    "vgroups_test.py",
    # Keying from the app: Blender's own I / Alt+I through a VIEW_3D override,
    # the shape-key branch end to end, and every refusal as a sentence.
    "al_keying_test.py",
    # The Save Shape Keys checklist source. It had NO Blender-side coverage and
    # raised on 5.x the moment a shape key was keyframed (Action.fcurves).
    "shapes_list_test.py",
    # Export Abc options: every default checked against Blender's OWN RNA, the
    # sanitiser that stops a bad value taking the whole export down, real
    # exports, and the app's copy of the table proven not to have drifted.
    "abc_export_test.py",
    # Weight-paint previews: the real mode switch and the real restore, with
    # only the GL write stubbed (background Blender has a viewport but no
    # OpenGL context, so render.opengl refuses outright).
    "vgroup_preview_test.py",
    # Texture bake (Node Editor's fast engine): real CPU bakes of the map
    # types, the pass_filter/sRGB/UV-rewire traps pinned, full restore.
    "texbake_test.py",
    # Render presets: the catalogue's ORDER (engine first, media_type before
    # file_format, view transform before look), capture -> JSON -> apply round
    # trips, and the whitelist that stops an edited preset file writing
    # anything reachable from the scene.
    "render_presets_test.py",
    # Save Anim's two new options (F-modifiers, bone properties) incl. the
    # property-only bone that used to vanish through a mirror, previews shot
    # with overlays OFF and put back, where Blender says its renders go, and
    # the last-render record the app and the add-on SHARE.
    "anim_options_test.py",
    # Timeline markers: the two facts that came from measuring Blender (a
    # marker refuses ID properties but KEEPS a registered bpy.props value, and
    # marker names are not unique), marker_list proven to be a PURE read, and
    # the panel drawing without writing.
    # NOTE: prints "Not freed memory blocks: 8" as Blender quits - a teardown
    # artifact of the script, not a leak in the add-on. Watch the counts.
    "markers_test.py",
    # abuse defences (2026-08-06): the cross-protocol hole that let a WEB PAGE
    # drive the bridge, the addon_update token, and the jiggle cache that used
    # to unpickle files sitting next to a downloaded .blend
    "bridge_security_test.py",
    # Quadify: the pipeline end to end on Suzanne through the REAL engine, the
    # evaluated triangle count (the label that read 2,424 for a 266,469-tri
    # job), the progress record's shape, the socket-thread routing for
    # progress/cancel, and the proof that Blender's main thread stays free
    # during a run. Roughly half a minute - it runs the engine twice.
    "quadify_test.py"
)
$appSuites = @(
    "al_app_test10.py", "al_app_test15.py", "al_app_layout.py",
    "al_slider_test.py", "al_test20_app.py", "al_test22_datatype.py",
    "al_test23b_app.py",
    # full-app suites (added 2026-08-02): node tab, render queue, vid cache
    "app_nodetab_test.py", "app_rq_test.py", "app_vidcache_test.py",
    # Physics tab: option plumbing, cage manager, capability gate, threading
    # Bone Jiggle panel: only-what-you-touched pushes, degrees<->radians,
    # debounce, both gates, threaded bake
    "app_jiggle_test.py",
    # shell polish: readable rail headers, Physics rail order, the global
    # wheel guard (every tab), the always-on-top pin
    "app_ui_test.py",
    # the section rail that replaced the top tab strip (2026-08-14): the
    # rail/tab seam in both directions, grouping, the tab text staying the
    # key under a rename, drawn glyphs, and no emoji back on a button
    "app_rail_test.py",
    # our own title bar in place of Windows' (2026-08-15): the hit test that
    # decides whether the window buttons get their clicks, the rail header
    # not becoming the window's minimum width, and the fallback
    "app_chrome_test.py",
    # performance ceilings (2026-08-15, PERF_PLAN.md). Asserts on WORK DONE -
    # widgets built, event-filter calls, full-item reads, modules imported -
    # never on wall-clock time, which is not comparable between machines
    "perf_bench.py",
    # library auto-refresh watcher (slow-ish: real debounce waits)
    "app_autorefresh_test.py",
    # update-safety contract (capability negotiation) + developer console
    "app_compat_test.py",
    # Developer mode: edit - right-click renaming, path|original keys, the
    # store, and that renames stay applied while the mode is off
    "app_devedit_test.py",
    "app_theme_test.py",
    # Node Editor tab: the test graph (reroute dots, the multi-input
    # socket), wires tracking moves, curving through devedit's field incl.
    # the 0-vs-None rule, selection outlines, zoom clamps - plus the theme
    # checkbox tick from the same batch
    "app_nodeeditor_test.py",
    # MadiRef: the time-based frame mapping (the whole point), the .mrfx
    # container incl. its crash-safety rule, the shared-memory ring + seqlock,
    # the audio playback-vs-scrub inference, and the two things that go stale
    # silently - the ring layout duplicated in the add-on, and the gate that
    # keeps an older add-on from breaking the tab
    "app_madiref_test.py",
    # licensing: Ed25519 verify (RFC vectors), fingerprint, DPAPI storage,
    # the state machine, and the four gated tabs locking / unlocking live
    # self-update: version comparison (which IS the anti-rollback rule, since a
    # manifest served from disk has no nonce), the signed manifest and the swap
    "app_addon_push_test.py",
    # Bone picker tab: the poll, the list-rebuild echo guard, retargeting bones
    # / group members / shape keys, the debounce, the capability gate
    "app_picker_test.py",
    # smooth scrolling: per-pixel item views, the glide (first frame synchronous),
    # a nested view not swallowing a wheel it cannot use, and the older
    # "wheel never edits" guarantee still holding with both filters installed
    "app_scroll_test.py",
    "app_zip_test.py",
    "app_updates_test.py",
    "app_shapes_test.py",
    # Optimization tab: the poll, the settings store (incl. not writing while
    # the lock preview builds), the confirmation before a long run, which
    # targets each tool offers, and the capability gate
    "app_optimizer_test.py",
    # Quadify's control: what a run sends, the evaluated count on the label,
    # the report painted ONLY from the reply, and the check that fails if the
    # run is ever re-wrapped in begin_capture (which greys the whole app).
    "app_quadify_test.py",
    # Blend file size: the .blend block reader against two REAL files (the same
    # scene saved compressed and not), the exact-total invariant that is the
    # only guard against a mis-read block header, and the tree window
    "app_blendsize_test.py",
    # Markers tool (option D), app side: the two rules the shape exists for -
    # a poll must not overwrite the field being typed in (a rebuild re-selecting
    # the same row used to walk straight past the focus check), and only the
    # field that changed is sent. Plus the import reader and the free-tab gate.
    "app_markers_test.py",
    # Set / Remove Keyframe in the Anim Layers tab: the channels are BLENDER'S
    # choice and the status line has to report what came back, not what was asked
    "app_keying_test.py",
    # Super focus: the decision (anything unrecognised is left alone), the poll,
    # and the tickbox in the status bar
    "app_superfocus_test.py",
    # The Export Abc options dialog: every option reachable, the values it hands
    # back, the greying, the frame range, and that the choices are remembered
    "app_abc_test.py",
    # Vertex groups, app side: the type filter that was hiding the items
    # outright, the Save picker, and the weight-paint preview's gate
    "app_vgroups_test.py",
    # Render presets, app side: the store on disk (incl. a folder a user can
    # drop junk into), the save dialog's ticks, the verbs, and the gate putting
    # the buttons BACK when a newer add-on arrives mid-session
    "app_render_presets_test.py",
    # Importing into the library, and the picker thumbnail that shows its
    # buttons. The two that matter: a zip with BACKSLASH entry names (what
    # PowerShell 5.1 writes) and one trying to climb out with '..'.
    "app_import_test.py",
    # Marty's 2026-08-05 anim/playblast batch, app side: the Save Anim options
    # dialog, the tile badges, the playblast defaults and output folder, both
    # Watch buttons, and the system monitor that only ticks while on screen.
    "app_anim_options_test.py",
    # The docs police themselves: the router routes, every module is in the
    # FILE -> DOC map, HANDOFF and RESUME_PROMPT stay under their caps, the
    # volatile numbers live in ONE place, and PITFALLS.md is not stale.
    "docs_test.py"
)

$totalPass = 0
$totalFail = 0
$failed = @()

function Report($name, $lines) {
    $summary = $lines | Select-String -Pattern "^\d+ passed, \d+ failed"
    if ($summary) {
        $m = [regex]::Match($summary[0].ToString(), "(\d+) passed, (\d+) failed")
        $p = [int]$m.Groups[1].Value
        $f = [int]$m.Groups[2].Value
        $script:totalPass += $p
        $script:totalFail += $f
        if ($f -gt 0) { $script:failed += $name }
        $mark = if ($f -gt 0) { "FAIL" } else { "ok  " }
        Write-Host ("{0} {1,-26} {2,4} passed, {3} failed" -f $mark, $name, $p, $f)
        if ($f -gt 0) {
            $lines | Select-String -Pattern "^FAIL " | ForEach-Object {
                Write-Host "       $_"
            }
        }
    } else {
        $script:failed += $name
        Write-Host ("FAIL {0,-26} no summary - suite crashed" -f $name)
        $lines | Select-Object -Last 6 | ForEach-Object { Write-Host "       $_" }
    }
}

Write-Host "=== Blender-side (factory startup, 5.2.0 LTS) ==="
# ⚠ THROWAWAY LOCALAPPDATA for every Blender suite. `bridgeauth.token_dir()`
# resolves from LOCALAPPDATA at call time, so ANY suite that starts the
# add-on's bridge - directly, or by installing and letting autostart fire -
# writes and then DELETES the token file belonging to Marty's live Blender.
# That is not a hypothetical: it cost three refused add-on pushes and two
# write-ups of a "cause not established" bug before it was caught
# (docs\security.md §5, docs\testing.md). Isolating the PORT was never
# enough; the filesystem is shared too.
$realLocalAppData = $env:LOCALAPPDATA
$tokenSandbox = Join-Path $env:TEMP ("madi_localappdata_" + [guid]::NewGuid().ToString("N").Substring(0,8))
New-Item -ItemType Directory -Force $tokenSandbox | Out-Null
$env:LOCALAPPDATA = $tokenSandbox

foreach ($s in $blenderSuites) {
    $path = Join-Path $here $s
    if (-not (Test-Path $path)) { Write-Host "SKIP $s (missing)"; continue }
    $out = & $blender -b --factory-startup --python $path 2>&1
    Report $s $out
}

Write-Host ""
Write-Host "=== Add-on self-update (ISOLATED Blender user resources) ==="
# This suite installs a dummy extension, so it runs with BLENDER_USER_RESOURCES
# pointed at a throwaway folder - otherwise it would land in Marty's real
# Blender. The suite ALSO refuses to install unless both variables are set, so
# running the file by hand can never pollute anything.
$isolated = Join-Path $env:TEMP ("madi_addon_test_" + [guid]::NewGuid().ToString("N").Substring(0,8))
New-Item -ItemType Directory -Force $isolated | Out-Null
$env:BLENDER_USER_RESOURCES = $isolated
$env:MADI_ADDON_INSTALL_TEST = "1"
$out = & $blender -b --factory-startup --python (Join-Path $here "addon_update_test.py") 2>&1
Report "addon_update_test.py" $out
Remove-Item $env:BLENDER_USER_RESOURCES -Recurse -Force -ErrorAction SilentlyContinue
$env:BLENDER_USER_RESOURCES = $null
$env:MADI_ADDON_INSTALL_TEST = $null

# the Blender half is done - hand LOCALAPPDATA back before the app suites,
# which legitimately read the real one
if (Test-Path (Join-Path $tokenSandbox "MadihsonNSFW Toolset\bridge.token")) {
    Write-Host "note: a Blender suite wrote a bridge token - it landed in the sandbox, as intended"
}
Remove-Item $tokenSandbox -Recurse -Force -ErrorAction SilentlyContinue
$env:LOCALAPPDATA = $realLocalAppData

Write-Host ""
Write-Host "=== App-side (offscreen Qt, stub bridge) ==="
$env:QT_QPA_PLATFORM = "offscreen"
foreach ($s in $appSuites) {
    $path = Join-Path $here $s
    if (-not (Test-Path $path)) { Write-Host "SKIP $s (missing)"; continue }
    $out = & $python $path 2>&1
    Report $s $out
}

Write-Host ""
Write-Host "=== App smoke ==="
Push-Location (Join-Path $root "app")
& $python "main.py" "--smoke"
$smoke = $LASTEXITCODE
Pop-Location
Write-Host "main.py --smoke exit $smoke"

Write-Host ""
Write-Host ("TOTAL: {0} passed, {1} failed" -f $totalPass, $totalFail)
if ($failed.Count -gt 0) {
    Write-Host ("Suites with failures: " + ($failed -join ", "))
    exit 1
}
if ($smoke -ne 0) { Write-Host "smoke failed"; exit 1 }
exit 0
