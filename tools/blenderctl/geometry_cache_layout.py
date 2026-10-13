"""Pure planning for compact private native cache reads; source bytes stay put."""
import copy
import os
from pathlib import Path
from protocol import Failure


def _under(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise Failure('CONFLICT', 'Cache member must be relative')
    path=(root/relative).resolve()
    if not path.is_relative_to(root):
        raise Failure('CONFLICT', 'Cache member escapes its root')
    return path


def plan_layout(manifest, destination, *, windows=None):
    """Map complete bake directories to short names; keep member relationships.

    The returned manifest describes copies only. It never edits an old receipt,
    changes identity, creates directories, or permits reuse of another cache.
    """
    destination=Path(destination).resolve();root=Path(manifest['root']).resolve()
    windows=os.name=='nt' if windows is None else windows
    snapshot=copy.deepcopy(manifest);snapshot['root']=str(destination)
    expected={row['path']:row for row in manifest['files']}
    if len(expected)!=len(manifest['files']):
        raise Failure('CONFLICT', 'Cache file list contains duplicates')
    copies=[];covered=set();globals_by_target={}
    for index,bake in enumerate(snapshot['bakes']):
        original=manifest['bakes'][index]['directory']
        _under(root,original)
        directory='b'+format(index,'x')
        bake['directory']=directory
        target=(bake['object'],bake['modifier'])
        global_directory=globals_by_target.setdefault(target,'g'+format(len(globals_by_target),'x'))
        bake['metadata']['global']['bake_directory']=str(destination/global_directory)
        bake['metadata']['item']['directory']=str(destination/directory)
        bake['metadata']['item']['use_custom_path']=True
        for member in bake['files']:
            source_relative=(Path(original)/member['path']).as_posix()
            source=_under(root,source_relative)
            relative=(Path(directory)/member['path']).as_posix()
            output=_under(destination,relative)
            if not output.is_relative_to(destination/directory):
                raise Failure('CONFLICT','Bake member escapes its private directory')
            if windows and len(str(output))>=260:
                raise Failure('UNSUPPORTED','Private cache path exceeds native Windows reader budget; use a shorter jobs-dir before submitting: '+str(output))
            if source_relative in covered or expected.get(source_relative)!={'path':source_relative,'size':member['size'],'sha256':member['sha256']}:
                raise Failure('CONFLICT','Cache members do not exactly cover the receipt')
            covered.add(source_relative)
            copies.append({'source':str(source),'path':relative,'size':member['size'],'sha256':member['sha256']})
    if covered!=set(expected):
        raise Failure('CONFLICT','Private cache map omits receipt members')
    snapshot['files']=sorted([{k:row[k] for k in ('path','size','sha256')} for row in copies],key=lambda row:row['path'])
    return snapshot,copies
