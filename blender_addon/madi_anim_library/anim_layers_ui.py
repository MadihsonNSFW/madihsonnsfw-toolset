"""Animation Layers, inside Blender's N-panel.

The same stack the Toolset app drives, reachable without leaving Blender. Marty
asked for it (2026-08-04) "with a dropdown menu to keep everything nice and
clean", so the panel itself is DEFAULT_CLOSED and everything past the everyday
controls lives behind one Layer Tools menu instead of a wall of buttons.

WHY THE TWO UIs CANNOT DRIFT
There is no second copy of anything. A layer IS an NLA track, and both this
panel and the app call the same `core.al_*` functions, so "sync" is not a
mechanism here - it is the absence of a second source of truth. Blender redraws
the panel constantly and the app polls `anim_layers_status`, so a change made on
either side shows up on the other without either one telling the other anything.

The one exception is SETTINGS (sync names / auto blend / default blend), which
are genuinely two stores: the app keeps them in config.json, Blender keeps them
in add-on preferences. Those are mirrored deliberately - the add-on's copy rides
along in every `anim_layers_status` reply and the app pushes its own with
`anim_layers_set_prefs`. Add a setting to one side and you must add it to both,
or it will silently mean different things in each.

⚠ draw() MUST NOT WRITE. `core.anim_layers_status()` is documented as a pure
read for exactly this reason, which is what makes it safe to call while drawing.
Anything that changes the scene belongs in an operator - including seeding the
blend/influence widgets, which happens when a layer is selected rather than on
every redraw.
"""

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, IntProperty,
                       StringProperty)
from bpy.types import Menu, Operator, Panel, PropertyGroup

# ⚠ `entitlement` is no longer imported here: Anim Layers is free (2026-08-06),
# and an unused import of the gate module makes a free tool read as gated. The
# LOCKED_HINT that lived here went with it.
from . import core

BLEND_ITEMS = [
    ('COMBINE', "Combine", "Blend on top of what is below (the usual choice)"),
    ('REPLACE', "Replace", "Replace what is below"),
    ('ADD', "Add", "Add to what is below"),
    ('SUBTRACT', "Subtract", "Subtract from what is below"),
    ('MULTIPLY', "Multiply", "Multiply what is below"),
]

DATA_ITEMS = [
    ('OBJECT', "Object", "Bone and object animation"),
    ('SHAPEKEY', "Shape Keys", "Shape-key animation"),
]


def _prefs():
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        return None


def shared_prefs():
    """The settings the app and this panel both own. Rides along in
    `anim_layers_status` so the app sees a Blender-side change on its next poll."""
    p = _prefs()
    if p is None:
        return {}
    return {"sync_names": bool(p.al_sync_names),
            "auto_blend": bool(p.al_auto_blend),
            "default_blend": p.al_default_blend}


def apply_prefs(values):
    """Take the app's copy of the settings. Returns what was actually stored."""
    p = _prefs()
    if p is None or not isinstance(values, dict):
        return {}
    if "sync_names" in values:
        p.al_sync_names = bool(values["sync_names"])
    if "auto_blend" in values:
        p.al_auto_blend = bool(values["auto_blend"])
    if values.get("default_blend") in {i[0] for i in BLEND_ITEMS}:
        p.al_default_blend = values["default_blend"]
    return shared_prefs()


# ------------------------------------------------------------------ state

# ⚠ WRITES ARE COALESCED, AND THAT IS THE WHOLE PERFORMANCE STORY.
# A Blender property `update=` callback fires on EVERY step of a slider drag and
# runs ON THE UI THREAD, and each al_set_layer_state ends in `_al_touch()` - a
# frame_set that re-evaluates the whole depsgraph. On a 461-bone rig that is a
# full scene recompute per mouse-move, which is exactly the lag spike Marty
# reported. Headless the same call is 2.5 ms and looks harmless: the cost is the
# viewport re-evaluation, which does not exist in `-b`, so this can only be
# measured by hand in a real session.
# The app solved this once already with a 250 ms debounce on its own sliders
# (docs\app-shell.md). This is the same idea on bpy.app.timers.
PUSH_DELAY = 0.12

