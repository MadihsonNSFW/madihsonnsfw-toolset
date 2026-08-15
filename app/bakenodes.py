"""The bake node set — the Node Editor's first REAL nodes (2026-08-07).

Marty's layout, chosen 2026-08-06 over two alternatives he was shown, with
his 2026-08-07 renames — the target node is titled **Bake** (it used to say
"Shader name") and the options node **Bake settings**, so only one node in
the graph is called Bake:

    Bake (material + all slots) ──┐ green
                                  ├─> Bake settings (type, resolution,
    Bulk bake (selection/folder)──┘     options + the button) ──orange──>
                                        Output image

The wiring is SEMANTIC: pressing the button walks the wires from that
Bake-settings node — through any reroute dots — to find its source node
and its Output node, so duplicates and rewiring behave the way a node
editor should. The bake itself is the add-on's `bake_texture`
(texbake.py) — NATIVE since 0.29.0: Blender's own operator with the
panel's own options, sampling/denoising/device left to the scene (the
0.24–0.28 "fast engine" is gone; Marty tested the real panel, saw no
seams, and had this rebuilt around it).

⚠ **NEITHER SOURCE BAKES ALONE** (Marty, 2026-08-07 pm). The **Bulk bake**
node finds its targets from the viewport selection or a collection
(`bake_targets`, add-on 0.26.0) rather than naming a material, but like
the Bake node it must reach a Bake-settings node — which must reach an
Output image node — or the press refuses in words. Its button is a
shortcut for pressing Bake over there, so a bulk run and a single run
take their type, resolution and options from one place.

Node classes subclass nodecanvas.NodeItem and add clickable rows: a click
on a value pill pops a menu / dialog, a click anywhere else drags the node
like always. The buttons and the Output preview are extra painted areas
below the rows (self.h grows; sockets sit in the row band, so socket_pos
is untouched).

Since 0.29.0 the Bake-settings node IS Blender's Bake panel: every
option the panel draws, in the panel's order and visibility, REBUILT per
type — the tables below (INFLUENCE / NO_VIEW_FROM / DATA_TYPES /
TARGET_LABELS) are Blender's own rules, mirrored from the add-on's
texbake.py and pinned against it by the suite.

⚠ `build_bake_graph`'s dict keys ("shader", "bake", …) PREDATE the renames
and stay as they are — they are internal handles the suites lean on, not
UI text.
"""

import os

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPen
from PySide6.QtWidgets import (QFileDialog, QInputDialog, QLineEdit, QMenu,
                               QWidgetAction)

import config
import theme
from nodecanvas import (HEADER_H, NODE_RADIUS, ROW_GAP, ROW_H, NodeItem,
                        RerouteItem)

# ⚠ A SOCKET'S COLOUR IS ITS TYPE (Marty, 2026-08-07: "can only enter in
# sockets that are green"). Both bake SOURCES — the Bake node and the Bulk
# bake node — send green; only the Bake-settings input takes green. The
# canvas refuses any drag between two different colours, so a wrong wire
# cannot be drawn in the first place (nodecanvas.socket_colour).
COL_MATERIAL = QColor("#4fc07a")     # a bake TARGET: one material, or many
COL_BAKED = QColor("#e0704f")        # the baked result

BAKE_TYPES = (
    "Combined", "Ambient Occlusion", "Shadow", "Position", "Normal", "UV",
    "Roughness", "Emission", "Environment", "Diffuse", "Glossy",
    "Transmission")
# UI label -> the cycles enum the bridge speaks
BAKE_ENUM = {
    "Combined": "COMBINED", "Ambient Occlusion": "AO", "Shadow": "SHADOW",
    "Position": "POSITION", "Normal": "NORMAL", "UV": "UV",
    "Roughness": "ROUGHNESS", "Emission": "EMIT",
    "Environment": "ENVIRONMENT", "Diffuse": "DIFFUSE", "Glossy": "GLOSSY",
    "Transmission": "TRANSMISSION"}

RES_PRESETS = (512, 1024, 2048, 4096)
BUTTON_H = 24
PREVIEW_H = 128
BAKE_W = 230                 # the widest node: "Transmission" + a long enum

# The Bulk bake node's two ways of finding targets (Marty, 2026-08-07):
# "bulk bake" = whatever meshes are selected in the viewport, "folder bake"
# = whatever a named collection holds. UI label -> the bake_targets mode.
BULK_MODES = (("Bulk bake", "SELECTED"), ("Folder bake", "COLLECTION"))

# What the add menus offer, in one place so the toolbar "+ Add node" and
# Shift+A can never drift apart. Reroute dots are appended by the tab (they
# take a colour, not a tab).
# ⚠ "Image texture" was REMOVED 2026-08-07 — its resolution moved onto the
# Bake settings node (Marty), which also retired the second input socket,
# the one that sat beside the Samples row and read like a Samples input.
NODE_KINDS = ("Bake", "Bulk bake", "Collection", "Map set", "Bake settings",
              "Output image")

# What a fresh Map set node ticks: the three maps a PBR material actually
# needs. Ticking all twelve by default would make the first press of a new
# node a twelve-map run nobody asked for.
MAP_SET_DEFAULT = ("Diffuse", "Roughness", "Normal")


def default_bake_dir():
    """The designated output folder: `baked\\` next to config.json — beside
    the exe when frozen, beside main.py from source (the render_presets
    pattern). ⚠ Anything the app keeps beside the exe must ALSO be in
    make_release.js NEVER_SHIP_DIRS, or a release ships (and an update
    sweeps) the user's baked maps."""
    return os.path.join(config.APP_DIR, "baked")


def sanitize_name(name):
    """The add-on's own filename rule (texbake._resolve_path), mirrored so
    an auto name the app builds is the name the file really gets."""
    return "".join(c if c.isalnum() or c in "-_ " else "_" for c in str(name))


def auto_out_path(material, folder=None):
    """Where a bake lands when the Output node's name is not filled:
    <folder>\\<material>_baked — EXTENSIONLESS on purpose, because the
    add-on appends .png (or .exr for POSITION, whose -1..1 values a PNG
    would clamp) and the app must not second-guess that rule."""
    return os.path.join(folder or default_bake_dir(),
                        sanitize_name(material) + "_baked")

# ⚠ These three MIRROR the add-on's texbake.py, which read them off
# Blender's own cycles/ui.py — which bake type offers which options is
# Blender's rule, not ours. `app_nodeeditor_test.py` pins this copy against
# the add-on's so they can never drift.
DATA_TYPES = {"NORMAL", "ROUGHNESS", "UV", "POSITION"}
NO_VIEW_FROM = {"AO", "POSITION", "NORMAL", "UV", "ROUGHNESS", "ENVIRONMENT"}
INFLUENCE = {
    "COMBINED": ("DIRECT", "INDIRECT", "DIFFUSE", "GLOSSY", "TRANSMISSION",
                 "EMIT"),
    "DIFFUSE": ("DIRECT", "INDIRECT", "COLOR"),
    "GLOSSY": ("DIRECT", "INDIRECT", "COLOR"),
    "TRANSMISSION": ("DIRECT", "INDIRECT", "COLOR"),
}
PASS_LABELS = {"DIRECT": "Direct", "INDIRECT": "Indirect", "COLOR": "Color",
               "DIFFUSE": "Diffuse", "GLOSSY": "Glossy",
               "TRANSMISSION": "Transmission", "EMIT": "Emit"}

