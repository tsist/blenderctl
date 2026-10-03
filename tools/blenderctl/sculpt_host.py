# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-side acceptance of fixed native replay outputs and reopen evidence."""
from pathlib import Path
from protocol import Failure,read_json
from filesystem import FileGuard
from review_contract import key

OUTPUTS=('sculpt-candidate.blend','sculpt-report.json','sculpt-verification.json','sculpt-resolved-stroke.json')

def runtime_files(blender):
    p=Path(blender)
    return [p,p.parent/'5.2/datafiles/assets/brushes/essentials_brushes-mesh_sculpt.blend']

def accept(job,request,data,stack,checkpoint,runtime_hashes):
    hashes={}
    for name in OUTPUTS:
        p=job/name
        g=stack.enter_context(FileGuard(p));hashes[name]=g.sha256(checkpoint)
    report=read_json(job/'sculpt-report.json');proof=read_json(job/'sculpt-verification.json')
    if key(report)!=key(data):raise Failure('CONFLICT','Worker result differs from sculpt report')
    if report['request_sha256']!=key(request) or proof['request_sha256']!=key(request):
        raise Failure('CONFLICT','Replay request binding differs')
    for field,name in [('candidate','sculpt-candidate.blend'),('verification','sculpt-verification.json')]:
        if report[field]!={'file':str(job/name),'expected_sha256':hashes[name]}:raise Failure('CONFLICT','Replay output descriptor differs')
    if proof['candidate_sha256']!=hashes['sculpt-candidate.blend'] or proof['source_sha256']!=request['params']['manifest']['source']['expected_sha256']:
        raise Failure('CONFLICT','Independent replay evidence SHA differs')
    if proof['before_sha256']!=key(read_json(job/'sculpt-before.json')):raise Failure('CONFLICT','Reopen baseline differs')
    for field in ('changed_vertices','max_displacement_local','protected_fields_unchanged'):
        if report[field]!=proof[field]:raise Failure('CONFLICT','Replay metrics differ from independent evidence')
    if proof['verifier_pid']==read_json(job/'sculpt-progress.json')['pid']:raise Failure('VALIDATION_FAILED','Verifier was not independent')
    if report['runtime_hashes']!=runtime_hashes:raise Failure('CONFLICT','Native runtime/brush binding differs')
    checkpoint()
    return report
