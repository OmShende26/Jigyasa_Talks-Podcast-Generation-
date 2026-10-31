"""Start the real app with fake model and speech services.

Env: DATA_DIR (required: a scratch folder), PORT (default 8765), MOCK_DELAY (seconds per fake model call).
"""
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
assert os.environ.get("DATA_DIR"), "set DATA_DIR to a scratch folder so tests never touch your real data"

import mocks  # noqa: E402

mocks.install()

import uvicorn  # noqa: E402
from app import main  # noqa: E402

uvicorn.run(main.app, host="127.0.0.1", port=int(os.environ.get("PORT", "8765")), log_level="warning")