# UI label -> the enum the bridge speaks, same shape as BAKE_ENUM.
VIEW_FROM_LABELS = (("Above Surface", "ABOVE_SURFACE"),
                    ("Active Camera", "ACTIVE_CAMERA"))
SPACE_LABELS = (("Tangent", "TANGENT"), ("Object", "OBJECT"))
SWIZZLE_LABELS = (("+X", "POS_X"), ("+Y", "POS_Y"), ("+Z", "POS_Z"),
                  ("−X", "NEG_X"), ("−Y", "NEG_Y"), ("−Z", "NEG_Z"))
MARGIN_TYPE_LABELS = (("Adjacent Faces", "ADJACENT_FACES"),
                      ("Extend", "EXTEND"))
# The panel's Output > Target, labels Blender's own: an image, or the
# mesh's active color attribute. A color-attribute bake has no image and
# no file — the Resolution row and the Output node's file/preview/Replace
# shader work all stand down for it, each saying so.
TARGET_LABELS = (("Image Textures", "IMAGE_TEXTURES"),
                 ("Active Color Attribute", "VERTEX_COLORS"))
SWIZZLE_DEFAULT = ("POS_X", "POS_Y", "POS_Z")
SAMPLE_PRESETS = (8, 16, 32, 64, 128, 256, 512)
MAX_SAMPLES = 4096


def _label_of(pairs, value):
    for label, enum_value in pairs:
        if enum_value == value:
            return label
    return str(value)


def _window(item):
    scene = item.scene()
    views = scene.views() if scene else []
    return views[0].window() if views else None


def filter_names(names, query):
    """Marty, 2026-08-08: *"ability to search for a shader from this node
    (shaders should show even if partial match)"*.

    Case-insensitive, and every whitespace-separated token has to appear
    SOMEWHERE in the name — so "lily body" finds "Lily Bodysuit" and
    "body" alone finds it too. Ranked rather than merely filtered: a name
    the query starts sorts above one that only contains it, ties keeping
    the scene's own order, because the top hit is what Enter takes.

    An empty query is not a filter — it hands back everything, in order."""
    rows = [str(n) for n in (names or [])]
    tokens = [t for t in str(query or "").lower().split() if t]
    if not tokens:
        return rows
    scored = []
    for index, name in enumerate(rows):
        low = name.lower()
        if not all(token in low for token in tokens):
            continue
        if low == tokens[0]:
            rank = 0
        elif low.startswith(tokens[0]):
            rank = 1
        else:
            rank = 2
        scored.append((rank, index, name))
    return [name for _rank, _index, name in sorted(scored)]


class SearchMenu(QMenu):
    """A pick-one menu with a search box in its first row.

    His scene carries 29 materials and a G8 body's list is longer still —
    a flat QMenu of every one of them is a scroll, not a choice. Typing
    narrows the list live (`filter_names`), Enter takes the best match,
    and an untouched menu is exactly the old flat list, so nothing is lost
    for a two-material scene.

    ⚠ The actions are HIDDEN, never rebuilt. Rebuilding a QMenu's actions
    while it is open invalidates whatever the mouse is currently over, and
    the lambdas that carry each name would have to be rebuilt with them —
    `setVisible` keeps one action per name alive for the menu's whole
    life."""

    def __init__(self, names, on_pick, parent=None,
                 placeholder="Search… (partial matches count)"):
        super().__init__(parent)
        self._on_pick = on_pick
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        self.edit.setClearButtonEnabled(True)
        holder = QWidgetAction(self)
        holder.setDefaultWidget(self.edit)
        self.addAction(holder)
        self.addSeparator()
        self.entries = []
        for name in names:
            action = self.addAction(name, lambda n=name: self._pick(n))
            self.entries.append((name, action))
        self.empty_action = self.addAction("(nothing matches)")
        self.empty_action.setEnabled(False)
        self.empty_action.setVisible(False)
        self.edit.textChanged.connect(self.apply_filter)
        self.edit.returnPressed.connect(self.accept_best)

    def apply_filter(self, text):
        keep = set(filter_names([n for n, _a in self.entries], text))
        for name, action in self.entries:
            action.setVisible(name in keep)
        self.empty_action.setVisible(not keep and bool(self.entries))

    def best_match(self):
        """The name Enter would take — the filter's own ranking, not menu
        order, so typing "bod" takes "Bodysuit" over "Lily Bodysuit"."""
        ranked = filter_names([n for n, _a in self.entries], self.edit.text())
        return ranked[0] if ranked else None

    def accept_best(self):
        name = self.best_match()
        if name is not None:
            self.close()
            self._pick(name)

    def _pick(self, name):
        self._on_pick(name)

    def showEvent(self, event):
        """⚠ A QMenu takes the keyboard for itself, and its own type-ahead
        would eat the first letters typed. Handing focus to the line edit
        as the menu appears is what makes it a search box instead of a
        decoration."""
        super().showEvent(event)
        self.edit.setFocus(Qt.PopupFocusReason)


