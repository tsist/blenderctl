"""UV dependency closure with explicit existing UDIM tile protection.

The public adapter must hold its existing FileGuard handles on every declared
resource for the whole operation. This reader matches each strong dependency
against that guarded declaration and never guesses members by directory glob.
"""
import os
from pathlib import Path
from protocol import Failure,digest

_KINDS=('images','fonts','libraries','sounds','movieclips','volumes')


def _absolute(bpy,item,raw):
    return str(Path(bpy.path.abspath(raw,library=getattr(item,'library',None))).resolve())


def _packed(item):
    return bool(getattr(item,'packed_file',None)) or bool(getattr(item,'packed_files',[]))


def _tile_members(item,raw):
    """Return explicit external members and packed tile IDs, without file I/O."""
    numbers=[int(tile.number) for tile in item.tiles]
    if not numbers or len(set(numbers))!=len(numbers) or any(number<1001 for number in numbers):
        raise Failure('UNSUPPORTED','UDIM image has invalid or empty explicit tile numbers: '+item.name)
    packed={int(part.tile_number) for part in getattr(item,'packed_files',[])
            if getattr(part,'packed_file',None) and getattr(part,'tile_number',0) in numbers}
    external=[number for number in numbers if number not in packed]
    if external and (raw.count('<UDIM>')!=1 or '<UVTILE>' in raw or '####' in raw):
        raise Failure('UNSUPPORTED','Existing external UDIM requires one <UDIM> template: '+item.name)
    return external,sorted(packed)


def _dependencies(bpy):
    for kind in _KINDS:
        for item in getattr(bpy.data,kind,()):
            raw=getattr(item,'filepath','');source=getattr(item,'source','')
            if not raw or raw=='<builtin>':continue
            if source in ('SEQUENCE','MOVIE'):
                raise Failure('UNSUPPORTED','UV sequence/movie resource members are not supported: '+item.name)
            if kind=='images' and source=='TILED':
                external,packed=_tile_members(item,raw)
                template=_absolute(bpy,item,raw)
                for tile in external:
                    yield item,{'kind':kind,'name':item.name,'file':template.replace('<UDIM>',str(tile)),
                                'stored_path':raw,'absolute_template':template,'tile':tile,'member_source':'image.tiles'}
                continue
            if _packed(item):continue
            if any(token in raw for token in ('<UDIM>','<UVTILE>','####')):
                raise Failure('UNSUPPORTED','UV dynamic resource requires an explicit supported image tile source: '+item.name)
            yield item,{'kind':kind,'name':item.name,'file':_absolute(bpy,item,raw),'stored_path':raw}


def resources(bpy,spec):
    """Read strong static IDs and image.tiles; require declared file/SHA pairs."""
    declared={os.path.normcase(str(Path(row['file']).resolve())):row['expected_sha256'] for row in spec['resources']}
    rows=[]
    for item,row in _dependencies(bpy):
        path=row['file'];key=os.path.normcase(path)
        if key not in declared:raise Failure('CONFLICT','Undeclared UV dependency: '+path)
        if not Path(path).is_file():raise Failure('NOT_FOUND','UV resource member not found: '+path)
        if digest(path)!=declared[key]:raise Failure('CONFLICT','UV resource SHA mismatch: '+path)
        rows.append({**row,'sha256':declared[key]})
    packed=[]
    for image in getattr(bpy.data,'images',()):
        if image.source=='TILED' and image.filepath:
            _,tiles=_tile_members(image,image.filepath)
            if tiles:packed.append({'kind':'images','name':image.name,'tiles':tiles})
    return {'status':'checked_static_and_explicit_udim_paths','rows':rows,'packed_udim':packed,
            'support':'Strong static ID paths and explicit existing image.tiles UDIM members; no UV/visual approval',
            'limitations':['SEQUENCE/MOVIE and <UVTILE> resource expansion remain unsupported',
                           'UDIM members come only from image.tiles; files not listed as tiles are not inferred'],
            'packed_policy':'Packed static IDs are self-contained; each packed UDIM tile is excluded individually',
            'weak_reference_policy':'Only strong loaded Blender IDs; blend_paths weak records are not dependencies',
            'guard_policy':'Caller holds existing public UV source/resource FileGuards throughout operation'}


def absolute_paths(bpy):
    """Make external local paths absolute, retaining the UDIM template itself."""
    planned={}
    for item,row in _dependencies(bpy):
        if not getattr(item,'library',None):
            planned[(row['kind'],row['name'])]=(item,row.get('absolute_template',row['file']))
    for (kind,name),(item,path) in planned.items():
        # Image.filepath reloads/rescans UDIM members. The raw setter preserves
        # the exact already checked image.tiles list, even beside extra files.
        if kind=='images':item.filepath_raw=path
        else:item.filepath=path
    return {'status':'absolute_bindings_preserved','paths':len(planned),'udim_template':'<UDIM> retained'}
