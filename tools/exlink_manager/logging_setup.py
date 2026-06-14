from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path

from .settings import app_data_dir


def log_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging(level: str = "INFO") -> Path:
    directory = log_dir()
    log_path = directory / f"ExlinkManager-{datetime.now():%Y%m%d}.log"
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )
    logging.getLogger("PySide6").setLevel(logging.WARNING)
    return log_path
