from __future__ import annotations

from pathlib import Path
import sys
import traceback

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.exlink_manager.app import run
from tools.exlink_manager.logging_setup import setup_logging
from tools.exlink_manager.settings import app_data_dir


def main() -> int:
    setup_logging()
    try:
        return run()
    except Exception:
        crash_dir = app_data_dir() / "logs"
        crash_dir.mkdir(parents=True, exist_ok=True)
        (crash_dir / "last_crash.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
