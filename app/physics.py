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

    def set_capture_busy(self, busy):
        """Forward to the tools. The base shell has nothing to disable, but a
        tool that queues work on Blender's main thread must grey out exactly
        like a capture."""
        for _title, _group, widget in self._tools:
            if hasattr(widget, "set_capture_busy"):
                widget.set_capture_busy(busy)
