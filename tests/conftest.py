from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point the SQLite store at a per-test temp file so tests never touch
    the real data/uph.db, and reset the module's connection/init state."""
    from app.config import settings
    from app.services import db

    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path / "data"))
    # Reset thread-local connection and the one-time-init flag.
    if hasattr(db._local, "conn"):
        try:
            db._local.conn.close()
        except Exception:
            pass
        del db._local.conn
    db._initialized = False
    yield
    if hasattr(db._local, "conn"):
        try:
            db._local.conn.close()
        except Exception:
            pass
        del db._local.conn
    db._initialized = False