class FieldNode(NodeItem):
    """A NodeItem whose value pills are clickable. Subclasses fill
    self.fields with row-index -> handler; everything else is a plain
    node."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields = {}

    def set_row(self, index, value):
        label, _old = self.rows[index]
        self.rows[index] = (label, value)
        self.update()

    def _row_index_at(self, pos):
        """Row index if pos sits in the value column, else None. Section
        headings never carry a field, so they simply fall through to the
        drag."""
        if pos.x() < self.value_x() or pos.x() > self.w - 6:
            return None
        for i in range(len(self.rows)):
            y = HEADER_H + ROW_GAP + i * (ROW_H + ROW_GAP)
            if y <= pos.y() <= y + ROW_H:
                return i
        return None

    def _menu(self, event, pairs, current, apply):
        """A pick-one menu with the live value ticked."""
        menu = QMenu()
        for label, value in pairs:
            action = menu.addAction(label, lambda v=value: apply(v))
            action.setCheckable(True)
            action.setChecked(value == current)
        menu.exec(event.screenPos())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            i = self._row_index_at(event.pos())
            if i is not None and i in self.fields:
                event.accept()
                self.fields[i](event)
                return
        super().mousePressEvent(event)


class BakeTargetNode(FieldNode):
    """Titled **Bake** (Marty's 2026-08-07 rename of "Shader name"): names
    the material to bake, and — since the same day — whether to bake EVERY
    slot of the object that carries it. The pill pops the live material
    list from Blender (or a typed name when the bridge is down)."""

    def __init__(self, tab):
        super().__init__("Bake", theme.TYPE_COLORS["set"],
                         rows=[("Material", "(pick…)"),
                               ("Bake all slots", False)],
                         outputs=[("Material", COL_MATERIAL)])
        self.tab = tab
        self.material = None
        self.all_slots = False
        self.help_text = (
            "Names ONE material to bake. Click the pill to search your "
            "scene's materials — partial matches count, so \"body\" finds "
            "\"Lily Bodysuit\".\n\n"
            "Bake all slots: instead of that one material, bake every "
            "material slot of the object carrying it, one map each.\n\n"
            "It cannot bake on its own: wire it into a Bake settings node, "
            "which must reach an Output image node.")
        self.fields = {0: self.pick_material,
                       1: lambda e: self.toggle_all_slots()}

    def build_material_menu(self):
        """The material menu, built but not shown — a SearchMenu when there
        is a live list to search, a plain refusal menu otherwise. Split out
        from `pick_material` so the suite can inspect the real menu without
        driving a modal popup."""
        names = self.tab.material_names()
        if names:
            menu = SearchMenu(names, self.set_material)
        else:
            menu = QMenu()
            menu.addAction("(Blender unreachable)" if names is None
                           else "(no materials in the scene)").setEnabled(False)
        menu.addSeparator()
        menu.addAction("Type a name…", self.type_material)
        return menu

    def pick_material(self, event):
        self.build_material_menu().exec(event.screenPos())

    def set_material(self, name):
        self.material = name
        self.set_row(0, name)

    def toggle_all_slots(self):
        """Every material slot of the object carrying the picked material,
        one map per slot, each auto-named <material>_baked. Resolved at
        press time by the add-on (`bake_targets mode=material`) so the app
        and `bake_texture` can never disagree about which object that is."""
        self.all_slots = not self.all_slots
        self.set_row(1, self.all_slots)

    def type_material(self):
        name, ok = QInputDialog.getText(_window(self), "Material",
                                        "Material name:",
                                        text=self.material or "")
        if ok and name.strip():
            self.set_material(name.strip())

    # what "remember node settings" stores / restores (nodecanvas tab)
    def settings_dict(self):
        return {"material": self.material, "all_slots": self.all_slots}

    def apply_settings(self, d):
        if d.get("material"):
            self.set_material(str(d["material"]))
        self.all_slots = bool(d.get("all_slots", False))
        self.set_row(1, self.all_slots)


class BakeSettingsNode(FieldNode):
    """Titled **Bake settings** (2026-08-07 — the target node took the name
    "Bake", and two nodes must not share a title): Blender's Bake panel,
    row for row, and the button that runs the bake through the bridge.

    Marty, 2026-08-08: *"do EXACTLY the way it is done in blender just in
    our UI with all the bake options they have"* — after testing the real
    panel and seeing none of the seams our 0.28.x pipeline gave him. So
    the row list IS the panel now: the twelve types, View From, the
    per-type Influence block, Selected to Active with the cage family,
    Target, Clear Image and Margin, in the panel's own order and
    visibility (`INFLUENCE` / `NO_VIEW_FROM` and the margin rule are read
    off cycles/ui.py in 5.2, and the add-on mirrors the same tables).
    Only Resolution and Samples are ours: the New-Image stand-in and a
    scene.cycles.samples override. The 0.28.x View transform and Denoise
    rows are gone — neither is a Bake panel option."""

    def __init__(self, tab):
        # ⚠ ONE input since 2026-08-07. The second one (Image) sat in the
        # row band beside "Samples" and read like a Samples socket, which
        # is what Marty saw; the Image-texture node it came from is gone
        # and its resolution lives on this node now.
        # ⚠ That one input became MULTI on 2026-08-08 (drawn hollow and
        # bigger): *"both Bake and 'Collection' nodes can be wired in bake
        # settings in the same time"*. It is the reason `add_wire`'s
        # one-wire-per-input rule no longer replaces a wire here.
        super().__init__("Bake settings", theme.TYPE_COLORS["renderpreset"],
                         inputs=[("Material", COL_MATERIAL, True)],
                         outputs=[("Baked", COL_BAKED)],
                         width=BAKE_W, label_frac=0.46)
        self.tab = tab
        # Defaults = a fresh Blender scene's Bake panel, field for field
        # (swizzle checked against a live 5.2 scene — the RNA property
        # default claims POS_X for all three and is wrong).
        self.bake_type = "Combined"
        self.width_px = 1024
        self.height_px = 1024
        self.samples = None                # None = the scene's own samples
        self.view_from = "ABOVE_SURFACE"
        self.normal_space = "TANGENT"
        self.swizzle = list(SWIZZLE_DEFAULT)
        self.passes = {flag: True for flag in PASS_LABELS}  # Blender's own
        self.selected_to_active = False
        self.use_cage = False
        self.cage_object = ""
        self.cage_extrusion = 0.0
        self.max_ray_distance = 0.0
        self.target = "IMAGE_TEXTURES"
        self.use_clear = True
        self.margin = 16
        self.margin_type = "ADJACENT_FACES"
        self.help_text = (
            "Blender's Bake panel, row for row — and pressing Bake here "
            "calls Blender's own bake operator with exactly these values.\n\n"
            "The rows change with the Type, the way Blender's panel does: "
            "View From, the Influence block, Selected to Active with its "
            "cage family, Target, Clear Image and Margin.\n\n"
            "Resolution and Samples are the two that are ours. Samples on "
            "\"Scene\" uses your scene's own render samples, which is what "
            "the panel's button does.\n\n"
            "It needs a source wired in (Bake, Bulk bake or Collection) and "
            "an Output image node on its output.")
        self.set_extra_height(BUTTON_H + ROW_GAP)
        self.rebuild()

    # --- the rows ---------------------------------------------------------

    def enum(self):
        """The Cycles enum for the picked type — every rule keys off this,
        never off the display label."""
        return BAKE_ENUM[self.bake_type]

    def samples_text(self):
        if self.samples is not None:
            return str(self.samples)
        return "Scene"        # Blender's own render samples — the native way

    def resolution_text(self):
        return "%d × %d" % (self.width_px, self.height_px)

    def rebuild(self):
        """Rebuild the whole row list for the current type — the rows ARE
        the Bake panel, in the panel's own order and visibility: Type,
        View From, the per-type Influence block, Selected to Active with
        its cage family, then Output (Target / Clear Image / Margin).
        Resolution (our New-Image stand-in; absent for a color-attribute
        bake, which has no image) and Samples close the list. `fields` and
        `dim_rows` go with it — both are keyed by ROW INDEX, so they can
        never be updated separately."""
        etype = self.enum()
        to_image = self.target == "IMAGE_TEXTURES"
        rows, fields, dim = [], {}, set()

        def add(label, value, handler=None, dimmed=False):
            index = len(rows)
            rows.append((label, value))
            if handler is not None:
                fields[index] = handler
            if dimmed:
                dim.add(index)

        add("Type", self.bake_type, self.pick_type)
        if etype not in NO_VIEW_FROM:
            add("View From", _label_of(VIEW_FROM_LABELS, self.view_from),
                self.pick_view_from)

        if etype == "NORMAL":
            add("Influence", None)
            add("Space", _label_of(SPACE_LABELS, self.normal_space),
                self.pick_space)
            for axis, label in enumerate(("Swizzle R", "G", "B")):
                add(label, _label_of(SWIZZLE_LABELS, self.swizzle[axis]),
                    lambda e, a=axis: self.pick_swizzle(e, a))
        elif etype == "COMBINED":
            # ⚠ Blender greys the contributions out when neither Direct nor
            # Indirect is on (layout.active = False) — dimmed but still
            # clickable, which is what dim_rows draws.
            lit = self.passes["DIRECT"] or self.passes["INDIRECT"]
            add("Lighting", None)
            for flag in ("DIRECT", "INDIRECT"):
                add(PASS_LABELS[flag], self.passes[flag],
                    lambda e, f=flag: self.toggle_pass(f))
            add("Contributions", None)
            for flag in ("DIFFUSE", "GLOSSY", "TRANSMISSION", "EMIT"):
                add(PASS_LABELS[flag], self.passes[flag],
                    lambda e, f=flag: self.toggle_pass(f), dimmed=not lit)
        elif etype in INFLUENCE:
            add("Contributions", None)
            for flag in INFLUENCE[etype]:
                add(PASS_LABELS[flag], self.passes[flag],
                    lambda e, f=flag: self.toggle_pass(f))

        # Selected to Active — the panel's sub-panel with the tickbox in
        # its header; here the tick IS the header and the body rows appear
        # under it (the panel greys the body when the tick is off; absent
        # rows say the same thing in this node's own idiom).
        add("Selected to Active", self.selected_to_active,
            lambda e: self.toggle_s2a())
        if self.selected_to_active:
            add("Cage", self.use_cage, lambda e: self.toggle_cage())
            if self.use_cage:
                add("Cage Object", self.cage_object or "—",
                    self.pick_cage_object)
                # ⚠ dimmed once a cage OBJECT is named — Blender's own
                # rule (the named object replaces the extrusion): dim,
                # still clickable
                add("Cage Extrusion", "%.2f m" % self.cage_extrusion,
                    self.pick_cage_extrusion,
                    dimmed=bool(self.cage_object))
            else:
                add("Extrusion", "%.2f m" % self.cage_extrusion,
                    self.pick_cage_extrusion)
            add("Max Ray Distance", "%.2f m" % self.max_ray_distance,
                self.pick_ray_distance)

        add("Output", None)
        add("Target", _label_of(TARGET_LABELS, self.target),
            self.pick_target)
        if to_image:
            add("Clear Image", self.use_clear,
                lambda e: self.toggle_clear())
            # ⚠ Blender hides the margin TYPE for tangent-space Normal and
            # UV bakes and keeps the size (CYCLES_RENDER_PT_bake_output_
            # margin in 5.2) — same rule, same order (Type above Size).
            size_only = ((etype == "NORMAL"
                          and self.normal_space == "TANGENT")
                         or etype == "UV")
            if not size_only:
                add("Margin type", _label_of(MARGIN_TYPE_LABELS,
                                             self.margin_type),
                    self.pick_margin_type)
            add("Margin", "%d px" % self.margin, self.pick_margin)
            # The Image-texture node's job, moved here 2026-08-07 (Marty):
            # the same presets, plus a custom size, on one row.
            add("Resolution", self.resolution_text(), self.pick_resolution)
        add("Samples", self.samples_text(), self.pick_samples)

        self.fields = fields
        self.dim_rows = dim
        self.set_rows(rows)

    def pass_filter_for(self, etype):
        """The ticked contributions `etype` actually offers, or None for a
        type with no Influence panel at all.

        ⚠ Keyed on the type PASSED IN, not on this node's Type row — a Map
        set node bakes several types through one settings node, and
        forwarding Combined's EMIT to a Diffuse bake is exactly the
        "contributions a type does not offer" mistake this drops."""
        offered = INFLUENCE.get(etype)
        if not offered:
            return None
        return [flag for flag in offered if self.passes[flag]]

    def pass_filter(self):
        """The contributions for this node's own Type row."""
        return self.pass_filter_for(self.enum())

    # --- the pickers ------------------------------------------------------

    def pick_type(self, event):
        self._menu(event, [(t, t) for t in BAKE_TYPES], self.bake_type,
                   self.set_type)

    def set_type(self, label):
        self.bake_type = label
        self.rebuild()

    def pick_resolution(self, event):
        menu = QMenu()
        for size in RES_PRESETS:
            action = menu.addAction("%d × %d" % (size, size),
                                    lambda s=size: self.set_size(s, s))
            action.setCheckable(True)
            action.setChecked(self.width_px == size and self.height_px == size)
        menu.addSeparator()
        menu.addAction("Custom size…", self.type_size)
        menu.exec(event.screenPos())

    def set_size(self, width, height):
        self.width_px, self.height_px = int(width), int(height)
        self.rebuild()

    def type_size(self):
        """Width then height, so a non-square bake is still reachable — the
        Image-texture node had a row each and this keeps the capability."""
        width, ok = QInputDialog.getInt(_window(self), "Resolution",
                                        "Width (px):", self.width_px, 16, 8192)
        if not ok:
            return
        height, ok = QInputDialog.getInt(_window(self), "Resolution",
                                         "Height (px):", self.height_px,
                                         16, 8192)
        if ok:
            self.set_size(width, height)

    def pick_samples(self, event):
        menu = QMenu()
        auto = menu.addAction("Scene — Blender's own render samples",
                              lambda: self.set_samples(None))
        auto.setCheckable(True)
        auto.setChecked(self.samples is None)
        menu.addSeparator()
        for count in SAMPLE_PRESETS:
            action = menu.addAction(str(count),
                                    lambda c=count: self.set_samples(c))
            action.setCheckable(True)
            action.setChecked(self.samples == count)
        menu.addSeparator()
        menu.addAction("Type a number…", self.type_samples)
        menu.exec(event.screenPos())

    def set_samples(self, count):
        self.samples = None if count is None else max(1, int(count))
        self.rebuild()

    def type_samples(self):
        count, ok = QInputDialog.getInt(
            _window(self), "Samples", "Bake samples:",
            self.samples or 16, 1, MAX_SAMPLES)
        if ok:
            self.set_samples(count)

    def pick_view_from(self, event):
        self._menu(event, VIEW_FROM_LABELS, self.view_from,
                   self.set_view_from)

    def set_view_from(self, value):
        self.view_from = value
        self.rebuild()

    def pick_space(self, event):
        self._menu(event, SPACE_LABELS, self.normal_space, self.set_space)

    def set_space(self, value):
        self.normal_space = value
        self.rebuild()

    def pick_swizzle(self, event, axis):
        self._menu(event, SWIZZLE_LABELS, self.swizzle[axis],
                   lambda v: self.set_swizzle(axis, v))

    def set_swizzle(self, axis, value):
        self.swizzle[axis] = value
        self.rebuild()

    def toggle_pass(self, flag):
        self.passes[flag] = not self.passes[flag]
        self.rebuild()

    def pick_margin(self, event):
        value, ok = QInputDialog.getInt(_window(self), "Margin",
                                        "Margin (px):", self.margin, 0, 64)
        if ok:
            self.margin = value
            self.rebuild()

    def pick_margin_type(self, event):
        self._menu(event, MARGIN_TYPE_LABELS, self.margin_type,
                   self.set_margin_type)

    def set_margin_type(self, value):
        self.margin_type = value
        self.rebuild()

    def pick_target(self, event):
        self._menu(event, TARGET_LABELS, self.target, self.set_target)

    def set_target(self, value):
        self.target = value
        self.rebuild()

    def toggle_clear(self):
        self.use_clear = not self.use_clear
        self.rebuild()

    def toggle_s2a(self):
        self.selected_to_active = not self.selected_to_active
        self.rebuild()

    def toggle_cage(self):
        self.use_cage = not self.use_cage
        self.rebuild()

    def pick_cage_object(self, event):
        # a name field, like Blender's pointer without the eyedropper —
        # the add-on validates it at bake time (exists, is a mesh) and its
        # refusal names the problem
        name, ok = QInputDialog.getText(
            _window(self), "Cage Object",
            "Cage mesh name (empty for none):", text=self.cage_object)
        if ok:
            self.cage_object = str(name).strip()
            self.rebuild()

    def pick_cage_extrusion(self, event):
        value, ok = QInputDialog.getDouble(
            _window(self), "Cage Extrusion", "Extrusion (m):",
            self.cage_extrusion, 0.0, 100.0, 2)
        if ok:
            self.cage_extrusion = value
            self.rebuild()

    def pick_ray_distance(self, event):
        value, ok = QInputDialog.getDouble(
            _window(self), "Max Ray Distance",
            "Distance (m, 0 = no limit):",
            self.max_ray_distance, 0.0, 1000.0, 2)
        if ok:
            self.max_ray_distance = value
            self.rebuild()

    # --- remember node settings ------------------------------------------

    def settings_dict(self):
        return {"bake_type": self.bake_type, "samples": self.samples,
                "width": self.width_px, "height": self.height_px,
                "view_from": self.view_from,
                "normal_space": self.normal_space,
                "swizzle": list(self.swizzle), "margin": self.margin,
                "margin_type": self.margin_type,
                "target": self.target, "use_clear": self.use_clear,
                "selected_to_active": self.selected_to_active,
                "use_cage": self.use_cage,
                "cage_object": self.cage_object,
                "cage_extrusion": self.cage_extrusion,
                "max_ray_distance": self.max_ray_distance,
                "passes": dict(self.passes)}

    def apply_settings(self, d):
        if d.get("bake_type") in BAKE_TYPES:
            self.bake_type = d["bake_type"]
        for attr, key in (("width_px", "width"), ("height_px", "height")):
            try:
                setattr(self, attr, max(16, min(int(d.get(key,
                        getattr(self, attr))), 8192)))
            except (TypeError, ValueError):
                pass
        if d.get("samples") is None or isinstance(d.get("samples"), int):
            self.samples = d.get("samples")
        for attr, allowed in (("view_from", VIEW_FROM_LABELS),
                              ("normal_space", SPACE_LABELS),
                              ("margin_type", MARGIN_TYPE_LABELS),
                              ("target", TARGET_LABELS)):
            value = d.get(attr)
            if any(value == enum for _l, enum in allowed):
                setattr(self, attr, value)
        swz = d.get("swizzle")
        if isinstance(swz, list) and len(swz) == 3 and all(
                any(axis == enum for _l, enum in SWIZZLE_LABELS)
                for axis in swz):
            self.swizzle = list(swz)
        try:
            self.margin = max(0, int(d.get("margin", self.margin)))
        except (TypeError, ValueError):
            pass
        # ⚠ a 0.28.x settings dict has none of the panel keys and two dead
        # ones (view_transform / denoise): unknown keys are simply never
        # read, missing ones keep the fresh-panel defaults above
        for attr in ("use_clear", "selected_to_active", "use_cage"):
            if isinstance(d.get(attr), bool):
                setattr(self, attr, d[attr])
        if isinstance(d.get("cage_object"), str):
            self.cage_object = d["cage_object"].strip()
        for attr in ("cage_extrusion", "max_ray_distance"):
            try:
                setattr(self, attr, max(0.0, float(d.get(attr,
                        getattr(self, attr)))))
            except (TypeError, ValueError):
                pass
        passes = d.get("passes")
        if isinstance(passes, dict):
            for flag in self.passes:
                if flag in passes:
                    self.passes[flag] = bool(passes[flag])
        self.rebuild()

    # --- the button -------------------------------------------------------

    def _button_rect(self):
        return QRectF(10, self.h - BUTTON_H - ROW_GAP,
                      self.w - 20, BUTTON_H)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        rect = self._button_rect()
        busy = bool(self.tab and self.tab.bake_running())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(theme.PANEL2 if busy
                                       else theme.ACCENT)))
        painter.drawRoundedRect(rect, 4, 4)
        font = painter.font()
        font.setPixelSize(12)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(theme.TEXT_DIM if busy else "#14161a")))
        painter.drawText(rect, Qt.AlignCenter,
                         "Baking…" if busy else "Bake")

    def mousePressEvent(self, event):
        if (event.button() == Qt.LeftButton
                and self._button_rect().contains(event.pos())):
            event.accept()
            if self.tab:
                self.tab.run_bake(self)
            return
        super().mousePressEvent(event)


