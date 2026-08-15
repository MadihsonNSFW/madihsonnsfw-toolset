# MADI Anim Library — Blender bridge add-on.
# The Blender side of the MadihsonNSFW Toolset: a localhost JSON bridge the
# external PySide6 app drives, plus the N-panel that starts it and opens the
# app. See HANDOFF.md in the project root.
#
# ⚠ The panel is NOT a second library UI. It had Save Pose / Save Set / Apply
# boxes of its own until 2026-08-08; Marty removed them, because the app is the
# only one of the two with a thumbnail grid, folders, tags, versions and the
# save dialogs — a poorer copy inside Blender was just somewhere for the two to
# disagree. The engine (`core.save_pose`, `core.apply_pose`, …) is untouched
# and still does the work; the bridge commands are how it is reached.

import os

import bpy
from bpy.types import AddonPreferences, Operator, Panel
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, IntProperty,
                       StringProperty)

from . import (anim_layers_ui, core, jiggle, markers, optimizer,
               picker, server)

# ⚠ NO PATH IS BAKED IN. `DEFAULT_APP` used to be an absolute path on the
# machine this was written on — fine for exactly one person, and on any other
# install pointing at a folder that does not exist, so the add-on looked broken
# before it had done anything. Empty instead, and ASKED FOR: pressing Open
# Toolset App with nothing set opens a file browser and remembers what you pick
# (MADILIB_OT_open_app).
#
# ⚠ There is no library path here any more either. It went with the panel's
# Save/Apply boxes on 2026-08-08: every bridge command carries its own
# `library_root` from the app, so the add-on has no library of its own to know
# about. Don't re-add one "for the panel" without a panel that needs it.
DEFAULT_APP = ""


def _prefs():
    return bpy.context.preferences.addons[__package__].preferences


def _app_path():
    """The Toolset exe, or "" if it has not been chosen yet.

    ⚠ Guarded rather than handed straight to `bpy.path.abspath` — with a saved
    .blend that turns "" into THAT FILE'S OWN FOLDER, which is how an unset
    path quietly becomes a real and wrong one.
    """
    raw = (_prefs().app_path or "").strip()
    return bpy.path.abspath(raw) if raw else ""


def _pk_redraw(self, context):
    """Bone picker appearance changed - just tag the editors for a repaint."""
    picker.tag_redraw(context)