# ⚠ SELECTING A LAYER MEANS ENTERING NLA TWEAK MODE, and that is 60% of the cost
# of a click (measured: `_al_activate(tweak=True)` 3.60 ms against 0.03 ms with
# tweak off, on a 461-bone rig, headless - far more with a viewport to
# re-evaluate). It cannot be dropped: tweak mode IS what "selected" means here,
# it is why keys and auto-key land in that layer (docs\anim-layers.md).
#
# So it is taken OFF THE CLICK instead. The row highlight and the blend/influence
# widgets update immediately from the status already in hand, and the tweak-mode
# switch follows on a short timer. Clicking through several layers only pays for
# the one you settle on, and a single click FEELS instant because the panel
# repaints before the expensive part rather than after it.
#
# ⚠ The trade: for ~100 ms after a click, Blender is still tweaking the PREVIOUS
# layer, so a key inserted inside that window lands in the old one. Longer than
# any human click-then-reach, and every panel operator flushes the pending
# select before it runs - but it is a real window, and shortening SELECT_DELAY to
# 0 restores the old behaviour exactly.
SELECT_DELAY = 0.10

_PENDING = {}
_PENDING_AT = {}
_PENDING_SELECT = {}
_TIMER_ON = False


def _flush_now():
    """Apply whatever the panel last asked for. Split out of the timer so the
    tests can call it: ⚠ bpy.app.timers DO NOT FIRE in background Blender."""
    global _TIMER_ON
    _TIMER_ON = False
    if not _PENDING and not _PENDING_SELECT:
        return None
    # Selection first: a queued influence carries its own index, but a reader of
    # this code should not have to know that to be sure of the order.
    if _PENDING_SELECT:
        target = dict(_PENDING_SELECT)
        _PENDING_SELECT.clear()
        try:
            core.al_select_layer(target["index"],
                                 data_type=target.get("data_type", 'OBJECT'))
        except (RuntimeError, IndexError, KeyError, TypeError):
            pass
    if _PENDING:
        fields = dict(_PENDING)
        _PENDING.clear()
        try:
            core.al_set_layer_state(_PENDING_AT.get("index", 0),
                                    data_type=_PENDING_AT.get("data_type", 'OBJECT'),
                                    **fields)
        except (RuntimeError, IndexError, KeyError, TypeError):
            pass    # the stack moved under us; the next redraw shows the truth
    _invalidate()
    return None


def _arm(delay):
    global _TIMER_ON
    if _TIMER_ON:
        return
    _TIMER_ON = True
    try:
        bpy.app.timers.register(_flush_now, first_interval=delay)
    except (AttributeError, ValueError):
        _flush_now()            # no timers (background) - apply immediately


def _queue(field, value, index, data_type):
    """Remember a pending change and make sure a flush is coming."""
    _PENDING[field] = value
    _PENDING_AT["index"] = index
    _PENDING_AT["data_type"] = data_type
    _arm(PUSH_DELAY)


def _queue_select(index, data_type):
    """Ask for a layer to become the tweak target, shortly."""
    _PENDING_SELECT["index"] = index
    _PENDING_SELECT["data_type"] = data_type
    _arm(SELECT_DELAY)


def _push_blend(self, context):
    props = context.window_manager.madilib_al
    if props.suspend:
        return
    # Only the field that changed. Writing both on every event made a slider
    # drag re-send the blend type dozens of times for nothing.
    _queue("blend_type", props.blend_type, props.active_index, props.data_type)


def _push_influence(self, context):
    props = context.window_manager.madilib_al
    if props.suspend:
        return
    _queue("influence", props.influence, props.active_index, props.data_type)


class MADILIB_ALProps(PropertyGroup):
    data_type: EnumProperty(name="Layers for", items=DATA_ITEMS,
                            default='OBJECT')
    active_index: IntProperty(name="Active layer", default=0, min=0)
    new_name: StringProperty(name="Name", default="")
    # Mirrors of the ACTIVE layer, seeded when a layer is selected - never in
    # draw(), which must not write.
    blend_type: EnumProperty(name="Blend", items=BLEND_ITEMS, default='COMBINE',
                             update=_push_blend)
    influence: FloatProperty(name="Influence", default=1.0, min=0.0, max=1.0,
                             subtype='FACTOR', update=_push_influence)
    # Set while seeding, so writing the mirrors does not bounce straight back
    # into the stack as an edit.
    suspend: BoolProperty(default=False)
    selected_only: BoolProperty(
        name="Only selected bones", default=True,
        description="Act only on the bones selected in Blender")


