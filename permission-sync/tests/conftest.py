import sys
from pathlib import Path

# Ensure `src` is importable when pytest is invoked from the permission-sync dir.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
