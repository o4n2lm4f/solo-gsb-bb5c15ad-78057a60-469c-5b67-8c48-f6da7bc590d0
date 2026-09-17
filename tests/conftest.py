"""Shared pytest configuration.

The application opens its SQLite job store at import time using
``RESOLVER_DB_PATH``.  Point it at a throw-away file *before* ``app.main``
is imported so tests never touch a real ``data/`` directory.
"""

import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="resolver-tests-")
os.environ.setdefault("RESOLVER_DB_PATH", os.path.join(_TMPDIR, "jobs.db"))
os.environ.setdefault("JOB_WORKERS", "1")
os.environ.setdefault("JOB_POLL_INTERVAL", "0.02")
