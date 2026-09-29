"""Load the plugin package without running an AstrBot server."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
runtime = tempfile.TemporaryDirectory(
    prefix="catlottery-tests-", ignore_cleanup_errors=True
)
os.environ["ASTRBOT_ROOT"] = runtime.name
