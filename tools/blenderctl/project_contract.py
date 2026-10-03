# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit disk-only project admission and candidate-copy policy."""
from pathlib import Path
from scene_contract import obj,array,enum,validate
from model_contract import SHA
from protocol import Failure
PATH={'type':'string','minLength':1}
RESOURCE=obj({'file':PATH,'expected_sha256':SHA})
RESOURCES=array(RESOURCE,0,1000)
MANIFEST=obj({'version_policy':enum('SAME_MINOR','UPGRADE'),'path_policy':enum('REMAP_RELATIVE','MAKE_RELATIVE'),'unused_ids':{'const':'REJECT_LOSS'},'compress':{'type':'boolean'}})
VERIFY=obj({'file':PATH,'expected_sha256':SHA,'resources':RESOURCES})
COPY=obj({**VERIFY['properties'],'manifest':MANIFEST})
def normalize(params,command):
    validate(params,VERIFY if command=='project.verify' else COPY)
    out=dict(params);out['resources']=[dict(x) for x in params['resources']]
    for d in [out,*out['resources']]:
        p=Path(d['file'])
        if not p.is_absolute():raise Failure('INVALID_REQUEST','Project inputs require absolute paths')
        d['file']=str(p.resolve())
    if Path(out['file']).suffix.lower()!='.blend':raise Failure('INVALID_REQUEST','Project input must be .blend')
    paths=[d['file'].casefold() for d in [out,*out['resources']]]
    if len(set(paths))!=len(paths):raise Failure('INVALID_REQUEST','Duplicate project source/resource path')
    return out