# draw() runs on every redraw of the region - during a slider drag that is
# ~60 a second, each one walking the NLA tracks. The walk is only ~0.5 ms, but
# it is pure waste when nothing has changed, so the answer is cached and
# INVALIDATED EXPLICITLY (by every operator and every flush) rather than aged
# out on a timer. A stale panel would be worse than a slow one.
_CACHE = {}


def _invalidate():
    _CACHE.clear()


def _status(context, fresh=False):
    props = context.window_manager.madilib_al
    key = props.data_type
    if not fresh and key in _CACHE:
        return _CACHE[key]
    try:
        st = core.anim_layers_status(data_type=props.data_type)
    except Exception as exc:                       # noqa: BLE001 - drawing
        st = {"error": str(exc), "layers": []}
    _CACHE[key] = st
    return st


def _seed(context, index=None, status=None):
    """Copy the active layer's blend and influence into the widgets.

    `status` takes the dict an operator already got back - every `core.al_*`
    returns the whole status, so re-asking for it is a second walk for nothing.
    """
    props = context.window_manager.madilib_al
    st = status if isinstance(status, dict) and "layers" in status \
        else _status(context, fresh=True)
    _CACHE[props.data_type] = st
    layers = st.get("layers") or []
    if index is None:
        index = props.active_index
    if not layers:
        return st
    index = max(0, min(index, len(layers) - 1))
    row = layers[index]
    props.suspend = True
    try:
        props.active_index = index
        if row.get("blend_type") in {i[0] for i in BLEND_ITEMS}:
            props.blend_type = row["blend_type"]
        infl = row.get("influence")
        if isinstance(infl, (int, float)):
            props.influence = float(infl)
    finally:
        props.suspend = False
    return st


# -------------------------------------------------------------- operators


class _ALOp:
    """Every layer operator reports its failure instead of raising a traceback
    into the UI, and refreshes the widgets afterwards.

    ⚠ ANIM LAYERS IS FREE as of 2026-08-06 (Marty). Until then this class
    carried the licence gate — a `poll` returning `entitlement.unlocked()` and
    an `execute` that refused with LOCKED_HINT — on the principle that hiding UI
    is not gating, since every operator has a bl_idname and can be reached from
    the search menu, a keymap or a one-line script. That principle still holds
    for whatever IS gated; it just no longer applies here, so the checks were
    removed rather than left in place returning True.
    """

    bl_options = {'REGISTER', 'UNDO'}

    def run(self, context, props):
        raise NotImplementedError

    def execute(self, context):
        props = context.window_manager.madilib_al
        # Anything queued from a slider has to land BEFORE this runs, or the
        # flush would write an influence onto whatever layer the operator just
        # made active.
        _flush_now()
        self.status = None          # run() parks the reply it already got here
        try:
            message = self.run(context, props)
        except (RuntimeError, ValueError, IndexError, KeyError) as exc:
            _invalidate()
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        _seed(context, status=self.status)
        if message:
            self.report({'INFO'}, message)
        return {'FINISHED'}


class MADILIB_OT_al_select(_ALOp, Operator):
    bl_idname = "madilib.al_select"
    bl_label = "Select Layer"
    bl_description = "Make this the active layer"

    index: IntProperty(default=0)

    def run(self, context, props):
        st = _status(context)
        self.status = st            # so execute()'s _seed does not re-walk
        layers = st.get("layers") or []
        if not layers or not 0 <= self.index < len(layers):
            return ""
        # Already the tweak target AND already highlighted: nothing to do.
        if self.index == props.active_index and st.get("active_index") == self.index:
            return ""
        # The cheap half NOW, from what we already know - highlight and widgets.
        _seed(context, self.index, status=st)
        # The expensive half (NLA tweak mode) on a short timer. See SELECT_DELAY.
        _queue_select(self.index, props.data_type)
        return ""


