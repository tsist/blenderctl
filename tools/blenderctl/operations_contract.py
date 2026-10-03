# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-only resource, cache planning and deployment contracts."""
from pathlib import Path
from scene_contract import obj,array,integer,validate
from protocol import Failure
PATH={'type':'string','minLength':1}
ID={'type':'string','pattern':'^[0-9a-f]{32}$'}
BUDGET=obj({'cpu_threads':integer(1,32),'memory_mb':integer(256,65536),'gpu_mb':integer(0,32768)})
CACHE=obj({'jobs_root':PATH,'pins':array(ID,0,1000),'references':array(ID,0,1000),'keep_days':integer(0,36500)})
PACKAGE=obj({'include_runtime':{'type':'boolean'},'max_bytes':integer(1,21474836480)})
CONTRACTS={'resource.init':obj({'domain':PATH,'manifest':BUDGET}),'resource.status':obj({'domain':PATH}),
 'cache.inspect':obj({'manifest':CACHE}),'cache.plan_clean':obj({'manifest':CACHE}),
 'project.package':obj({'manifest':PACKAGE}),'doctor.verify_install':obj({'directory':PATH})}
def normalize(params,command):
    validate(params,CONTRACTS[command])
    for p in ([params['domain']] if 'domain' in params else [params['directory']] if 'directory' in params else [params['manifest']['jobs_root']] if command.startswith('cache.') else []):
        if not Path(p).is_absolute():raise Failure('INVALID_REQUEST','Host operation paths must be absolute')
    return params
