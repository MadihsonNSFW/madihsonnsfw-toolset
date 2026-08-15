"""Crisp vector toolbar icons, drawn from inline SVG (no external asset files).

Feather-style stroke icons rendered to a QIcon at call time so we can tint them
(e.g. green Start, red Stop) to match the UI.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

DEFAULT = "#c9ced6"

# viewBox is 0 0 24 24 for every path body below.
_BODIES = {
    "add":        '<rect x="3" y="3" width="18" height="18" rx="2"/>'
                  '<line x1="12" y1="8" x2="12" y2="16"/>'
                  '<line x1="8" y1="12" x2="16" y2="12"/>',
    "remove":     '<rect x="3" y="3" width="18" height="18" rx="2"/>'
                  '<line x1="8" y1="12" x2="16" y2="12"/>',
    "clear":      '<polyline points="3 6 5 6 21 6"/>'
                  '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>'
                  '<line x1="10" y1="11" x2="10" y2="17"/>'
                  '<line x1="14" y1="11" x2="14" y2="17"/>',
    "open":       '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 '
                  '2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
    "output":     '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 '
                  '2-2h6"/><polyline points="15 3 21 3 21 9"/>'
                  '<line x1="10" y1="14" x2="21" y2="3"/>',
    "log":        '<polyline points="4 17 10 11 4 5"/>'
                  '<line x1="12" y1="19" x2="20" y2="19"/>',
    "start":      '<polygon points="6 4 20 12 6 20 6 4"/>',
    "pause":      '<rect x="6" y="5" width="4" height="14" rx="1"/>'
                  '<rect x="14" y="5" width="4" height="14" rx="1"/>',
    "stop":       '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    "console":    '<rect x="3" y="4" width="18" height="16" rx="2"/>'
                  '<polyline points="7 9 10 12 7 15"/>'
                  '<line x1="12" y1="15" x2="16" y2="15"/>',
    "settings":   '<circle cx="12" cy="12" r="3"/>'
                  '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1'
                  '-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0'
                  '-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 '
                  '1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 '
                  '1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h'
                  '.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 '
                  '2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 '
                  '0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 '
                  '1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 '
                  '1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 '
                  '4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    "watch":      '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>'
                  '<circle cx="12" cy="12" r="3"/>',
    "scheduler":  '<circle cx="12" cy="12" r="9"/>'
                  '<polyline points="12 7 12 12 15 14"/>',
    "power":      '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/>'
                  '<line x1="12" y1="2" x2="12" y2="12"/>',
    # Steaming coffee mug — the universal "keep it awake" idiom.
    "awake":      '<path d="M2 8h16v9a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4z"/>'
                  '<path d="M18 9h1a3 3 0 0 1 0 7h-1"/>'
                  '<line x1="6" y1="1" x2="6" y2="4"/>'
                  '<line x1="10" y1="1" x2="10" y2="4"/>'
                  '<line x1="14" y1="1" x2="14" y2="4"/>',
    "folder":     '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 '
                  '2-2h5l2 3h9a2 2 0 0 1 2 2z"/>'
                  '<polyline points="8 13 12 17 16 13"/>'
                  '<line x1="12" y1="10" x2="12" y2="17"/>',
    # Film clapper-ish frame with a play triangle — the solid-mode preview video.
    "preview":    '<rect x="2" y="5" width="20" height="14" rx="2"/>'
                  '<line x1="7" y1="5" x2="7" y2="19"/>'
                  '<line x1="17" y1="5" x2="17" y2="19"/>'
                  '<polygon points="10.5 9 15 12 10.5 15"/>',
    # Monitor with a play triangle — open the rendered animation in the player.
    "play":       '<rect x="2" y="3" width="20" height="14" rx="2"/>'
                  '<polygon points="10 7 15 10 10 13"/>'
                  '<line x1="8" y1="21" x2="16" y2="21"/>'
                  '<line x1="12" y1="17" x2="12" y2="21"/>',
    # Play with a small arrow tail — resuming a paused job.
    "resume":     '<polygon points="7 4 20 12 7 20 7 4"/>'
                  '<line x1="3" y1="5" x2="3" y2="19"/>',
}


def icon(name: str, color: str = DEFAULT, size: int = 30) -> QIcon:
    body = _BODIES[name]
    # Play-triangle icons read better filled; the rest are outlines.
    fill = color if name in ("start", "resume") else "none"
    doc = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
           f'fill="{fill}" stroke="{color}" stroke-width="2" '
           f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    renderer = QSvgRenderer(QByteArray(doc.encode("utf-8")))
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    renderer.render(p, QRectF(0, 0, size, size))
    p.end()
    return QIcon(pm)
