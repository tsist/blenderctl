# SPDX-License-Identifier: GPL-3.0-or-later
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
kwargs={}
if os.name=='nt' and any(Path(arg).name=='sculpt_worker.py' for arg in sys.argv[2:]):
    si=subprocess.STARTUPINFO();si.dwFlags|=subprocess.STARTF_USESHOWWINDOW;si.wShowWindow=0
    kwargs['startupinfo']=si
raise SystemExit(subprocess.call(sys.argv[2:], creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,**kwargs))
