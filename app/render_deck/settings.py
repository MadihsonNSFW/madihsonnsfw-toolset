"""Persistent app settings, stored as JSON under the user's home dir."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

from .util import data_dir


def _config_path() -> str:
    return os.path.join(data_dir(), "settings.json")

# Engine ids Blender accepts on the CLI via -E. "" means "leave as saved".
# NOTE: EEVEE's id is BLENDER_EEVEE on Blender 5.0+ (was BLENDER_EEVEE_NEXT on
# 4.2-4.5). This tool targets 5.1.2 / 5.2.0, so BLENDER_EEVEE is correct.
ENGINES = {
    "Use file's engine": "",
    "Cycles": "CYCLES",
    "EEVEE": "BLENDER_EEVEE",
    "Workbench": "BLENDER_WORKBENCH",
}


@dataclass
class Settings:
    # All configured Blender executables (the user can add several versions).
    blender_paths: list = field(default_factory=list)
    # The one currently selected to render with (must be one of blender_paths).
    blender_path: str = ""
    output_dir: str = ""          # "" => let the .blend decide its own output
    default_engine: str = ""      # applied to newly added jobs
    extra_args: str = ""          # raw args appended to every Blender call
    verbose_log: bool = True
    auto_resume: bool = True      # after a crash, restart at the frame it died on
    toolbar_scale: float = 1.0    # size multiplier for the top icon toolbar
    # keep_awake — block automatic sleep while the app is open. Unlike
    # shutdown_when_done this IS persisted: it can't damage anything, the lock
    # only lasts as long as the app is running, and the toolbar button shows at
    # a glance that it's on.
    keep_awake: bool = False

    # Runtime-only (NOT dataclass fields, so asdict()/save() never serialise
    # them, and load() never restores them):
    #
    # first_run — True when no settings.json existed at load, i.e. the very
    # first launch; used to prompt for Blender setup once.
    first_run = False
    # shutdown_when_done — force-shut down the PC when the queue finishes.
    # Deliberately NOT persisted: it's a per-session intent ("I'm going to bed,
    # kill the machine after this queue"), and silently carrying it into the
    # next launch is how you lose a PC mid-work. Always starts off.
    shutdown_when_done = False

    @classmethod
    def load(cls) -> "Settings":
        existed = os.path.exists(_config_path())
        try:
            with open(_config_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            known = {k: v for k, v in data.items() if k in cls.__annotations__}
            obj = cls(**known)
        except (FileNotFoundError, json.JSONDecodeError, TypeError, OSError):
            obj = cls()
        obj.first_run = not existed
        # Migrate older single-path configs into the versions list.
        if obj.blender_path and obj.blender_path not in obj.blender_paths:
            obj.blender_paths.insert(0, obj.blender_path)
        # Keep the active selection valid.
        if obj.blender_path not in obj.blender_paths:
            obj.blender_path = obj.blender_paths[0] if obj.blender_paths else ""
        return obj

    def save(self) -> None:
        try:
            with open(_config_path(), "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, indent=2)
        except OSError:
            pass