class OutputImageNode(FieldNode):
    """Where the baked file lands, and the preview of the last result.

    "(auto)" means `default_bake_dir()\\<material>_baked` since 2026-08-07 —
    the designated folder inside the toolset, not Blender's //bakes — and
    the file picker starts there too.

    **Replace shader** (Marty, 2026-08-07, default OFF) carries the run one
    step further: when the maps are on disk, each baked map is PLACED into
    the material it came from as an Image Texture node and wired to that
    material's active Material Output — his own words, *"just PLACE the
    node in the material > respective slots and attach it to material
    output (one of them if many)"* (`apply_baked_material`, add-on
    0.27.0). It is the only thing in this pipeline that writes to the
    user's scene, which is why it is a tickbox that starts unticked."""

    def __init__(self, tab):
        super().__init__("Output image", theme.TYPE_COLORS["playblast"],
                         inputs=[("Baked", COL_BAKED)], width=200)
        self.tab = tab
        self.out_path = None
        self.preview = None
        self.no_preview_text = "no bake yet"
        self.replace_shader = False
        self.replace_all_slots = False
        self.status_text = "—"
        self.help_text = (
            "Where the baked map lands, and a preview of the last one.\n\n"
            "Path: leave it automatic and maps go to <material>_baked in the "
            "toolset's own baked folder. Name a file .exr and the bake keeps "
            "values above 1.0 instead of clipping them flat.\n\n"
            "Replace shader: put each baked map back INTO the material it "
            "came from, wired to that material's Material Output. The old "
            "shader network stays, just unplugged.\n\n"
            "All slots: do that for every material slot of the object, not "
            "only the one that was baked.")
        self.set_extra_height(PREVIEW_H + ROW_GAP)
        self.setToolTip("Path: unset means %s\\<material>_baked — our own "
                        "folder, so a Blender extension update can never "
                        "sweep your maps.\n\nReplace shader: after the bake, "
                        "the new map is placed in each baked material as an "
                        "image node and wired to its Material Output. The "
                        "old network stays, just unplugged.\n\nAll slots: "
                        "place it into EVERY material slot of the baked "
                        "object, not only the material it came from."
                        % default_bake_dir())
        self.rebuild()

    def path_text(self):
        """What the Path row shows. ⚠ An unset path used to read "(auto)",
        which says a default exists without saying WHERE — Marty asked for
        a default path he can see (2026-08-08). The folder name is the
        answer that fits the row; the tooltip carries it in full."""
        if self.out_path:
            return os.path.basename(self.out_path)
        return "auto: %s\\" % os.path.basename(default_bake_dir())

    def rebuild(self):
        """Four rows: Path, Replace shader, All slots, Status. ⚠ "All slots"
        is dimmed (Blender's `layout.active = False`, the idiom this node
        set already uses) while Replace shader is off — it has nothing to
        act on until something is being placed, but it stays clickable so
        the pair can be armed in either order. `fields` / `dim_rows` are
        rebuilt with the rows, all three keyed by ROW INDEX."""
        rows, fields, dim = [], {}, set()

        def add(label, value, handler=None, dimmed=False):
            index = len(rows)
            rows.append((label, value))
            if handler is not None:
                fields[index] = handler
            if dimmed:
                dim.add(index)

        add("Path", self.path_text(), self.pick_path)
        add("Replace shader", self.replace_shader,
            lambda e: self.toggle_replace())
        add("All slots", self.replace_all_slots,
            lambda e: self.toggle_all_slots(), dimmed=not self.replace_shader)
        self.status_row = len(rows)
        add("Status", self.status_text)
        self.fields = fields
        self.dim_rows = dim
        self.set_rows(rows)

    def pick_path(self, event):
        menu = QMenu()
        menu.addAction("Choose file…", self.choose_path)
        if self.out_path:
            menu.addAction("Back to automatic",
                           lambda: self.set_path(None))
        menu.exec(event.screenPos())

    def choose_path(self):
        start = self.out_path or default_bake_dir()
        path, _filter = QFileDialog.getSaveFileName(
            _window(self), "Baked image", start,
            "Images (*.png *.exr)")
        if path:
            self.set_path(path)

    def set_path(self, path):
        self.out_path = path or None
        self.rebuild()

    def toggle_replace(self):
        """Place the baked maps into their own materials once the run
        finishes. Off by default — every other part of this pipeline leaves
        the scene exactly as it found it, and this one does not."""
        self.replace_shader = not self.replace_shader
        self.rebuild()

    def toggle_all_slots(self):
        """Marty, 2026-08-08: *"a tickbox that will automatically place and
        connect baked result to Active material output of EVERY material
        slot"*. Off, the map goes into the material it was baked from
        (0.27.0's rule); on, it goes into every slot of that object — a
        slot with its OWN baked map still keeps that one, since overwriting
        a correct map with a neighbour's would be a downgrade."""
        self.replace_all_slots = not self.replace_all_slots
        self.rebuild()

    def settings_dict(self):
        return {"out_path": self.out_path,
                "replace_shader": self.replace_shader,
                "replace_all_slots": self.replace_all_slots}

    def apply_settings(self, d):
        path = d.get("out_path")
        if path is None or isinstance(path, str):
            self.out_path = path or None
        self.replace_shader = bool(d.get("replace_shader", False))
        self.replace_all_slots = bool(d.get("replace_all_slots", False))
        self.rebuild()

    def show_result(self, path, note):
        self.out_path = self.out_path or None
        self.set_row(0, os.path.basename(path))
        self.status_text = note
        self.set_row(self.status_row, note)
        img = QImage(path)
        self.preview = None if img.isNull() else img
        # ⚠ Qt cannot decode EXR — after a perfectly good EXR bake the
        # preview would say "no bake yet", which reads as a FAILED bake to
        # someone judging by eye. Say what actually happened instead.
        self.no_preview_text = ("EXR saved — no preview"
                                if img.isNull()
                                and path.lower().endswith(".exr")
                                else "no bake yet")
        self.update()

    def _preview_rect(self):
        return QRectF(10, self.h - PREVIEW_H - ROW_GAP,
                      self.w - 20, PREVIEW_H)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        rect = self._preview_rect()
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.setBrush(QBrush(QColor("#14161a")))
        painter.drawRoundedRect(rect, 4, 4)
        if self.preview is not None:
            scaled = self.preview.scaled(
                int(rect.width()) - 4, int(rect.height()) - 4,
                Qt.KeepAspectRatio, Qt.SmoothTransformation)
            x = rect.x() + (rect.width() - scaled.width()) / 2
            y = rect.y() + (rect.height() - scaled.height()) / 2
            painter.drawImage(int(x), int(y), scaled)
        else:
            painter.setPen(QPen(QColor(theme.TEXT_DIM)))
            font = painter.font()
            font.setPixelSize(10)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignCenter, self.no_preview_text)


