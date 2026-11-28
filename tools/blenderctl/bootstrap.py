"""Gate child creation until the supervisor owns this process tree."""
from pathlib import Path
import os
import subprocess
import sys
import time

job = Path(sys.argv[1])
deadline = time.monotonic() + 10
while not (job / "worker.gate").exists():
    if time.monotonic() > deadline:
        raise SystemExit(23)
    time.sleep(.02)
raise SystemExit(subprocess.call(sys.argv[2:], creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
