"""Prove a rebuilt exe really contains the current source, by reading its OWN bytecode.

    app\\.venv\\Scripts\\python.exe tools\\verify_exe.py

Why this exists: **the exe stub's timestamp lies and `--smoke` passes on a stale
build**, so neither proves a rebuild took. Startup timing only discriminates when
the change has a timing signature. The only check that always works is to open the
build, pull its compiled modules out and look for code that only the new source has.
This has caught two silently-skipped rebuilds.

Written up in `docs\\app-shell.md`. It used to be re-derived from scratch every
time; it lives here now so it stops being rediscovered.

Two traps it has to handle, both of which have cost real time:

⚠ **Descend into TUPLE constants.** CPython folds `return None, "some_reason"`
into ONE tuple const, so a walk that only collects `isinstance(c, str)` from
`co_consts` cannot see the string at all - and a perfectly good build reads as
stale. That cost 20 minutes on the 2026-08-03 rebuild (24/26 "missing", both in
the same module, every one a tuple-returned reason string).

⚠ **Match on SUBSTRINGS, not set membership.** Adjacent string literals are
concatenated at compile time, so `"Added %s. " "Its settings are on the %s"` is a
single constant and an exact-match probe for the second half misses it.

⚠ **Retire an absence check when the string comes back under a new meaning.** The
verifier once held both `("nsfw", "Add Affector Torus", True)` and the same marker
with `False`, so exactly one of them failed on every build no matter what the exe
contained.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DIST = os.path.join(ROOT, "app", "dist")
_NAME = "MadihsonNSFW Toolset"

# ⚠ Getting these paths wrong does not fail loudly — the walk simply finds
# nothing and every marker "passes" against an empty archive, which is the
# worst possible outcome for a verifier. `main()` refuses outright when the
# binary is missing, so a wrong path is a stop, not a pass.
# ⚠ Windows only since 2026-08-17; the macOS `.app`/`Frameworks` and Linux
# branches went with the cancelled port.
EXE = os.path.join(_DIST, _NAME, _NAME + ".exe")
INTERNAL = os.path.join(_DIST, _NAME, "_internal")
NATIVE_SUFFIX = ".pyd"

# (module, marker, must_be_present)
MARKERS = [
    # --- Marty's 2026-08-06 follow-ups -----------------------------------
    # The anim paste moved off the GUI thread, and the app grew an icon. Both
    # are things a stale build shows perfectly while behaving like the old one:
    # the freeze would still be there and the icon would still be Qt's default.
    ("main", "apply_anim_flow", True),
    ("main", "app_icon", True),
    ("main", "app_icon.ico", True),
    # --- the abuse pass (2026-08-06) -------------------------------------
    # ⚠ These are the ones where a stale build is a SECURITY problem rather
    # than a cosmetic one: an exe without them cannot authenticate itself to
    # the add-on, so "Update add-on" would fail against 0.22.0 with a message
    # about a token the old build has never heard of.
    ("bridge", "bridge_token", True),
    ("bridge", "BRIDGE_TOKEN_FILE", True),
    # ⚠ A VERSION STRING, i.e. the marker shape that dies when the version
    # moves. Kept because it is exactly what a stale build gets wrong, and
    # validated against the source before this build - "0.20.0" survives
    # alongside it only because it is a real since_version in
    # FEATURE_REQUIREMENTS, not because it is the expected version.
    ("bridge", "0.22.0", True),
    # --- three tabs freed (2026-08-06) -----------------------------------
    # ⚠ The gated set went from SIX to THREE. A stale build would still be
    # showing lock panels on Bone picker / Anim Layers / Node Setup, which is
    # the most visible thing in this batch and the easiest to ship wrong.
    ("main", "FREE_TOOLS", True),
    ("main", "Bone picker", True),
    ("main", "Node Setup", True),
    # The blurbs belong to GATED entries only, so the two that were freed must
    # have taken their lock-panel text with them.
    ("main", "AnimSchool-style 2D picker", False),
    ("main", "Animate in layers over the NLA", False),
    ("main", "Relink whole node trees", False),
    # ⚠ ...and since 2026-08-14 EVERY blurb is gone: GATED is empty, all four
    # paid tabs were freed (premium packs are the paid thing now). This was
    # a PRESENCE marker while Optimization was paid — the same string, the
    # other way round, is what proves the freeing actually shipped.
    ("main", "Fit a heavy scene into memory", False),

    # --- annual licences, open updates, About (2026-08-06) ---------------
    # Marty's links. If either of these is missing, the About box ships with a
    # dead button and the only route to bug reports is gone.
    ("version", "https://discord.gg/EPcgrRkdhe", True),
    ("version", "https://www.patreon.com/c/MadihsonNSFW", True),
    ("main", "About, Discord and Patreon", True),
    ("main", "Discord  (report a bug)", True),
    ("main", "Patreon  (support)", True),

    # ⚠ THE LICENCE SERVER URL, WHICH NO LONGER LIVES IN TRACKED SOURCE.
    # `licensing\endpoint.py` is local-only, so a build made from a fresh clone
    # — or from a tree where that file was lost — packages an app whose
    # SERVER_URL is the empty string. It starts perfectly, every tab works
    # (they are all free), and the ONLY symptom is that signing in can never
    # succeed. Nothing else in this file would catch it.

    # Expiry: the field, the state and the wording that must NOT be the
    # revoked wording.

    # ⚠ THE ONE-ENV-VAR BYPASS BEING SHUT. The variable NAME is still read, so
    # its presence proves nothing about the ordering - this matches a phrase
    # from the fixed `is_gated()` docstring instead, which only the new source
    # has. (Docstrings survive: build_exe.ps1 does not pass -OO.)

    # ⚠ THE CRASH FIX. Without it this exe ABORTS on `--smoke` (0xC0000409),
    # because the every-launch licence check is still mid-request when the run
    # returns and Qt kills the process for destroying a live QThread. That is
    # not cosmetic: `swap.smoke()` uses the exit code to decide whether to KEEP
    # an update, so a build missing this would roll back every update forever.
    ("main", "shutdown_workers", True),

    # ⚠⚠ THE 1.19.0 REMOVALS. The self-updater and the WHOLE licensing
    # subsystem were deleted. A stale build carries both and looks completely
    # normal — it would still phone a server on launch, still show a licence
    # chip and an Updates button, and still try to install a release over
    # itself. These absence markers are the only thing that catches that.
    ("main", "update_license_chip", False),
    ("main", "_manual_update_check", False),
    ("main", "_push_license", False),
    ("main", "Check for updates automatically", False),
    ("bridge", "license_unlock", False),
    ("config", "auto_update", False),
    # …and the one local operation that SURVIVED the removal. If this is
    # missing, "Update add-on" is gone and there is no way to get the Blender
    # half out of the app at all.
    ("addon_push", "AddonPusher", True),
    ("addon_push", "install_bundled_addon", True),
    ("main", "addon_pusher", True),
    # ⚠ The two-Blender verdict, which is the hardest-won message in the
    # module and the easiest thing to lose while moving code between files.
    ("addon_push", "addon_other_blender", True),
    ("addon_push", "the bridge in the Blender you want updated", True),

    # --- the Bone picker tab (2026-08-04) --------------------------------
    ("picker", "PickerTabsTool", True),
    ("picker", "PickerButtonsTool", True),
    ("picker", "PickerPresetsTool", True),
    ("picker", "PickerOptionsTool", True),
    ("picker", "picker_status", True),
    ("picker", "bone_picker", True),
    # the poll must be flagged, or a dead bridge stalls the GUI thread
    ("picker", "poll", True),
    # retargeting needs the rig's bones, which only the new status carries
    ("picker", "bones", True),
    ("picker", "member_index", True),
    ("bridge", "picker_status", True),
    ("bridge", "picker_apply_item", True),
    ("bridge", "bone_picker", True),
    # ⚠ RETIRED as a build marker. "0.10.0" is still in bridge.py forever — it
    # is the picker's FEATURE_REQUIREMENTS version — so it passed on every
    # build regardless of what was in it and discriminated nothing. The live
    # baseline is checked below instead.
    ("bridge", "EXPECTED_ADDON_VERSION", True),
    ("main", "Bone picker", True),
    ("main", "_build_picker", True),
    ("main", "current_library_root", True),
    ("library", ".picker", True),
    ("theme", "#8f7ae0", True),
    # --- smooth scrolling (2026-08-04) -----------------------------------
    ("widgets", "SmoothScroller", True),
    ("widgets", "tune_scroll_widget", True),
    ("widgets", "_madi_scroll_tuned", True),
    ("main", "install_smooth_scroll", True),
    # --- the tab strip ---------------------------------------------------
    # NSFW Tools no longer has to be last: SectionTabBar paints the tint by
    # NAME. What must be present is the painter itself, or the pink silently
    # goes missing everywhere.
    ("main", "NSFW Tools", True),
    ("main", "SectionTabBar", True),
    ("theme", "TAB_TINTS", True),
    ("theme", "PREMIUM_MARK", True),
    # --- 2026-08-05, Marty's feature batch -------------------------------
    ("bridge", "opt_group_apply", True),
    ("bridge", "apply_vgroups", True),
    ("optimizer", "Texture sets", True),
    ("optimizer", "ProgressRow", True),
    ("optimizer", "opt_progress", True),
    ("main", "save_vgroups_flow", True),
    ("main", "transfer_vgroups", True),
    ("main", "zip_items", True),
    ("main", "save_shapes_separately", True),
    ("main", "show_library_settings", True),
    ("updates", "UpdatesPage", True),
    ("updates", "CHANGELOG.md", True),
    ("library", ".vgroups", True),
    ("panels", "Save Vertex Groups", True),
    ("devedit", "ROUNDABLE", True),
    ("render_deck.util", "disable_collections_expr", True),
    # ⚠ hide_render, NEVER LayerCollection.exclude — excluding drops objects
    # out of the depsgraph and breaks anything depending on them.
    ("render_deck.util", "hide_render", True),
    # --- 2026-08-05, the queue fixes -------------------------------------
    # ⚠ The version marker is the one that goes stale silently — it passed
    # 32/32 once against a version string that had not moved. Move it every
    # time EXPECTED_ADDON_VERSION moves, and check the OLD one is gone.
    ("bridge", "opt_clear_cache", True),
    ("optimizer", "_enqueue_selection", True),
    ("optimizer", "_queue_renamed", True),
    ("optimizer", "Clear cache folder", True),
    # --- 2026-08-05, Rendering freed + keying + Super focus ---------------
    ("bridge", "0.16.0", True),
    ("bridge", "0.15.0", False),
    ("main", "AbcExportDialog", True),
    ("main", "evaluation_mode", True),
    ("bridge", "anim_layers_key_selection", True),
    ("anim_layers", "Set Keyframe", True),
    ("anim_layers", "Remove Keyframe", True),
    ("superfocus", "GHOST_WindowClass", True),
    ("superfocus", "AttachThreadInput", True),
    ("main", "Super focus", True),
    # ⚠ Rendering is FREE now, so its blurb must NOT be in the gated tuple any
    # more. An absence check is the only way to see that from bytecode - the
    # tab itself is still there either way.
    ("main", "queue renders that run", False),
    # --- 2026-08-05, Marty's second batch ---------------------------------
    ("main", "chk_scene_range", True),
    ("main", "SaveVGroupsDialog", True),
    ("main", "_start_vgroup_capture", True),
    ("bridge", "capture_vgroup_preview", True),
    # ⚠ The two item types that were INVISIBLE until 2026-08-05: absent from
    # the sidebar's filter list, and refilter() drops anything not in it. The
    # bytecode check is the only place a packed build can be asked.
    ("panels", "vgroups", True),
    ("panels", "picker", True),
    ("main", "save_vgroups_separately", True),
    ("grid", "_stamp_bulk", True),
    ("library", "_BULK_UNITS", True),
    # --- 2026-08-05, Render presets ---------------------------------------
    ("bridge", "0.17.0", True),
    # ⚠ NO absence check for "0.16.0" here, unlike the pattern above: it is
    # still a legitimate string in bridge.py (the vgroup_preview requirement
    # names it), so asserting it is gone would fail on a perfectly good build.
    # `render_preset_capture` is what proves this batch is in the exe.
    ("bridge", "render_preset_capture", True),
    ("bridge", "render_presets", True),
    ("render_presets", "SavePresetDialog", True),
    ("render_presets", "RenderPresetsTool", True),
    ("render_presets", "render_presets", True),   # the folder next to the exe
    ("main", "Render presets", True),
    # --- 2026-08-05, presets as Studio Library items ----------------------
    # ⚠ "0.18.0" AND "0.19.0" USED TO BE MARKERS HERE AND WERE RETIRED
    # 2026-08-06. A version-string PRESENCE marker only means anything while it
    # IS `EXPECTED_ADDON_VERSION`: the moment that constant moves on, the string
    # leaves bridge.py and the marker becomes a guaranteed failure that says
    # nothing about the exe. The mirror of the absence-check lesson below.
    # ⚠ "0.17.0" is the exception and stays PRESENT: it is the since-version
    # inside the render_presets requirement, so it is a legitimate string
    # forever. That is the ONLY reason an old version string survives here.
    ("render_presets", "write_library_item", True),
    ("render_presets", "SaveToLibraryDialog", True),
    ("library", "renderpreset", True),
    ("panels", "renderpreset", True),
    ("grid", "TYPE_LABELS", True),
    ("main", "Apply Render Settings", True),
    # --- 2026-08-05, Import + picker thumbnails ---------------------------
    ("importer", "scan_zip", True),
    ("importer", "BARE_EXTS", True),
    ("main", "ImportDialog", True),
    ("main", "import_flow", True),
    ("picker", "compose_thumbnail", True),
    ("main", "_note_connected_file", True),
    ("main", "different Blender", True),
    ("picker", "reference.jpg", True),
    ("library", "reference.jpg", True),
    # --- 2026-08-06, Marty's anim / playblast batch -----------------------
    # ⚠ Without these, a rebuild would verify 101/101 and prove only that
    # YESTERDAY's code shipped — which is the exact "the markers went stale"
    # failure this tool exists to avoid.
    ("bridge", "0.20.0", True),
    ("bridge", "note_render", True),
    # ⚠ "0.19.0" is NOT asserted absent: it would be a legitimate since-version
    # string the moment anything gates on it, and an absence check that has to
    # be retired later is how the contradictory pair got left behind in 2026-08-04.
    ("main", "SaveAnimDialog", True),
    ("main", "ask_anim_options", True),
    ("main", "anim_layer_warning", True),
    ("main", "keep_modifiers", True),
    ("main", "watch_last_render", True),
    ("main", "sync_watch_button", True),
    ("lastrender", "last_render.json", True),
    ("lastrender", "state_dir", True),
    ("library", "_peek_metadata", True),
    ("library", "ANIM_FLAGS", True),
    ("grid", "_stamp_flags", True),
    ("theme", "WARN", True),
    ("anim_layers", "Merge / Bake", True),
    # ⚠ The removals from this batch, which a stale build would still carry:
    # the bake tick left the info panel for the dialog, and a loose .mp4 is no
    # longer scanned as an item.
    ("panels", "Bake every frame", False),
    ("library", "playblast", False),
    # --- 2026-08-07, the Node Editor becomes real -------------------------
    # Grid/zoom (2026-08-06, never marked — the exe skipped that batch),
    # Ctrl+drag cutting, the smooth pan, and the BAKE node set.
    # ⚠ "0.24.0" is the since_version of texture_bake, so it is a legitimate
    # string forever (the 0.17.0 rule) — not just today's EXPECTED version.
    ("bridge", "0.24.0", True),
    ("bridge", "bake_texture", True),
    ("bridge", "texture_bake", True),
    ("nodecanvas", "grid_spacing", True),
    ("widgets", "_madi_wire_canvas", True),
    ("nodecanvas", "wires_crossing", True),
    ("nodecanvas", "_pan_carry", True),
    ("nodecanvas", "_BakeTask", True),
    ("nodecanvas", "run_bake", True),
    ("bakenodes", "Ambient Occlusion", True),
    ("bakenodes", "upstream_node", True),
    # the add-on bundle rides in the exe, addressed by its hash — ⚠ this
    # marker moves with every repack; validate against addon_bundle.py
    # before building (now 0.43.0: real hiding — a shown layer parks the other
    # markers so Blender's timeline strip clears — plus named marker sets in
    # the .blend and the B1 sub-panel split; on top of 0.42.0's viewport panel,
    # 0.41.0's layers, 0.40.0's timeline markers and 0.39.0's no-autostart)
    # ⚠ MOVES WITH EVERY REPACK of the add-on — update it whenever
    # tools\pack_addon.py runs, or the build fails on a marker that is only
    # out of date. 0.46.0's pack (all tabs free, gates out, cage.py GONE —
    # 46 files). ⚠⚠ The pinned-absent one (`15758d6b…`) is the package with
    # the UTF-8 BOM on its manifest, which Blender REFUSES while
    # `package_install_files` still returns {'FINISHED'} — a build carrying
    # that hash ships an add-on nobody can install and nothing reports it.
    # ⚠⚠ SET THIS FROM WHAT THE REPO STORES, NEVER FROM A WORKING TREE.
    # `10fd4f45…` sat here until 2026-08-17 and was WRONG on every machine but
    # one: `picker.py` happened to have CRLF locally while the other fourteen
    # files had LF, and `text=auto` makes git call such a tree CLEAN, so
    # nothing anywhere hinted at it. `pack_addon.py` reads bytes verbatim, so
    # that one file put 5511 extra bytes (one per line) into the zip and moved
    # the hash. The marker was therefore derived from a local accident — it
    # failed on the Windows runner and would have failed in any clone.
    # `blender_addon/** eol=lf` in `.gitattributes` now keeps the checkout
    # identical everywhere; this value is the packed sha of that canonical
    # source, reproduced byte for byte by the runner.
    # 0.49.0 — Quadify RESTORED, so the bundle is 45 files / 2.7 MB again
    # (the engine\ folder is most of it) rather than the 15-file / 294 KB
    # pack that 8d501dbd… was. ⚠ Verified LF-clean before pinning: every text
    # file under `blender_addon\` was checked for CRLF, because that is what
    # moved this hash the last time (see the block above the desktop markers).
    # 0.52.0 — Rig properties adds one module, so 48 files / 2,729 KB.
    # ⚠ Verified LF-clean before pinning, as the note above requires.
    # 0.55.0 - Quadify's preserve adds `quadpreserve.py`, so 49 files.
    # ⚠ A NEW ADD-ON FILE MOVES THIS HASH *AND* NEEDS `pack_addon.py`
    # re-run: `addon_bundle.py` carries an explicit FILES list, and a module
    # missing from it ships an add-on whose `quadify` import fails outright.
    # 0.56.0 - adds `bakedeform.py`, so 50 files.
    # 0.57.0 - "Delete all shape keys" adds a ROUTE, not a file: still
    # 50 files, but the hash moves because `server.py` and
    # `bakedeform.py` both changed.
    # 0.58.0 - the Quadify read crash fix: `new_from_object` instead of
    # `to_mesh()`. No new file, so still 50 - only the hash moves.
    # 0.59.0 - adds `assetlib.py` (Blender assets in the Studio Library),
    # so 51 files. The hash moves for that and for `core.py` + `server.py`.
    ("addon_bundle", "2cb5973fd86cae77", True),
    ("addon_bundle", "54932b2d97de30ac", False),
    ("addon_bundle", "9015c8cad34d540e", False),
    ("addon_bundle", "e387f17ccb5e70fb", False),
    ("addon_bundle", "69515f50c272c5ed", False),
    ("addon_bundle", "7bdd871ff2bb3689", False),
    ("addon_bundle", "b239d3fc209876d7", False),
    ("addon_bundle", "6703b5ae7f1b8221", False),
    ("addon_bundle", "e9a0ef4eb63a3462", False),
    ("addon_bundle", "183298fb0141cf96", False),
    ("addon_bundle", "d254f571ec21de73", False),
    # ⚠ The Quadify-LESS bundle, pinned absent: a build made from source that
    # still has the removal would ship an app with a Quadify tab whose Blender
    # half answers no `quad_*` at all, and nothing else would report it.
    ("addon_bundle", "8d501dbdcb95e874", False),
    ("addon_bundle", "10fd4f45dc7bb4a2", False),
    ("addon_bundle", "15758d6b33ae5f84", False),
    # --- 2026-08-07, all of Blender's bake options + a sample count -------
    ("bridge", "0.25.0", True),
    ("bakenodes", "Contributions", True),
    ("bakenodes", "NO_VIEW_FROM", True),
    ("bakenodes", "samples_text", True),
    ("bakenodes", "pass_filter", True),
    ("nodecanvas", "draw_check", True),
    # the empty-bake diagnosis (1.1.2): _bake_done reads the reply's
    # "warning" and surfaces it (a comment is not a marker — only string
    # constants and names survive compilation)
    ("nodecanvas", "warning", True),
    # the old toolbar hint — a stale build would still carry the test-graph tab
    ("nodecanvas", "Test nodes", False),
    # --- things that must still be there from earlier builds -------------
    ("main", "TAB_TEXT_COLORS", True),
    ("nsfw", "Add Stretching torus", True),
    ("theme", "#9c4071", True),
    ("devedit", "RichTextDialog", True),
    ("anim_layers", "DragSlider", True),
    # --- 2026-08-08, the node batch: free tab, Map set, search, All slots -
    # ⚠ The exe Marty runs was FIVE batches behind when he reported three of
    # this batch's asks as missing — they were built, just not in his build.
    # These markers are what turns that into a build-time failure instead of
    # a conversation.
    ("main", "Node Editor", True),          # now a FREE_TOOLS entry
    # ...so its lock-panel blurb must have left with it, exactly like the
    # three tabs freed on 2026-08-06 above
    ("main", "A node-graph workspace on an infinite canvas", False),
    ("bakenodes", "Map set", True),
    ("bakenodes", "MAP_SET_DEFAULT", True),
    ("bakenodes", "filter_names", True),
    ("bakenodes", "SearchMenu", True),
    ("bakenodes", "replace_all_slots", True),
    ("bakenodes", "pass_filter_for", True),
    ("nodecanvas", "upstream_source", True),
    ("bridge", "all_slots", True),
    # --- 2026-08-08 pm: Collection node, gestures, help, the width fix ---
    ("bakenodes", "Collection", True),
    ("bakenodes", "CollectionNode", True),
    ("nodecanvas", "upstream_sources", True),
    ("nodecanvas", "split_with_reroute", True),
    ("nodecanvas", "HelpBubble", True),
    ("nodecanvas", "HELP_STYLE", True),
    ("nodecanvas", "help_rect", True),
    # ⚠ the window-width fix: without ElidedLabel in the build, the exe
    # Marty runs cannot be narrowed below ~2200 px however good the source is
    ("widgets", "ElidedLabel", True),
    # --- 2026-08-14: Proxy Cage REMOVED OUTRIGHT (Marty) ------------------
    # The whole tool went — page, worker, cage manager, the add-on's cage
    # module and its seven cage_* routes — deliberately with no What's New
    # entry. ⚠ physics.py's DOCSTRING still says "Proxy Cage" on purpose (it
    # is the tombstone, and docstrings survive the build), so absence is
    # asserted on the CLASS and BUTTON names, which only the live tool had.
    ("main", "Proxy Cage", False),
    ("main", "ProxyCageTool", False),
    ("physics", "ProxyCageTool", False),
    ("physics", "Build Cage", False),
    ("main", "Jiggle Cage", False),
    # the controls removed back on 2026-08-08 — still gone, now trivially
    ("physics", "Bind the body to the cage", False),
    ("physics", "Bind with the character posed", False),
    ("physics", "Corrective smooth on the cage", False),
    ("physics", "Collision  (collider only)", False),
    # --- 2026-08-08 pm: one instance, drag filters, a panel that hides ----
    # ⚠ Without this in the build, launching twice opens two windows onto one
    # library — and the whole point of the feature is that it cannot.
    ("main", "claim_single_instance", True),
    ("main", "SINGLE_INSTANCE_KEY", True),
    ("main", "_on_second_instance", True),
    ("widgets", "DragCheckBox", True),
    ("panels", "DragCheckBox", True),
    # --- 2026-08-08 eve: 1.4.0, the first PUBLISHED release ---------------
    # The version in the bottom-left corner. `set_version` is the call, and
    # `StatusBar` is the class that makes "at all times" possible at all — a
    # build with only the first would show it and then hide it again the next
    # time anything called showMessage.
    ("widgets", "StatusBar", True),
    ("main", "set_version", True),
    # Developer mode: edit is not OFFERED in a shipped build.
    # ⚠ AND THAT CANNOT BE CHECKED AS AN ABSENCE. "Developer mode: edit" is
    # still a string literal in main.py — the block is skipped at RUNTIME by
    # `devedit.available()`, not deleted — so it is in the bytecode either
    # way, and an absence marker for it would fail every build while saying
    # nothing. The gate itself is what must be present; `app_ui_test.py`
    # section 9 is what proves the gate actually withholds the control.
    # ⚠ Also NOT a version-string marker: 1.4.0 would die the moment the
    # version moves, which is the trap at the top of docs\packaging.md.
    # ⚠ A MARKER IS MATCHED AGAINST MARSHALLED BYTECODE, so it has to be a
    # STRING CONSTANT — `"def available"` was tried here first and failed on a
    # build that contained the function perfectly well. Its docstring is a
    # constant; the `def` line is not.
    ("devedit", "MADI_DEV_EDIT", True),
    ("devedit", "offered in this run at all", True),
    ("main", "devedit_available", True),
    # --- 1.5.0: the update flow in the status bar (Marty picked option B) --
    # ⚠ `show_progress` is the one that would be missed. UpdateManager.progress
    # emitted bytes for weeks with nothing connected to it, and a build where
    # the strip is absent looks exactly like a build where nothing is
    # downloading.
    ("widgets", "Popover", True),
    ("widgets", "show_progress", True),
    # --- 1.6.0: four colour themes -----------------------------------------
    # ⚠ `refresh_theme` is the one that would be missed and would look like a
    # half-broken app rather than a missing feature: without it the shell
    # changes colour and the node canvas stays in the previous theme.
    ("theme", "THEMES", True),
    ("theme", "apply_theme", True),
    ("theme", "graphite", True),
    ("theme", "blender", True),
    ("theme", "plum", True),
    ("nodecanvas", "refresh_theme", True),
    ("main", "cmb_theme", True),
    # --- 1.6.1: the lock screen names the tier ----------------------------
    # ⚠ The free-tab COUNT is derived from FREE_TOOLS at runtime, so it cannot
    # be checked as a literal here — `free_tabs_line` being present is the
    # thing that matters; `lic_client_test.py` proves what it renders.
    # ⚠ NO ABSENCE MARKER FOR "unlocks permanently". The phrase still appears
    # in the COMMENT explaining why it was removed, and comments are not in
    # bytecode — so the check would pass for a reason unrelated to what it
    # claims to prove. That the LABELS no longer say it is asserted where the
    # labels actually exist, in `lic_client_test.py`.
    # --- 1.7.0: Marty's six-item batch ------------------------------------
    # ⚠ Every one of these is a STRING CONSTANT or a NAME — both survive
    # compilation. Validated against source before this list was written.
    # ⚠ 0.43.1, not 0.43.0 — and the reason is worth keeping. The push waits
    # for the bridge to REPORT the target version, so re-pushing the same
    # number reports success instantly without installing anything. A changed
    # add-on always needs a changed version.
    # ⚠ A VERSION-STRING MARKER DIES WHEN THE VERSION MOVES — fourth time.
    # This is EXPECTED_ADDON_VERSION; validate it against source before every
    # build rather than discovering it after a four-minute one.
    # --- THE RESKIN + the update-install fix (1.17.0) ----------------------
    # ⚠ The lesson Quadify taught, applied on the way in this time: a new
    # module with NO markers can be missing from a build and only fail when
    # somebody clicks it. These three are the load-bearing pieces of 1.17.0 —
    # the drawn icon set, the rail that replaced the tab strip, and the swap
    # fix without which an update cannot install at all. All are docstring or
    # literal CONSTANTS, validated against source before the build.
    ("icons", "DRAWN, NOT SHIPPED AS FILES", True),
    ("widgets", "THE TAB BAR IS STILL THERE, JUST HIDDEN", True),
    ("main", "Studio Library", True),
    # --- the window's own title bar (1.18.0) -------------------------------
    # ⚠ Same lesson again, applied on the way in: `chrome.py` is a new module,
    # and a build missing it would start perfectly — `install()` swallows every
    # failure by design, so the app would just quietly wear the Windows title
    # bar again. The first two are docstring text, the third a literal the
    # maximise button sets; all three checked against source before the build.
    ("chrome", "THE NATIVE FRAME IS NOT REMOVED", True),
    ("chrome", "HAS TO BE ANSWERED TOO", True),
    ("chrome", "Restore down", True),
    # --- Quadify (1.15.0), REMOVED 2026-08-17 and RESTORED the same day at
    # Marty's request ("can we reappend quadify like we had before"). ⚠ The
    # absence twins that guarded the removal are gone again — a marker pinned
    # both present and absent fails every build, so the pair must move
    # together. ⚠ Quadify originally shipped with NO markers at all, so a
    # build could carry the tab with the module missing and only fail when
    # the button was pressed. All three are string CONSTANTS validated
    # against source, not comments.
    ("quadify", "Retopologising", True),
    ("quadify", "quad_progress", True),
    ("optimizer", "quad_progress", True),
    # --- Bake to shape keys (2026-08-21) ----------------------------------
    # ⚠ The delete button is a WHOLE CONTROL, not a tweak: a build that froze
    # an older `bakedeform.py` would show the Bake tool looking completely
    # normal with no way back out of a bake. Both halves are pinned — the
    # button's own label and the route it calls.
    ("bakedeform", "Delete all shape keys", True),
    ("bakedeform", "bake_clear_keys", True),
    # --- Blender assets in the Studio Library (2026-08-22) ----------------
    # ⚠ Assets are a WHOLE HALF OF A TAB behind a switch, so a build that
    # froze older modules would show a Studio Library that looks perfectly
    # normal and simply has no Assets button. Every layer is pinned: the
    # switch, the four registrations, the route and the catalog reader.
    ("main", "set_library_mode", True),
    ("main", "apply_asset_flow", True),
    ("main", "on_mark_asset", True),
    ("library", "read_catalogs", True),
    ("library", "ASSET_KINDS", True),
    ("library", "blender_assets.cats.txt", True),
    ("panels", "Mark selected", True),
    ("panels", "set_catalogs", True),
    ("bridge", "assetlib_save", True),
    ("bridge", "assetlib_apply", True),
    # ⚠ The four extensions themselves. A build whose `library.py` predates
    # them scans the same folders and finds nothing — an empty Assets grid
    # over a library full of assets, with no error anywhere.
    ("library", ".nodegroup", True),
    ("grid", "nodegroup", True),
    # --- Texture Maps (2026-08-17) ----------------------------------------
    # ⚠⚠ **THE LAZY-TAB TRAP, AND THIS TAB IS THE WORST CASE FOR IT.** Its
    # three modules are imported INSIDE `_build_texmaps` (PERF_PLAN option D),
    # and PyInstaller collecting a function-level import is something to
    # prove, not assume — the anim_layers lesson. A build missing them looks
    # perfect until somebody opens the tab, and then the tab is simply dead.
    ("texmaps", "TexMapsPage", True),
    ("texmaps", "README_TEMPLATE", True),
    ("texmaps_gl", "MapRunner", True),
    # The GLSL travels as string constants inside the module: if the shaders
    # were ever moved to files, they would be data that a build can drop.
    ("texmaps_gl", "#version 330 core", True),
    ("texmaps_gl", "GLUnavailable", True),
    # ⚠ The reference implementation is a TEST ORACLE and must NOT ship:
    # nothing under `app\` imports it, so PyInstaller never collects it,
    # and the first 1.22.0 build proved that by failing a present-marker.
    # Pinned ABSENT so the intent is recorded — and so an accidental
    # runtime import (which would quietly put a pure-Python copy of every
    # map into the binary) fails this check instead of shipping.
    ("texmaps_ref", "levels", False),
    ("texmaps_source", "ThumbCache", True),
    # ⚠ The scene picker's gate and its two commands. A build whose bridge
    # lost these would offer the picker and then fail on the call.
    ("bridge", "texmaps_scene", True),
    ("bridge", "tex_export", True),
    # --- Organize (1.23.0) ------------------------------------------------
    # ⚠ Imported INSIDE `_build_organize`, like the texmaps three. This tab
    # POLLS, so a build where it is missing does not merely look broken — it
    # dies on first open for everyone.
    ("organize", "OrganizePage", True),
    ("organize", "SetsTree", True),
    ("bridge", "organize_sets", True),
    ("bridge", "sets_isolate", True),
    ("bridge", "0.51.0", True),
    # ⚠⚠ "0.50.0" STAYS A PRESENCE MARKER, and pinning it absent was a real
    # mistake caught by the first 1.23.0 build. It is not merely the previous
    # EXPECTED_ADDON_VERSION — it is also the **since_version of the
    # `texmaps_scene` feature requirement**, so it is a legitimate string in
    # `bridge.py` forever. This is precisely the "0.40.0" case documented a
    # few lines down, made again by rote: **before adding an absence twin,
    # grep the version out of `app\bridge.py` and check it is not somebody's
    # since_version.**
    ("bridge", "0.50.0", True),
    # --- Rig properties (1.24.0) -------------------------------------------
    # ⚠ The second page of the Organize rail, imported inside
    # `_build_organize` beside `organize`. It POLLS too, so a build missing it
    # dies on first open of that rail entry.
    ("rigprops", "RigPropsPage", True),
    ("rigprops", "ChannelDelegate", True),
    # ⚠ The delegate is the tab's whole performance story — 775 rows drawn by
    # ONE painter rather than 2,325 widgets. Pinning it here means a build
    # that somehow shipped without it fails the check rather than shipping a
    # tab that takes seconds to open.
    ("rigprops", "ChannelTable", True),
    ("bridge", "rig_props", True),
    ("bridge", "rig_props_unkey", True),
    ("bridge", "0.59.0", True),
    # ⚠ "0.58.0" grepped out of `appridge.py` before flipping this,
    # as the rule requires - it was only ever the expected version, and
    # 0.59.0 replaced it. 0.59.0 IS also a since_version (the assetlib
    # gate), so it has two mentions rather than one.
    ("bridge", "0.58.0", False),
    # ⚠ "0.57.0" grepped out of `app\bridge.py` before flipping
    # this, as the rule requires - it was only ever the expected version.
    ("bridge", "0.57.0", False),
    # ⚠ "0.56.0" is ABSENT for the same reason "0.55.0" is below: it
    # was only ever the expected version, never a since_version, so
    # bumping removed its last mention from `app\bridge.py` -
    # grepped to confirm before flipping this.
    ("bridge", "0.56.0", False),
    # ⚠ "0.55.0" is ABSENT for the same reason "0.52.1" is: it was only ever
    # the expected version, never a since_version, so bumping removed its
    # last mention from `app\bridge.py` - grepped to confirm.
    ("bridge", "0.55.0", False),
    # ⚠⚠ "0.52.1" is now ABSENT, and that is the check working. It was only
    # ever `EXPECTED_ADDON_VERSION`, never a since_version, so bumping the
    # expectation to 0.55.0 removed its last mention from `appridge.py`
    # entirely - grepped to confirm before flipping this, which is the rule
    # the notes below ask for.
    ("bridge", "0.52.1", False),
    # ⚠ "0.52.0" stays PRESENT too — it is `rig_props`'s since_version, so it
    # lives in `app\bridge.py` forever even though 0.52.1 is what the app now
    # expects. The 0.52.1 bump was the REDRAW fix: writes tagged the depsgraph
    # and asked nothing to repaint, so the viewport showed the old shape.
    ("bridge", "0.52.0", True),
    # ⚠ "0.51.0" stays a PRESENCE marker for the same reason "0.50.0" does
    # above: it is `organize_sets`'s since_version, not merely the previous
    # expected version. Checked by grepping it out of `app\bridge.py` before
    # writing this line — which is the rule the comment above asks for.
    ("bridge", "0.51.0", True),
    # --- the desktop surface ----------------------------------------------
    # ⚠ These three outlived the cancelled port (2026-08-17) because they are
    # worth having on Windows alone: `desktop.py` is the ONE place the app asks
    # the OS to open or reveal a file, and `main` must go through it rather
    # than calling `os.startfile` itself — five scattered call sites is what
    # the module replaced. `DATA_DIR` now equals `APP_DIR`, and the name is
    # still what ~50 call sites and every suite's temp redirect use.
    ("config", "DATA_DIR", True),
    ("desktop", "reveal_command", True),
    ("main", "os.startfile", False),
    # The four locks that make a refused add-on package impossible to ship
    # silently. A stale build without them is the failure this all came from.
    ("bridge", "addon_update_result", True),
    # ⚠ Absence twins, each earned when EXPECTED moved past it — every one of
    # these was only ever that constant in THIS module. "0.41.0" is still a
    # live string in `markers.py` (the layer gate's message), and markers are
    # matched per-module, so the two cannot collide.
    # ⚠ "0.45.0" JOINED ON 2026-08-14 and the presence marker moved to "0.45.1"
    # with it — the fifth time a version-string marker has had to move. Checked
    # against source first: `grep 0.4[0-9].[0-9] app\bridge.py` leaves only
    # 0.40.0 and 0.44.0 (both real `since_version`s) plus the expected one.
    # ⚠ "0.45.1" joined 2026-08-14 evening when EXPECTED moved to 0.46.0
    # (the all-tabs-free add-on) — sixth move. Same source check as ever.
    # ⚠ "0.48.0" joined 2026-08-17 evening when EXPECTED moved to 0.49.0 for
    # the Quadify restore — the sixth time a version-string marker has had to
    # move, and it failed the build rather than passing quietly, which is the
    # whole point of the pair. Checked against source first: `0.48.0` appears
    # nowhere else in `bridge.py`.
    ("bridge", "0.48.0", False),
    ("bridge", "0.45.1", False),
    ("bridge", "0.45.0", False),
    ("bridge", "0.42.0", False),
    ("bridge", "0.41.0", False),
    # ⚠ "0.40.0" STAYS A PRESENCE MARKER even though EXPECTED_ADDON_VERSION
    # moved past it — it is also the since_version of the `markers` feature
    # requirement, so it is a legitimate string in bridge.py forever. Exactly
    # the "0.37.0" case below; do not "tidy" it into an absence twin.
    ("bridge", "0.40.0", True),
    # ⚠ AND THE ABSENCE TWINS. Each of these was only ever
    # EXPECTED_ADDON_VERSION, so once that moved on the string left bridge.py
    # entirely. Checked with `grep 0.3[6789].0 app\bridge.py` before adding
    # them — that is the whole validation, and it is worth the ten seconds.
    # ⚠ "0.39.0" JOINED THIS LIST ON 2026-08-12 and the presence marker moved
    # to "0.40.0" with it: a version-string marker DIES when the version moves,
    # which is the trap at the top of docs\packaging.md.
    ("bridge", "0.39.0", False),
    ("bridge", "0.38.0", False),
    # ⚠ NO ABSENCE TWIN FOR "0.37.0". It moved out of EXPECTED_ADDON_VERSION on
    # 2026-08-11, but it is ALSO the since_version of the `madiref_pin`
    # requirement, so it is a legitimate string in bridge.py forever — exactly
    # the "0.17.0" case at the top of docs\packaging.md. Asserting it absent
    # would fail every good build.
    ("bridge", "save_blend", True),
    ("render_deck.queue_tool", "already queued", True),
    ("render_deck.queue_tool", "save_open_blend", True),
    ("main", "save_open_blend_for_queue", True),
    ("main", "save_picker_flow", True),
    ("panels", "Save Picker Tab", True),
    # --- 2026-08-11: MadiRef -----------------------------------------------
    # ⚠ String constants only. `MRFX`/`MRRB` are the on-disk and in-memory
    # magic numbers as ints, so they are NOT matchable — these are literals and
    # names that survive compilation, validated against source before building.
    ("madiref.proxy", "not a MadiRef proxy", True),
    ("madiref.proxy", "ingest was interrupted", True),
    ("madiref.shm", "not a MadiRef ring", True),
    ("madiref.decoder", "target_frame", True),
    ("madiref.decoder", "madiref_", True),
    ("madiref.ingest", "image2pipe", True),
    ("madiref.audio", "DRIFT_TOLERANCE_MS", True),
    ("madiref.tab", "Follow Blender's timeline", True),
    ("madiref.tab", "Show in Blender", True),
    ("madiref.tab", "Play reference audio", True),
    ("madiref.tab", "Reset placement", True),
    ("madiref.tab", "_mirror_view_from_ring", True),
    ("madiref.tab", "madiref_pin", True),
    ("bridge", "madiref_placement", True),
    ("bridge", "madiref_viewport", True),
    ("bridge", "madiref_open", True),
    ("main", "MadiRef", True),
    # --- 2026-08-12: timeline markers. A whole new app module, and its absence
    # is SILENT in the usual way — the Anim Layers tab still opens and the rail
    # still lists Layers and Options, so only the missing third entry would say
    # anything. String constants and names, validated against source first.
    ("markers", "madi-markers", True),
    ("markers", "read_marker_file", True),
    ("markers", "Search name, tag or note", True),
    ("markers", "_fill_detail", True),
    ("markers", "Render at marker", True),
    # --- 2026-08-12 (later): marker LAYERS, a filter over the same markers
    ("markers", "All layers", True),
    ("markers", "Marker layers need Blender add-on", True),
    ("markers", "_set_layers_supported", True),
    ("markers", "active_layer", True),
    # --- 2026-08-12 (later still): real hiding, named sets, the A2 rows
    ("markers", "MarkerRowDelegate", True),
    ("markers", "Marker sets", True),
    ("markers", "hidden by this layer", True),
    ("markers", "_on_layer_pick", True),
    ("bridge", "marker_show_layer", True),
    ("bridge", "marker_set_save", True),
    ("bridge", "marker_list", True),
    ("bridge", "marker_set", True),
    ("render_deck.queue_tool", "queue_at_frame", True),
    ("main", "markers_tool", True),
    # --- 2026-08-12: the drawn notes (a whole new module, so it is worth
    # proving the exe carries it rather than the folder having been missed)
    ("madiref.notes", "_madiref_notes", True),
    ("madiref.notes", "strokes_at", True),
    # ⚠ `note_in_force` WAS a marker here and had to be retired the same day:
    # it was the "until the next note" rule, which Marty reversed after using
    # it. A marker naming an implementation detail dies with that detail —
    # exactly like a version string. Validate against SOURCE before building.
    ("madiref.notes", "note_in_force", False),
    ("madiref.tab", "Show markings in Blender", True),
    ("madiref.decoder", "notes_changed", True),
    # --- 2026-08-12 pm: Blend file size (Marty: a tree of what makes the
    # .blend big). Another whole new module, and one whose absence is silent:
    # the tab would still open, the tool would still be listed, and only the
    # button would fail. `blendsize` is imported by `optimizer`, so a build
    # that missed the file fails HERE rather than in front of him.
    ("blendsize", "BLENDER", True),
    ("blendsize", "id_name_offset", True),
    ("blendsize", "Surface Deform bind data", True),
    ("optimizer", "Measure the open .blend", True),
    ("optimizer", "The file's own bookkeeping", True),
    # ⚠ zstd is not optional in practice — nearly every .blend saved today is
    # compressed, so a build whose venv missed the package ships a tool that
    # refuses most real files.
    ("blendsize", "zstandard", True),
    # --- 2026-08-11 pm: MadiRef GATED — then FREED with everything else on
    # 2026-08-14. ⚠ The lock-panel blurb is still the marker that
    # discriminates: "MadiRef" is in the exe either way (it is the tab
    # title), but a blurb only ever existed for a GATED entry, so its
    # ABSENCE is what proves the all-tabs-free build is the one shipping.
    # A stale build here would still be selling locks.
    ("main", "Play a video reference inside Blender's 3D viewport", False),
    # ⚠ NO MARKER FOR THE PICKER'S OWN THREE (the walls, the per-button scale,
    # the Bones & Extras toggle). They live in the ADD-ON, which rides in the
    # exe as one base64 blob — the bundle sha above is what covers them, and a
    # per-string marker would be looking in the wrong module entirely.
    # --- and things that must NOT have come back -------------------------
    ("nsfw", "Add test affector", False),
    ("anim_layers", "Apply Inbetween", False),
    ("bridge", "asset_add_affector", False),
]


def load_archive():
    from PyInstaller.archive.readers import CArchiveReader
    return CArchiveReader(EXE)


def strings_of(code, out):
    """Every string constant reachable from `code`, INCLUDING inside tuples."""
    def walk(const):
        if isinstance(const, str):
            out.add(const)
        elif isinstance(const, (tuple, frozenset, list)):
            for item in const:
                walk(item)
        elif hasattr(const, "co_consts"):
            for item in const.co_consts:
                walk(item)
            out.update(const.co_names)
            out.update(getattr(const, "co_varnames", ()))
    walk(code)
    out.update(code.co_names)


def main():
    if not os.path.isfile(EXE):
        print("exe not found: %s" % EXE)
        return 1
    import marshal

    arch = load_archive()
    # The entry script `main` is NOT in the PYZ - it is stored in the CArchive
    # itself, marshalled.
    pyz_name = next(n for n in arch.toc if n.endswith(".pyz"))
    from PyInstaller.archive.readers import ZlibArchiveReader
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "madi_verify.pyz")
    with open(tmp, "wb") as fh:
        fh.write(arch.extract(pyz_name))
    pyz = ZlibArchiveReader(tmp)

    cache = {}

    def module_strings(name):
        if name in cache:
            return cache[name]
        found = set()
        try:
            if name == "main":
                data = arch.extract("main")
                code = marshal.loads(data)
            else:
                code = pyz.extract(name)
            strings_of(code, found)
        except Exception as exc:              # noqa: BLE001
            print("   ! could not read module %r: %s" % (name, exc))
        cache[name] = found
        return found

    ok = bad = 0
    for module, marker, want in MARKERS:
        blob = module_strings(module)
        # SUBSTRING, not membership - adjacent literals are one constant.
        present = any(marker in s for s in blob)
        good = (present == want)
        ok, bad = (ok + 1, bad) if good else (ok, bad + 1)
        verdict = "ok  " if good else "FAIL"
        print("%s %-12s %-24s %s" % (
            verdict, module, marker,
            "present" if present else "absent"))
    print("\n%d/%d markers as expected" % (ok, ok + bad))

    # ⚠ A NATIVE PACKAGE NEEDS BOTH HALVES, AND A MARKER CANNOT SEE THAT.
    # `blendsize` imports zstandard inside a try, so a build that shipped the
    # .pyd without the Python package — or the package without the .pyd —
    # starts perfectly, passes every marker above, and then refuses nearly
    # every real .blend in the File size tool, because Blender compresses by
    # default. The frozen app is `--windowed` and prints nothing, so it cannot
    # report this itself: the exit code is its only channel and that is wired
    # to the updater's keep/roll-back decision. So it is checked from out here.
    pyz_names = [n for n in pyz.toc if "zstandard" in n]
    pyds = []
    for base, _dirs, files in os.walk(INTERNAL):
        for name in files:
            if name.endswith(NATIVE_SUFFIX) and (
                    "zstandard" in name.lower() or
                    "zstandard" in base.lower()):
                pyds.append(name)
    native_ok = bool(pyz_names) and bool(pyds)
    print("%s zstandard: %d module(s) in the archive, %d native lib(s) beside "
          "the exe" % ("ok  " if native_ok else "FAIL",
                       len(pyz_names), len(pyds)))
    if not native_ok:
        print("     -> the File size tool would refuse every compressed .blend")

    # ⚠ LAZY-TAB MODULES (PERF_PLAN C+D, 2026-08-15). These are imported only
    # inside their tab's `_build_*`, so nothing at module level in main.py
    # names them. PyInstaller does collect function-level imports — but that
    # is exactly the kind of claim this file exists to PROVE on the frozen
    # build rather than believe: a module missing here is a tab that crashes
    # on FIRST OPEN, for every user, and never from a source run.
    lazy_bad = 0
    for lazy in ("anim_layers", "markers",
                 # Texture Maps (2026-08-17): three modules behind one
                 # function-level import, and the tab is dead without any.
                 "texmaps", "texmaps_gl", "texmaps_source", "organize",
                 # Rig properties (2026-08-19) — the Organize rail's second
                 # page, behind the same function-level import as `organize`.
                 "rigprops"):
        if lazy in pyz.toc:
            print("ok   lazy module %-12s is in the frozen PYZ" % lazy)
        else:
            lazy_bad += 1
            print("FAIL lazy module %-12s MISSING from the frozen PYZ - its "
                  "tab dies on first open" % lazy)

    size = os.path.getsize(EXE) / (1024.0 * 1024.0)
    print("exe: %.2f MB  %s" % (size, EXE))
    return 1 if (bad or not native_ok or lazy_bad) else 0


if __name__ == "__main__":
    sys.exit(main())
