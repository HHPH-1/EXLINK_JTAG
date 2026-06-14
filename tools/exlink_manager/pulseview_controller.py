from __future__ import annotations

from pathlib import Path
import subprocess

from .settings import default_pulseview_candidates


class PulseViewError(RuntimeError):
    pass


def find_pulseview(configured_path: str = "") -> Path | None:
    if configured_path:
        path = Path(configured_path)
        if path.exists():
            return path
    for candidate in default_pulseview_candidates():
        if candidate.exists():
            return candidate
    return None


def validate_pulseview_path(path: str) -> Path:
    pulseview = Path(path)
    if not pulseview.exists():
        raise PulseViewError(f"PulseView path does not exist: {path}")
    if pulseview.is_dir():
        pulseview = pulseview / "pulseview.exe"
    if pulseview.name.lower() != "pulseview.exe":
        raise PulseViewError("Select pulseview.exe")
    if not pulseview.exists():
        raise PulseViewError(f"pulseview.exe was not found: {pulseview}")
    return pulseview


def launch_pulseview(path: str) -> subprocess.Popen:
    pulseview = validate_pulseview_path(path)
    return subprocess.Popen([str(pulseview)], close_fds=True)