class MADILIB_OT_al_add(_ALOp, Operator):
    bl_idname = "madilib.al_add"
    bl_label = "Add Layer"
    bl_description = "Add a new animation layer above the current one"

    def run(self, context, props):
        prefs = shared_prefs()
        # ⚠ Every core.al_* call returns the WHOLE status dict with its result
        # under a key of its own - not a small info dict. Reading info["name"]
        # here silently produced "Added layer '?'".
        self.status = status = core.al_add_layer(
            data_type=props.data_type, name=props.new_name.strip() or None,
            blend_type=prefs.get("default_blend", 'COMBINE'))
        props.new_name = ""
        return "Added layer '%s'" % status.get("added", "?")


class MADILIB_OT_al_delete(_ALOp, Operator):
    bl_idname = "madilib.al_delete"
    bl_label = "Delete Layer"
    bl_description = "Delete the active layer"

    def run(self, context, props):
        self.status = status = core.al_delete_layer(props.active_index,
                                      data_type=props.data_type)
        return "Deleted '%s'" % status.get("deleted", "?")


class MADILIB_OT_al_duplicate(_ALOp, Operator):
    bl_idname = "madilib.al_duplicate"
    bl_label = "Duplicate Layer"
    bl_description = "Copy the active layer into a new one above it"

    linked: BoolProperty(name="Linked", default=False,
                         description="Share the action instead of copying it")

    def run(self, context, props):
        self.status = status = core.al_duplicate_layer(props.active_index,
                                         linked=self.linked,
                                         data_type=props.data_type)
        return "Duplicated to '%s'" % status.get("duplicated", "?")


class MADILIB_OT_al_move(_ALOp, Operator):
    bl_idname = "madilib.al_move"
    bl_label = "Move Layer"
    bl_description = "Move the active layer up or down the stack"

    direction: EnumProperty(items=[('UP', "Up", ""), ('DOWN', "Down", "")],
                            default='UP')

    def run(self, context, props):
        self.status = status = core.al_move_layer(props.active_index, self.direction,
                                     data_type=props.data_type)
        moved = (status.get("moved") or {}).get("layer")
        for row in status.get("layers") or []:
            if row.get("name") == moved:
                props.active_index = row["index"]
                break
        return ""


class MADILIB_OT_al_state(_ALOp, Operator):
    bl_idname = "madilib.al_state"
    bl_label = "Toggle Layer State"
    bl_description = "Mute or lock this layer"

    index: IntProperty(default=0)
    field: EnumProperty(items=[('mute', "Mute", ""), ('lock', "Lock", "")],
                        default='mute')
    value: BoolProperty(default=True)

    def run(self, context, props):
        self.status = core.al_set_layer_state(
            self.index, data_type=props.data_type, **{self.field: self.value})
        return ""


class MADILIB_OT_al_solo(_ALOp, Operator):
    bl_idname = "madilib.al_solo"
    bl_label = "Solo Layer"
    bl_description = "Hear this layer on its own. Press again to release"

    index: IntProperty(default=0)
    off: BoolProperty(default=False)

    def run(self, context, props):
        self.status = core.al_solo(None if self.off else self.index,
                                   data_type=props.data_type)
        return ""


class MADILIB_OT_al_rename(_ALOp, Operator):
    bl_idname = "madilib.al_rename"
    bl_label = "Rename Layer"
    bl_description = "Rename the active layer"

    name: StringProperty(name="Name", default="")

    def invoke(self, context, event):
        st = _status(context)
        layers = st.get("layers") or []
        props = context.window_manager.madilib_al
        if 0 <= props.active_index < len(layers):
            self.name = layers[props.active_index].get("name", "")
        return context.window_manager.invoke_props_dialog(self)

    def run(self, context, props):
        if not self.name.strip():
            raise ValueError("A layer needs a name")
        self.status = core.al_rename_layer(
            props.active_index, self.name.strip(),
            sync_action=shared_prefs().get("sync_names", True),
            data_type=props.data_type)
        return "Renamed to '%s'" % self.name.strip()


class MADILIB_OT_al_select_bones(_ALOp, Operator):
    bl_idname = "madilib.al_select_bones"
    bl_label = "Select Bones"
    bl_description = "Select the bones this layer animates"

    def run(self, context, props):
        self.status = status = core.al_select_bones_in_layer(index=props.active_index,
                                               data_type=props.data_type)
        got = (status.get("selected_bones") or {}).get("bones") or []
        return "Selected %d bone(s)" % len(got)