class MADILIB_Prefs(AddonPreferences):
    bl_idname = __package__

    port: IntProperty(name="Bridge Port", default=server.DEFAULT_PORT,
                      min=1024, max=65535)
    # ⚠ NO `auto_start` PREFERENCE — the bridge is started by hand, per Blender,
    # every session (Marty, 2026-08-12: "Don't automatically Start blender
    # bridge no matter what, users must start per blender instance"). The
    # preference went with the behaviour rather than being defaulted to off: a
    # switch labelled "Start Bridge Automatically" that no longer can is worse
    # than no switch. Nothing reads it; Blender drops an unknown property from
    # an older userpref file without complaint.
    app_path: StringProperty(name="Toolset App", subtype='FILE_PATH',
                             description="The external MadihsonNSFW Toolset app "
                                         "(exe or run_app.bat) launched by the "
                                         "panel's Open Toolset App button. "
                                         "Leave it empty and you will be asked "
                                         "to pick it the first time you press "
                                         "that button",
                             default=DEFAULT_APP)

    # --- Animation Layers settings, SHARED WITH THE APP.
    # These are the one part of Anim Layers that is genuinely two stores: the
    # app keeps them in config.json, Blender keeps them here. They are mirrored
    # both ways - the add-on's copy rides along in every anim_layers_status
    # reply, and the app pushes its own with anim_layers_set_prefs.
    # ⚠ Add a setting to one side and you MUST add it to the other, or the two
    # UIs will quietly mean different things by the same switch.
    al_sync_names: BoolProperty(
        name="Sync layer and action names", default=True,
        description="Renaming a layer renames its action too")
    al_auto_blend: BoolProperty(
        name="Auto blend mode", default=True,
        description="Pick Add or Replace automatically when loading an action")
    al_default_blend: EnumProperty(
        name="New layer blend", default='COMBINE',
        items=[('COMBINE', "Combine", "Blend on top of what is below"),
               ('REPLACE', "Replace", "Replace what is below"),
               ('ADD', "Add", "Add to what is below"),
               ('SUBTRACT', "Subtract", "Subtract from what is below"),
               ('MULTIPLY', "Multiply", "Multiply what is below")],
        description="Blend type given to a newly added layer")

    # --- Bone picker appearance.
    # ⚠ These live HERE, not on a preferences class of the picker's own. As a
    # standalone add-on the picker had one keyed `bl_idname = __name__`; inside
    # this package that resolves to a module path, which is not an add-on key,
    # so the lookup returns None - and every reader in picker.py falls back to a
    # constant without raising. The settings would silently stop working.
    # `picker._prefs()` reads these by __package__; the `pk_` prefix keeps them
    # clear of the `al_` ones above.
    pk_bg_darken: FloatProperty(
        name="Darken Background",
        description="Dim the Image Editor behind the picker while it runs "
                    "(1-100%), so buttons stay readable over a bright "
                    "reference. Drawn as an overlay - the image itself is "
                    "never touched, and a stopped picker dims nothing",
        subtype='PERCENTAGE', min=1.0, max=100.0, default=60.0,
        update=_pk_redraw)

    pk_btn_alpha: FloatProperty(
        name="Button Opacity",
        description="How solid the buttons are drawn (1-100%). SOLID by "
                    "default - drop it to see the reference image through "
                    "them. Applies to every button, slider track and fill; "
                    "labels and selection outlines stay opaque",
        subtype='PERCENTAGE', min=1.0, max=100.0, default=100.0,
        update=_pk_redraw)

    pk_btn_round: FloatProperty(
        name="Corner Roundness",
        description="How round the corners of buttons and sliders are, as a "
                    "percentage of the button's smaller drawn side. 0% = "
                    "square corners, 50% = a full stadium. Cosmetic only - a "
                    "click in a cut corner still hits the button",
        subtype='PERCENTAGE', min=0.0, max=50.0,
        default=picker.BTN_ROUND * 100.0,
        update=_pk_redraw)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "app_path")
        # Empty on a fresh install, so say what happens instead of leaving a
        # blank field and no clue.
        if not (self.app_path or "").strip():
            col.label(text="Not set — the panel's Open Toolset App button "
                           "will ask you to locate it", icon='INFO')
        col.prop(self, "port")
        col.label(text="The bridge is started by hand from the sidebar, "
                       "once per Blender", icon='INFO')
        box = self.layout.box()
        box.label(text="Animation Layers (shared with the Toolset app)")
        box.prop(self, "al_sync_names")
        box.prop(self, "al_auto_blend")
        box.prop(self, "al_default_blend")
        box = self.layout.box()
        box.label(text="Bone Picker appearance")
        box.prop(self, "pk_btn_alpha", slider=True)
        box.prop(self, "pk_btn_round", slider=True)
        box.prop(self, "pk_bg_darken", slider=True)


# ---------------------------------------------------------------- operators

class MADILIB_OT_server_toggle(Operator):
    bl_idname = "madilib.server_toggle"
    bl_label = "Start/Stop Bridge"
    bl_description = "Start or stop the local bridge server for the library app"

    def execute(self, context):
        srv = server.server
        if srv.running:
            srv.stop()
            self.report({'INFO'}, "Bridge stopped")
            return {'FINISHED'}

        prefs = context.preferences.addons[__package__].preferences
        srv.port = prefs.port
        # ⚠ JUDGE THE RETURN VALUE, NOT AN EXCEPTION. `start()` catches its own
        # bind error, so the `except OSError` that used to sit here could never
        # fire and this reported "Bridge listening on port 9877" over a port it
        # had just been refused. Harmless while a retry took the port seconds
        # later; a plain lie now that nothing does.
        if not srv.start():
            self.report({'ERROR'},
                        "Port %d is already in use by another Blender. Stop "
                        "the bridge there first, then start it here."
                        % srv.port)
            return {'CANCELLED'}
        self.report({'INFO'}, "Bridge listening on port %d" % srv.port)
        return {'FINISHED'}


