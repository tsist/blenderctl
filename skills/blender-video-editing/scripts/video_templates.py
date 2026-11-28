"""Installed video template library entry; compiler does not launch Blender."""
import hashlib
import runpy
import sys
import uuid
from pathlib import Path

ROOT = Path(r'<PROJECT_ROOT>\runtime\video-editing\template-library\0.1.0')
PIN = {'template_library.py': '5ba0031ba2db1dc27ce0c4996a925a49d94f38885a312c0ef60dfa4d609a550c',
       'cli.py': 'c3795affa3c91e74430c902f3a22d401d34ce901487196e5b60e276305b78127',
       'catalog.json': '9f41f0e04de4404d16c8c10d6bef8da3d59f514a82316d5d0c7252629681862f'}
for name, expected in PIN.items():
    if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
        raise RuntimeError('Installed template library identity changed: ' + name)
if 'template_library' in sys.modules:
    raise RuntimeError('Template module was preloaded')
prefix = ROOT / ('_absent_helper_pycache_' + uuid.uuid4().hex)
if prefix.exists():
    raise RuntimeError('Fresh bytecode namespace required')
sys.dont_write_bytecode = True
sys.pycache_prefix = str(prefix)
sys.path.insert(0, str(ROOT))
runpy.run_path(str(ROOT / 'cli.py'), run_name='__main__')
