"""Test env for one service, set BEFORE app/helpers import their config.

Offline by construction: a temp database, a known token, and no sockets — the
suite runs the ASGI app in-process. Instantiated copies inherit this unchanged.
"""

import os
import sys
import tempfile
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

_TMP = tempfile.mkdtemp(prefix="svc-test-")
os.environ["DB_PATH"] = str(Path(_TMP) / "test.db")
os.environ["SERVICE_TOKEN"] = "test-service-token"