class BulkBakeNode(FieldNode):
    """Many meshes, every slot, one press (Marty, 2026-08-07).

    Two modes on the dropdown:
      Bulk bake   — "Selected to bake queue" grabs the viewport selection;
                    meshes with materials and UVs are queued, everything
                    else selected is ignored (the add-on counts it).
      Folder bake — the selection controls gray out and a collection picker
                    appears; whatever meshes the collection holds get baked.

    Targets come from `bake_targets` (add-on 0.26.0) at PRESS time — a
    queued run must not follow a selection the user changes afterwards
    (the optimizer learned that one the hard way).

    ⚠ It is a SOURCE, not a whole pipeline (Marty, 2026-08-07: neither
    this nor the Bake node may bake on its own). Its green output must
    reach a **Bake settings** node — which owns the type, the resolution
    and every option for a bulk run exactly as it does for a single one —
    and that must reach an **Output image** node. Pressing the button here
    is a shortcut for pressing Bake over there; unwired, it refuses in
    words."""

    def __init__(self, tab):
        super().__init__("Bulk bake", theme.TYPE_COLORS["anim"],
                         outputs=[("Material", COL_MATERIAL)],
                         width=BAKE_W, label_frac=0.42)
        self.tab = tab
        self.mode = "SELECTED"
        self.collection = None
        self.help_text = (
            "Many meshes, every slot, one press.\n\n"
            "Bulk bake: whatever is selected in the viewport right now. "
            "Folder bake: every mesh in a collection you pick.\n\n"
            "Targets are resolved when you press, not when the bake runs, so "
            "changing the selection afterwards cannot alter it. Meshes with "
            "no materials or no UVs are skipped and counted.\n\n"
            "Type, resolution and every option come from the Bake settings "
            "node it is wired to.")
        self.set_extra_height(BUTTON_H + ROW_GAP)
        self.rebuild()

    def rebuild(self):
        """Rows swap with the mode, the way the Bake-settings node swaps
        with the type — and `fields` / `dim_rows` are rebuilt with them
        (all three key by ROW INDEX)."""
        rows, fields, dim = [], {}, set()

        def add(label, value, handler=None, dimmed=False):
            index = len(rows)
            rows.append((label, value))
            if handler is not None:
                fields[index] = handler
            if dimmed:
                dim.add(index)

        add("Mode", _label_of(BULK_MODES, self.mode), self.pick_mode)
        if self.mode == "COLLECTION":
            add("Collection", self.collection or "(pick…)",
                self.pick_collection)
        else:
            # visible but grayed — Marty's "folder bake grays out the bulk
            # options", mirrored: in bulk mode the collection is the dim one
            add("Collection", self.collection or "—", dimmed=True)

        self.fields = fields
        self.dim_rows = dim
        self.set_rows(rows)

    def button_text(self):
        return ("Bake collection" if self.mode == "COLLECTION"
                else "Selected to bake queue")

    # --- pickers ----------------------------------------------------------

    def pick_mode(self, event):
        self._menu(event, BULK_MODES, self.mode, self.set_mode)

    def set_mode(self, value):
        self.mode = value
        self.rebuild()

    def pick_collection(self, event):
        menu = QMenu()
        rows = self.tab.collection_names() if self.tab else None
        if rows is None:
            menu.addAction("(Blender unreachable)").setEnabled(False)
        elif not rows:
            menu.addAction("(no collections in the scene)").setEnabled(False)
        else:
            for row in rows:
                label = "%s%s  (%d bakeable)" % (
                    "    " * int(row.get("depth", 0)), row["name"],
                    row.get("meshes", 0))
                action = menu.addAction(
                    label, lambda n=row["name"]: self.set_collection(n))
                if not row.get("meshes"):
                    # refuse BEFORE the run is spent, like the all-off
                    # contributions rule
                    action.setEnabled(False)
        menu.exec(event.screenPos())

    def set_collection(self, name):
        self.collection = name
        self.rebuild()

    # --- remember node settings ------------------------------------------

    def settings_dict(self):
        return {"mode": self.mode, "collection": self.collection}

    def apply_settings(self, d):
        if any(d.get("mode") == enum for _l, enum in BULK_MODES):
            self.mode = d["mode"]
        col = d.get("collection")
        if col is None or isinstance(col, str):
            self.collection = col or None
        self.rebuild()

    # --- the button -------------------------------------------------------

    def _button_rect(self):
        return QRectF(10, self.h - BUTTON_H - ROW_GAP,
                      self.w - 20, BUTTON_H)

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        rect = self._button_rect()
        busy = bool(self.tab and self.tab.bake_running())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(theme.PANEL2 if busy
                                       else theme.ACCENT)))
        painter.drawRoundedRect(rect, 4, 4)
        font = painter.font()
        font.setPixelSize(12)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(theme.TEXT_DIM if busy else "#14161a")))
        painter.drawText(rect, Qt.AlignCenter,
                         "Baking…" if busy else self.button_text())

    def mousePressEvent(self, event):
        if (event.button() == Qt.LeftButton
                and self._button_rect().contains(event.pos())):
            event.accept()
            if self.tab:
                self.tab.run_bulk_bake(self)
            return
        super().mousePressEvent(event)