class MADILIB_OT_al_reset(_ALOp, Operator):
    bl_idname = "madilib.al_reset"
    bl_label = "Reset Layer"
    bl_description = "Key rest values into THIS layer so it stops affecting " \
                     "the scoped bones. Other layers are untouched"

    def run(self, context, props):
        self.status = status = core.al_reset_layer(index=props.active_index,
                                     selected_only=props.selected_only,
                                     data_type=props.data_type)
        info = status.get("reset") or {}
        return "Reset %d channel(s) in '%s'" % (info.get("channels", 0),
                                                info.get("layer", "?"))


class MADILIB_OT_al_cyclic(_ALOp, Operator):
    bl_idname = "madilib.al_cyclic"
    bl_label = "Cyclic"
    bl_description = "Add or remove a Cycles modifier so the layer repeats"

    enable: BoolProperty(default=True)

    def run(self, context, props):
        self.status = status = core.al_cyclic_fcurves(index=props.active_index,
                                        enable=self.enable,
                                        selected_only=props.selected_only,
                                        data_type=props.data_type)
        info = status.get("cyclic") or {}
        return "%s cyclic on %d curve(s)" % (
            "Made" if self.enable else "Removed", info.get("curves", 0))


class MADILIB_OT_al_extract(_ALOp, Operator):
    bl_idname = "madilib.al_extract"
    bl_label = "Extract Bones"
    bl_description = "Move the scoped curves into a new layer above this one"

    def run(self, context, props):
        self.status = status = core.al_extract_bones(index=props.active_index,
                                       selected_only=props.selected_only,
                                       data_type=props.data_type)
        info = status.get("extracted") or {}
        return "Moved %d curve(s) into '%s'" % (info.get("curves", 0),
                                                info.get("layer", "?"))


class MADILIB_OT_al_sync_names(_ALOp, Operator):
    bl_idname = "madilib.al_sync_names"
    bl_label = "Sync Action Names"
    bl_description = "Rename each layer's action to match the layer"

    def run(self, context, props):
        renamed = core.al_sync_layer_names(data_type=props.data_type)
        _invalidate()
        return "Renamed %d action(s)" % len(renamed or [])


class MADILIB_OT_al_adopt_nla(_ALOp, Operator):
    bl_idname = "madilib.al_adopt_nla"
    bl_label = "Use Existing NLA as Layers"
    bl_description = "Treat the NLA tracks already on this object as layers"

    def run(self, context, props):
        self.status = status = core.al_adopt_nla(data_type=props.data_type)
        info = status.get("adopted") or {}
        return "Adopted %d track(s)" % len(info.get("layers") or [])


# ------------------------------------------------------------------- menu


class MADILIB_MT_al_tools(Menu):
    """The dropdown Marty asked for: everything past the everyday controls, so
    the panel stays short."""

    bl_idname = "MADILIB_MT_al_tools"
    bl_label = "Layer Tools"

    def draw(self, context):
        props = context.window_manager.madilib_al
        layout = self.layout
        layout.prop(props, "selected_only")
        layout.separator()
        layout.operator("madilib.al_rename", icon='OUTLINER_DATA_FONT')
        layout.operator("madilib.al_select_bones", icon='BONE_DATA')
        layout.operator("madilib.al_reset", icon='LOOP_BACK')
        layout.separator()
        layout.operator("madilib.al_cyclic", text="Make Cyclic",
                        icon='FORCE_HARMONIC').enable = True
        layout.operator("madilib.al_cyclic", text="Remove Cyclic",
                        icon='X').enable = False
        layout.separator()
        layout.operator("madilib.al_extract", icon='UNLINKED')
        layout.operator("madilib.al_duplicate", icon='DUPLICATE')
        layout.separator()
        layout.operator("madilib.al_sync_names", icon='SYNTAX_OFF')
        layout.operator("madilib.al_adopt_nla", icon='NLA')


# ------------------------------------------------------------------ panel