class MADILIB_OT_open_app(Operator):
    """Launch the Toolset, asking where it is the first time.

    ⚠ The path used to default to an absolute path on one machine. On every
    other install that button could only ever fail, and the error told you to
    go and edit a preference — which is a poor answer when the operator can
    simply ask. It asks now, and remembers.
    """

    bl_idname = "madilib.open_app"
    bl_label = "Open Toolset App"
    bl_description = "Launch the external MadihsonNSFW Toolset app. You are " \
                     "asked to locate it the first time; the path is then " \
                     "remembered in the add-on preferences"

    # Set only when the file browser was used, so a normal launch never writes
    # over a path that is already good.
    filepath: StringProperty(subtype='FILE_PATH', options={'SKIP_SAVE'})
    filter_glob: StringProperty(default="*.exe;*.bat", options={'HIDDEN'})

    def invoke(self, context, event):
        if os.path.isfile(_app_path()):
            return self.execute(context)
        # Nothing usable set yet. Open the browser at the folder they last
        # named, if any, rather than wherever Blender happens to point.
        self.filepath = _app_path()
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        import subprocess
        prefs = context.preferences.addons[__package__].preferences
        if self.filepath:
            prefs.app_path = self.filepath
            # ⚠ Blender only writes preferences to disk when auto-save is on.
            # With it off this is remembered for the session and forgotten on
            # restart, so SAY so rather than letting them discover it.
            if not context.preferences.use_preferences_save:
                self.report({'WARNING'},
                            "Toolset path set. Preferences ▸ Save Preferences "
                            "to keep it — auto-save is off")
        path = _app_path()
        if not os.path.isfile(path):
            self.report({'ERROR'},
                        "App not found: %s — press this button again to pick "
                        "it, or set 'Toolset App' in the add-on preferences"
                        % (path or "(no path set)"))
            return {'CANCELLED'}
        # detached: the app must outlive Blender and never inherit its console
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        try:
            subprocess.Popen([path], cwd=os.path.dirname(path),
                             creationflags=flags)
        except OSError as exc:
            self.report({'ERROR'}, "Launch failed: %s" % exc)
            return {'CANCELLED'}
        self.report({'INFO'}, "Toolset app launched")
        return {'FINISHED'}


class MADILIB_OT_watch_last_render(Operator):
    bl_idname = "madilib.watch_last_render"
    bl_label = "Watch Last Render"
    bl_description = ("Open the newest viewport render made through the Toolset "
                      "(playblast) in the system video player")

    @classmethod
    def poll(cls, context):
        # ⚠ core.last_render() is CACHED for exactly this reason: poll runs on
        # every panel redraw, which is every mouse move over the region.
        return core.last_render() is not None

    def execute(self, context):
        # Read past the cache: the button may have sat here since before the
        # file was moved, and opening a path that has gone is the one outcome
        # this is supposed to prevent.
        path = core.last_render(max_age=0)
        if path is None:
            self.report({'ERROR'}, "That render is no longer on disk")
            return {'CANCELLED'}
        bpy.ops.wm.path_open(filepath=path)
        self.report({'INFO'}, os.path.basename(path))
        return {'FINISHED'}


# ---------------------------------------------------------------- panel

class MADILIB_PT_panel(Panel):
    bl_idname = "MADILIB_PT_panel"
    bl_label = "Studio Library"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadihsonNSFW"

    def draw(self, context):
        layout = self.layout
        srv = server.server

        # ⚠ THREE states, not two. "off" and "another Blender already has the
        # port" look the same through a bare running flag and need opposite
        # reactions — press Start, versus "go and stop it in your OTHER
        # Blender". Marty ran two instances and had no way to tell which one
        # the app was talking to (2026-08-05).
        # ⚠ THE BRIDGE NEVER STARTS ITSELF (0.39.0). This row is the only way
        # it ever comes up, in every instance, every session.
        row = layout.row()
        state = getattr(srv, "state", "listening" if srv.running else "stopped")
        if srv.running:
            row.label(text="Bridge: port %d" % srv.port, icon='CHECKMARK')
        elif state == "blocked":
            row.label(text="Bridge: port %d in use" % srv.port, icon='ERROR')
        else:
            row.label(text="Bridge: off", icon='X')
        row.operator("madilib.server_toggle",
                     text="Stop" if srv.running else "Start")
        if not srv.running and state == "blocked":
            col = layout.column(align=True)
            col.label(text="Another Blender has the bridge.", icon='INFO')
            col.label(text="Stop it there, then press Start here.")
        layout.operator("madilib.open_app", icon='WINDOW')
        # Greys itself out when there is no render on disk to watch — the
        # operator's poll does the check, so the row needs no logic of its own.
        layout.operator("madilib.watch_last_render", icon='PLAY')

        # ⚠ NO Save / Apply boxes here (Marty, 2026-08-08: "We don't need Save
        # pose features in the blender bridge, only app like we have now is
        # fine, same with apply poses"). This panel is the BRIDGE — start it,
        # open the app, watch the last render. Saving and applying live in the
        # app, which is the only one of the two with a thumbnail grid, folders,
        # tags, versions and an options dialog; a second, worse copy of the
        # same feature inside Blender was a place for the two to disagree.
        # `core.save_pose` / `apply_pose` and the rest are untouched — the
        # bridge commands still call them, and that is how the app does it.


