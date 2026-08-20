"""Physics tab — the shell that hosts the rig-physics tools.

The page is the shared LAYOUT A shell from rendering.py (grouped rail on the
left, the selected tool's settings on the right); only the empty-state text
differs. Bone Jiggle (jiggle.py) is the tab's tool.

The Proxy Cage builder lived here until 2026-08-14, when Marty had it removed
outright — this page's cage tool, the `cage_*` bridge commands and the
add-on's cage module all went, deliberately with no What's New entry. The
design is preserved in `docs` (physics-cage tombstone) if it is ever wanted
again; `MADI_Cage_*` objects in old scenes are ordinary Blender objects and
simply no longer have a manager.
"""

from rendering import RenderingPage


class PhysicsPage(RenderingPage):
    """Top-level 'Physics' tab — same shell, different contents."""

    EMPTY_TEXT = (
        "No physics tools yet.\n\n"
        "This tab is the home for rig physics tooling —\n"
        "each tool gets a rail entry on the left and its settings here.")

    # ⚠ `set_capture_busy` USED TO BE OVERRIDDEN HERE, and this tab was the
    # only rail whose tools really greyed out while Blender rendered — the
    # base shell's version was a docstring and nothing else, so Anim Layers,
    # Rendering and Node Setup silently did not. The base forwards since
    # 2026-08-19, so this override was an identical second copy: deleted, so
    # the two cannot drift. `app_rigprops_test.py` holds the base's behaviour
    # and `app_jiggle_test.py` still holds this tab's.