class MADILIB_PT_anim_layers(Panel):
    bl_idname = "MADILIB_PT_anim_layers"
    bl_label = "Animation Layers"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadihsonNSFW"
    # Collapsed by default - this sits under Studio Library in the same tab and
    # most sessions never open it.
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        props = context.window_manager.madilib_al

        # ⚠ NO LOCKED CARD — Anim Layers is free (2026-08-06). This used to
        # read `entitlement.state()` and draw a "Locked / sign in" card in place
        # of the whole panel.
        layout.prop(props, "data_type", text="")

        st = _status(context)
        if st.get("error"):
            layout.label(text=st["error"], icon='ERROR')
            return
        layers = st.get("layers") or []
        if not layers:
            layout.label(text="No layers yet.", icon='INFO')
            if st.get("foreign_nla"):
                layout.operator("madilib.al_adopt_nla", icon='NLA')
            layout.operator("madilib.al_add", icon='ADD')
            return

        solo_name = st.get("solo")
        box = layout.box()
        col = box.column(align=True)
        # Top of the stack first, the way every layer UI shows it.
        for row_data in reversed(layers):
            i = row_data["index"]
            row = col.row(align=True)
            active = (i == props.active_index)
            row.operator("madilib.al_select", text=row_data["name"],
                         emboss=active,
                         icon='LAYER_ACTIVE' if active else 'LAYER_USED').index = i
            sub = row.row(align=True)
            solo_on = bool(solo_name) and row_data.get("solo")
            op = sub.operator("madilib.al_solo", text="",
                              icon='SOLO_ON' if solo_on else 'SOLO_OFF',
                              depress=solo_on)
            op.index, op.off = i, solo_on
            op = sub.operator("madilib.al_state", text="",
                              icon='CHECKBOX_DEHLT' if row_data["mute"]
                              else 'CHECKBOX_HLT')
            op.index, op.field, op.value = i, 'mute', not row_data["mute"]
            op = sub.operator("madilib.al_state", text="",
                              icon='LOCKED' if row_data["lock"] else 'UNLOCKED')
            op.index, op.field, op.value = i, 'lock', not row_data["lock"]

        row = layout.row(align=True)
        row.operator("madilib.al_add", text="", icon='ADD')
        row.operator("madilib.al_delete", text="", icon='REMOVE')
        row.separator()
        row.operator("madilib.al_move", text="", icon='TRIA_UP').direction = 'UP'
        row.operator("madilib.al_move", text="", icon='TRIA_DOWN').direction = 'DOWN'
        row.separator()
        row.menu("MADILIB_MT_al_tools", text="", icon='DOWNARROW_HLT')

        if 0 <= props.active_index < len(layers):
            active = layers[props.active_index]
            col = layout.column(align=True)
            col.prop(props, "blend_type", text="")
            col.prop(props, "influence", slider=True)
            if active.get("locked_reason"):
                layout.label(text=active["locked_reason"], icon='LOCKED')
        if st.get("in_tweak"):
            layout.label(text="In tweak mode", icon='INFO')


class MADILIB_PT_anim_layers_options(Panel):
    """Shared with the app - see the module docstring."""

    bl_idname = "MADILIB_PT_anim_layers_options"
    bl_label = "Options"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MadihsonNSFW"
    bl_parent_id = "MADILIB_PT_anim_layers"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        prefs = _prefs()
        layout = self.layout
        if prefs is None:
            layout.label(text="Preferences unavailable", icon='ERROR')
            return
        col = layout.column()
        col.prop(prefs, "al_sync_names")
        col.prop(prefs, "al_auto_blend")
        col.prop(prefs, "al_default_blend")
        col.label(text="Shared with the Toolset app.", icon='INFO')


_classes = (
    MADILIB_ALProps,
    MADILIB_OT_al_select,
    MADILIB_OT_al_add,
    MADILIB_OT_al_delete,
    MADILIB_OT_al_duplicate,
    MADILIB_OT_al_move,
    MADILIB_OT_al_state,
    MADILIB_OT_al_solo,
    MADILIB_OT_al_rename,
    MADILIB_OT_al_select_bones,
    MADILIB_OT_al_reset,
    MADILIB_OT_al_cyclic,
    MADILIB_OT_al_extract,
    MADILIB_OT_al_sync_names,
    MADILIB_OT_al_adopt_nla,
    MADILIB_MT_al_tools,
    MADILIB_PT_anim_layers,
    MADILIB_PT_anim_layers_options,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.madilib_al = bpy.props.PointerProperty(
        type=MADILIB_ALProps)


def unregister():
    del bpy.types.WindowManager.madilib_al
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