class CollectionNode(FieldNode):
    """A whole collection as a bake source (Marty, 2026-08-08).

    *"if inputed in 'type' socket of bake settings node — our app will
    search every mesh in that collection and bake all materials and slots
    when baking, both Bake and 'Collection' nodes can be wired in bake
    settings in the same time."*

    So this is a SECOND green source that can sit alongside the Bake node
    rather than replacing it — which is why the Bake-settings input became
    a MULTI-INPUT socket in the same batch. Targets come from the add-on's
    `bake_targets mode=collection` (children included, non-meshes and
    meshes without materials or UVs skipped and counted) at PRESS time, the
    same rule the Bulk bake node follows.

    ⚠ It overlaps the Bulk bake node's "Folder bake" mode ON PURPOSE and is
    not a replacement: Bulk bake is one-or-the-other with a button of its
    own, this one composes. Both resolve through the same command, so they
    can never disagree about what a collection contains."""

    def __init__(self, tab):
        super().__init__("Collection", theme.TYPE_COLORS["vgroups"],
                         rows=[("Collection", "(pick…)")],
                         outputs=[("Material", COL_MATERIAL)],
                         width=BAKE_W, label_frac=0.42)
        self.tab = tab
        self.collection = None
        self.fields = {0: self.pick_collection}
        self.help_text = (
            "Every mesh in a collection, all materials and all slots, as one "
            "bake source. Children of the collection are included.\n\n"
            "It can share a Bake settings node WITH a Bake node — that "
            "input takes several wires — so \"this collection plus this "
            "one material\" is one press. A material named twice is baked "
            "once.")

    def pick_collection(self, event):
        menu = QMenu()
        rows = self.tab.collection_names() if self.tab else None
        if rows is None:
            menu.addAction("(Blender unreachable)").setEnabled(False)
        elif not rows:
            menu.addAction("(no collections in the scene)").setEnabled(False)
        else:
            for row in rows:
                label = "%s%s  (%d bakeable)" % (
                    "    " * int(row.get("depth", 0)), row["name"],
                    row.get("meshes", 0))
                action = menu.addAction(
                    label, lambda n=row["name"]: self.set_collection(n))
                if not row.get("meshes"):
                    action.setEnabled(False)
        menu.exec(event.screenPos())

    def set_collection(self, name):
        self.collection = name
        self.set_row(0, name)

    def settings_dict(self):
        return {"collection": self.collection}

    def apply_settings(self, d):
        col = d.get("collection")
        if col is None or isinstance(col, str):
            self.collection = col or None
        self.set_row(0, self.collection or "(pick…)")


