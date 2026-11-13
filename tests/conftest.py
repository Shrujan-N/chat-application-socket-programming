"""Pytest configuration ensuring App/ is importable from tests/."""
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "App"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
