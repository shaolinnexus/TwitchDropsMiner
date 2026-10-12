import sys
from pathlib import Path
from unittest.mock import MagicMock

# make the application modules importable, and their paths resolve to the repository
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.argv[0] = str(ROOT / "main.py")

# pystray needs a display to import on Linux, and isn't needed for these tests
try:
    import pystray  # noqa: F401
except Exception:
    sys.modules["pystray"] = MagicMock()
