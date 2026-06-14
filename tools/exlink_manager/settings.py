from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "ExlinkManager"
    return Path.home() / ".exlink_manager"


def default_pulseview_candidates() -> list[Path]:
    return [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "sigrok" / "PulseView" / "pulseview.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PulseView" / "pulseview.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "sigrok" / "PulseView" / "pulseview.exe",
        Path("pulseview.exe"),
    ]


@dataclass
class AppSettings:
    recent_serial_number: str = ""
    manual_com_port: str = ""
    xvc_host: str = "127.0.0.1"
    xvc_port: int = 2542
    tck_khz: int = 12500
    engine: str = "pio_safe"
    dma_chunk_bits: int = 8192
    max_shift_bits: int = 131072
    pulseview_path: str = ""
    log_level: str = "INFO"
    window_geometry_hex: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> "AppSettings":
        settings_path = path or app_data_dir() / "settings.json"
        if not settings_path.exists():
            settings = cls()
            for candidate in default_pulseview_candidates():
                if candidate.exists():
                    settings.pulseview_path = str(candidate)
                    break
            return settings
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def save(self, path: Path | None = None) -> Path:
        settings_path = path or app_data_dir() / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return settings_path
