# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal sandbox entry. Everything written by this process is untrusted output."""
import hashlib,json,sys,traceback
from pathlib import Path

def main():
    request=json.loads(Path(sys.argv[sys.argv.index('--')+1]).read_text(encoding='utf-8'))
    output=Path(request['scratch'])
    try:
        script=Path(request['script']);raw=script.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=request['script_sha256']:raise ValueError('Staged script hash mismatch')
        namespace={'__name__':'__main__','__file__':str(script),'job_directory':str(output),
                   'input_files':request['input_files'],'parameters':request['params'],'result':None}
        exec(compile(raw,str(script),'exec'),namespace,namespace)
        (output/'script-return.json').write_text(json.dumps({'result':namespace.get('result')},ensure_ascii=False,allow_nan=False),encoding='utf-8')
    except BaseException:
        traceback.print_exc();raise

if __name__=='__main__':main()