class MapSetNode(FieldNode):
    """Several map types, one press (Marty's "add another node", 2026-08-08).

    Baking a PBR set means pressing Bake once per type and changing the
    Type row in between — the one repetitive thing left in this pipeline.
    This node ticks the types instead: wire it BETWEEN a bake source and
    the Bake settings node, and every ticked type is queued for every
    target the source found, each map auto-named
    `<material>_<type>_baked`.

    ⚠ It is a PASS-THROUGH, not a source. The green target list flows
    through it untouched — which keeps "the chain is the permission"
    exactly as it was: a source still has to reach Bake settings, and Bake
    settings still has to reach an Output image node. The one thing this
    node overrides is the settings node's own Type row, and the status
    line says so when it does.

    ⚠ **Resolution, margin and every other option still live on the Bake
    settings node** — one place, the same rule the Bulk bake node follows.
    A per-type resolution would be the natural next ask and is deliberately
    not here: it would make this node a second settings node."""

    def __init__(self, tab):
        super().__init__("Map set", theme.TYPE_COLORS["remap"],
                         inputs=[("Material", COL_MATERIAL)],
                         outputs=[("Material", COL_MATERIAL)],
                         width=BAKE_W, label_frac=0.62)
        self.tab = tab
        # A sane PBR set out of the box, in Blender's own type order.
        self.picked = {label: label in MAP_SET_DEFAULT for label in BAKE_TYPES}
        self.help_text = (
            "Bake several map types in one press. Tick the maps you want and "
            "every one is baked for every target, saved as "
            "<material>_<type>_baked.\n\n"
            "Wire it BETWEEN a source and the Bake settings node: the targets "
            "flow through it untouched and only the Type row is overridden. "
            "Resolution, margin and the rest still come from Bake settings.")
        self.rebuild()

    def rebuild(self):
        rows, fields = [], {}

        def add(label, value, handler=None):
            index = len(rows)
            rows.append((label, value))
            if handler is not None:
                fields[index] = handler

        add("Maps", None)                      # a dim heading, like Influence
        for label in BAKE_TYPES:
            add(label, self.picked[label],
                lambda e, t=label: self.toggle(t))
        self.fields = fields
        self.dim_rows = set()
        self.set_rows(rows)

    def toggle(self, label):
        self.picked[label] = not self.picked[label]
        self.rebuild()

    def types(self):
        """The ticked types as cycles enums, in Blender's type order — the
        order the queue bakes them in, and therefore the order their files
        appear in the folder."""
        return [BAKE_ENUM[label] for label in BAKE_TYPES
                if self.picked[label]]

    def settings_dict(self):
        return {"picked": dict(self.picked)}

    def apply_settings(self, d):
        picked = d.get("picked")
        if isinstance(picked, dict):
            for label in self.picked:
                if label in picked:
                    self.picked[label] = bool(picked[label])
        self.rebuild()