# ---------------------------------------------------------------- registration

# ⚠ The save/apply operators and their PropertyGroup went on 2026-08-08 with
# the panel boxes that drove them. Nothing else registered `wm.madilib`, so
# that property is gone too — Anim Layers has its own, `wm.madilib_al`.
_classes = (
    MADILIB_Prefs,
    MADILIB_OT_server_toggle,
    MADILIB_OT_open_app,
    MADILIB_OT_watch_last_render,
    MADILIB_PT_panel,
)


# ⚠ `_autostart()` LIVED HERE AND IS GONE (0.39.0). It ran on a 0.5 s timer
# after register() and started the bridge in every Blender that loaded the
# add-on. Marty, 2026-08-12: *"Don't automatically Start blender bridge no
# matter what, users must start per blender instance"*.
#
# ⚠ **`register()` IS ALSO WHAT A RELOAD CALLS**, so removing this had one
# consequence worth knowing: a self-update reloads the extension, and the
# bridge used to come back by itself here. `selfupdate.reload_addon()` now
# restarts a bridge that was ALREADY RUNNING when the reload began — restoring
# what the user started, which is a different thing from starting it for them.
# Without that, `addon_update` would install correctly and then look like a
# hang, because the app re-polls `ping` for a bridge that is never coming back.


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    # Bone Jiggle owns its own property groups and frame handlers, so it
    # registers itself. Done early for the same reason the panel classes come
    # last everywhere else: nothing may draw or evaluate a property that is not
    # registered yet.
    jiggle.register()
    # Same reason: the Anim Layers panel owns its own operators, menu and
    # property group, so it registers itself.
    anim_layers_ui.register()
    # Markers owns three properties ON `bpy.types.TimelineMarker` plus its own
    # panel, and the properties must exist before anything draws one — same
    # rule as the three around it.
    markers.register()
    # And the Bone Picker, which owns the most of all: its own property groups,
    # two frame handlers and a POST_PIXEL draw handler. Registered before
    # anything can serve a command, for the same reason as the other two - the
    # bridge must not be able to answer a picker command before the picker
    # exists.
    picker.register()
    # And the Scene Optimizer, which owns a load_post handler (it re-makes any
    # stand-in texture that went missing while the file was closed) and the
    # modal preview operator. Same rule as the three above: registered before
    # the bridge can answer a command that would use it.
    optimizer.register()
    # MadiRef owns the modal operator that makes the reference overlay
    # movable/scalable/rotatable in the viewport. Same rule as the four above:
    # registered before the bridge can answer a command that would use it.
    from . import madiref
    madiref.register()
    # ⚠ NOTHING STARTS THE BRIDGE HERE. The 0.5 s autostart timer that used to
    # close this function is gone (see the note above `register`), and the rule
    # it existed for still stands for anything added later: NEVER touch
    # bpy.data / scene state in register() — defer to a timer.


def unregister():
    if server.server.running:
        server.server.stop()
    optimizer.unregister()
    picker.unregister()
    markers.unregister()
    anim_layers_ui.unregister()
    jiggle.unregister()
    # ⚠ Imported lazily and guarded: a draw handler that survives an add-on
    # reload keeps drawing from a dead module and takes the viewport with it,
    # so this must run even if madiref was never opened this session.
    try:
        from . import madiref
        madiref.unregister()
    except Exception:                                # noqa: BLE001
        pass
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
