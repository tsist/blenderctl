"""Project color management profile with declared OCIO dependencies."""
import hashlib
import os
from pathlib import Path
import re


def verify_files(rows):
    result={}
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'file','sha256'}:
            raise ValueError('Each OCIO dependency must declare file and sha256')
        path=Path(row['file'])
        if not path.is_absolute() or not re.fullmatch(r'[0-9a-f]{64}',row['sha256']):
            raise ValueError('OCIO input requires its absolute path and SHA-256')
        path=path.resolve(strict=True)
        with path.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=row['sha256']:
                raise ValueError(f'OCIO input SHA-256 changed: {path}')
        if path in result:
            raise ValueError(f'Duplicate OCIO input: {path}')
        result[path]=row['sha256']
    return result


def bind_profile(profile):
    import PyOpenColorIO as ocio

    if not isinstance(profile,dict) or set(profile)!={'config','dependencies','display','view','output_encoding'}:
        raise ValueError('Declare config, dependencies, display, view and output_encoding')
    if profile['output_encoding']!='sRGB':
        raise ValueError('This C5 PNG delivery expects an sRGB display encoding')
    config=Path(profile['config']['file']).resolve(strict=True)
    files=verify_files([profile['config'],*profile['dependencies']])
    assigned=os.environ.get('OCIO')
    if not assigned or Path(assigned).resolve()!=config:
        raise ValueError('Start the Blender worker with OCIO set to the declared project config')
    cfg=ocio.Config.CreateFromFile(str(config))
    cfg.validate()
    working=cfg.getColorSpace(ocio.ROLE_SCENE_LINEAR)
    if working is None or not working.getInteropID():
        raise ValueError('Project scene_linear role requires an interop ID')
    display,view=profile['display'],profile['view']
    if display not in cfg.getDisplays() or view not in cfg.getViews(display):
        raise ValueError('The desired display/view is absent from the project config')
    display_space=cfg.getDisplayViewColorSpaceName(display,view)
    destination=cfg.getColorSpace(display if display_space=='<USE_DISPLAY_NAME>' else display_space)
    if destination is None or destination.getInteropID()!='srgb_rec709_display':
        raise ValueError('The selected project display is not verified as IEC sRGB/Rec.709')
    processor=cfg.getProcessor(working.getName(),display,view,ocio.TRANSFORM_DIR_FORWARD)
    consumed={Path(file).resolve(strict=True) for file in processor.getProcessorMetadata().getFiles()}
    if not consumed.issubset(files):
        raise ValueError('An active color transform uses an undeclared OCIO dependency')
    return {**profile,'working_interop':working.getInteropID(),'working_space':working.getName(),
            'display_interop':destination.getInteropID(),'processor_cache_id':processor.getCacheID(),
            'runtime':ocio.__version__}