# What add_bake_node constructs for each menu entry. One table, used by the
# toolbar menu AND Shift+A, so the two can never offer different nodes.
NODE_MAKERS = {"Bake": BakeTargetNode, "Bulk bake": BulkBakeNode,
               "Collection": CollectionNode, "Map set": MapSetNode,
               "Bake settings": BakeSettingsNode,
               "Output image": OutputImageNode}


# ------------------------------------------------------------- graph walk


def upstream_node(scene, node, input_index, want_type):
    """The node feeding `input_index`, walked through reroute dots. None if
    the wire chain does not end at a `want_type`."""
    seen = set()
    item, index = node, input_index
    while True:
        wires = scene.wires_into(item, index)
        if not wires:
            return None
        src = wires[0].src[0]
        if isinstance(src, want_type):
            return src
        if isinstance(src, RerouteItem) and id(src) not in seen:
            seen.add(id(src))
            item, index = src, 0
            continue
        return None


SOURCE_TYPES = (BakeTargetNode, BulkBakeNode, CollectionNode)


def upstream_sources(scene, settings_node):
    """EVERY source feeding a Bake-settings node, plus the Map set crossed
    on the way — `(sources, map_set)`.

    ⚠ **The input is a MULTI-INPUT socket since 2026-08-08** (Marty: *"both
    Bake and 'Collection' nodes can be wired in bake settings in the same
    time"*), so this returns a LIST where it used to return one node. Every
    wire into the socket is walked, straight through reroute dots and
    **Map set** nodes — a Map set is not a source, it only multiplies the
    types whatever IS a source gets baked at.

    `sources` is empty when nothing bakeable reaches the socket; `map_set`
    is the first Map set crossed (chaining two is legal and the nearest to
    the settings node wins)."""
    sources, map_set, seen = [], None, set()
    frontier = [(settings_node, 0)]
    while frontier:
        item, index = frontier.pop(0)
        for wire in scene.wires_into(item, index):
            src = wire.src[0]
            if id(src) in seen:
                continue
            seen.add(id(src))
            if isinstance(src, SOURCE_TYPES):
                sources.append(src)
            elif isinstance(src, (RerouteItem, MapSetNode)):
                if isinstance(src, MapSetNode) and map_set is None:
                    map_set = src
                frontier.append((src, 0))
    return sources, map_set


def upstream_source(scene, settings_node):
    """The FIRST source feeding a Bake-settings node — `(source, map_set)`.

    Kept for callers that only need to know *whether* one node feeds the
    chain; `upstream_sources` is the one the bake itself uses now that a
    settings node can be fed by several at once."""
    sources, map_set = upstream_sources(scene, settings_node)
    return (sources[0] if sources else None), map_set


def downstream_node(scene, node, want_type):
    """The first `want_type` fed by any of `node`'s outputs, through
    reroutes."""
    seen = set()
    frontier = [node]
    while frontier:
        item = frontier.pop(0)
        if id(item) in seen:
            continue
        seen.add(id(item))
        for wire in scene.wires:
            if wire.src[0] is item:
                dst = wire.dst[0]
                if isinstance(dst, want_type):
                    return dst
                if isinstance(dst, RerouteItem):
                    frontier.append(dst)
    return None


def build_bake_graph(scene, tab):
    """The default three nodes, pre-wired in Marty's layout. ⚠ The dict keys
    predate the 2026-08-07 renames and are internal handles: "shader" is
    the node titled Bake, "bake" the one titled Bake settings."""
    n_shader = BakeTargetNode(tab)
    n_shader.setPos(-420, -110)
    n_bake = BakeSettingsNode(tab)
    n_bake.setPos(-120, -70)
    n_out = OutputImageNode(tab)
    n_out.setPos(180, -90)
    for item in (n_shader, n_bake, n_out):
        scene.addItem(item)
    scene.add_wire((n_shader, "out", 0), (n_bake, "in", 0), COL_MATERIAL)
    scene.add_wire((n_bake, "out", 0), (n_out, "in", 0), COL_BAKED)
    return {"shader": n_shader, "bake": n_bake, "out": n_out}
