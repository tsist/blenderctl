"""Host-safe UV contract. Unsupported constraints are explicit, never discarded."""
import copy
import math
import re

class WorkflowError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

def fail(message, code="INVALID_REQUEST"):
    raise WorkflowError(code, message)

def _number(value):
    return type(value) in (int, float) and math.isfinite(value)

def normalize_edit(v, names):
    selection=v.get('selection')
    if not isinstance(selection,dict) or not selection or set(selection)-set(names):
        fail('edit.selection must name at least one target object')
    for name,row in selection.items():
        if not isinstance(row,dict) or set(row)!={'domain','indices'} or row['domain'] not in ('ISLANDS','FACES','LOOPS'):
            fail('Selection requires domain ISLANDS/FACES/LOOPS and explicit indices')
        ids=row['indices']
        if not isinstance(ids,list) or not ids or len(ids)>1000000 or any(type(i) is not int or i<0 for i in ids) or len(ids)!=len(set(ids)):
            fail('Selection indices must be unique nonnegative integers')
    action=v.get('action')
    if not isinstance(action,dict):fail('edit.action is required')
    fields={'translate':({'delta_px'},set()),'rotate':({'angle_degrees'},{'pivot_px'}),
            'scale':({'factors'},{'pivot_px'}),'mirror':({'axis'},{'pivot_px'}),
            'align':({'axis'},{'value_px'}),'straighten':(set(),set()),
            'density':({'target_px_m'},set()),'pin':({'enabled'},set()),'lock':({'enabled'},set()),
            'relax':({'iterations'},set()),'unwrap':({'method'},{'iterations','no_flip'}),
            'pack':({'rotate','scale','pin_method'},set()),
            'stitch':({'pairs','limit_px','seam_mode'},set()),
            'sync_pins':(set(),set()),'split':({'delta_px'},set()),
            'orient':({'edge_loops','angle_degrees'},set()),
            'stack':({'reference','pairs','mirrored','group_id'},set()),'unstack':({'offset_px'},set()),
            'merge_micro_islands':(set(),{'threshold_px2'})}
    kind=action.get('kind')
    if kind not in fields:fail('Unknown edit action')
    required,optional=fields[kind]
    if set(action)-required-optional-{'kind'} or required-set(action):fail('Unexpected or missing edit action fields')
    for key in ('delta_px','factors','pivot_px','offset_px'):
        if key in action and (not isinstance(action[key],list) or len(action[key])!=2 or not all(_number(x) for x in action[key])):
            fail(key+' must contain two finite numbers')
    for key in ('angle_degrees','value_px','target_px_m'):
        if key in action and not _number(action[key]):fail(key+' must be finite')
    if 'axis' in action and action['axis'] not in ('U','V'):fail('axis must be U or V')
    if 'factors' in action and min(action['factors'])<=0:fail('scale factors must be positive; mirror is explicit')
    if 'target_px_m' in action and action['target_px_m']<=0:fail('density must be positive')
    if 'enabled' in action and type(action['enabled']) is not bool:fail('enabled must be boolean')
    if kind=='split':
        if any(r['domain']!='FACES' for r in selection.values()):fail('Split requires an explicit moving face region')
        if action['delta_px']==[0,0]:fail('Split requires a nonzero pixel displacement')
    if kind in ('orient','stack','unstack') and any(r['domain']!='ISLANDS' for r in selection.values()):fail('Orientation/stack/unstack require complete islands')
    if kind=='orient':
        if len(selection)!=1:fail('Orient one explicit island at a time')
        ids=action['edge_loops']
        if not isinstance(ids,list) or len(ids)!=2 or any(type(i) is not int or i<0 for i in ids) or ids[0]==ids[1]:fail('Orient requires two explicit edge corners')
    if kind=='stack':
        if not isinstance(action['group_id'],str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,47}',action['group_id']):fail('Stack group_id must be a short ASCII identifier')
        if type(action['mirrored']) is not bool:fail('Stack mirrored must be boolean')
        ref=action['reference']
        if not isinstance(ref,dict) or set(ref)!={'object','face'} or ref['object'] not in names or type(ref['face']) is not int or ref['face']<0:fail('Stack reference requires target object and face seed')
        if not isinstance(action['pairs'],dict) or set(action['pairs'])!=set(selection):fail('Stack pairs must match selected objects')
        for rows in action['pairs'].values():
            if not isinstance(rows,list) or not rows or any(not isinstance(p,list) or len(p)!=2 or any(type(i) is not int or i<0 for i in p) for p in rows):fail('Invalid stack corner pairs')
    if kind=='unstack' and action['offset_px']==[0,0]:fail('Unstack requires a nonzero offset')
    if kind=='density' and any(r['domain']!='ISLANDS' for r in selection.values()):fail('density requires whole islands')
    if kind=='lock' and any(r['domain']=='LOOPS' for r in selection.values()):fail('lock requires faces or islands; use pin for corners')
    if kind in ('relax','unwrap') and any(r['domain']=='LOOPS' for r in selection.values()):
        fail('Local solve requires complete faces or islands; chart and selection boundaries stay fixed')
    if kind=='relax' and (type(action['iterations']) is not int or not 1<=action['iterations']<=1000):
        fail('relax iterations must be an integer from 1 to 1000')
    if kind=='unwrap':
        if action['method'] not in ('ANGLE_BASED','CONFORMAL','MINIMUM_STRETCH'):
            fail('Local unwrap method must be ANGLE_BASED, CONFORMAL, or MINIMUM_STRETCH')
        if action['method']!='MINIMUM_STRETCH' and any(k in action for k in ('iterations','no_flip')):
            fail('iterations and no_flip only apply to MINIMUM_STRETCH unwrap')
        if action['method']=='MINIMUM_STRETCH':
            action=dict(action)
            action.setdefault('iterations',10)
            action.setdefault('no_flip',True)
            if type(action['iterations']) is not int or not 1<=action['iterations']<=100:
                fail('SLIM unwrap iterations must be an integer from 1 to 100')
            if type(action['no_flip']) is not bool:
                fail('SLIM unwrap no_flip must be boolean')
            v['action']=action
    if kind=='merge_micro_islands':
        if len(names)!=1 or set(selection)!=set(names) or any(r['domain']!='ISLANDS' for r in selection.values()):
            fail('Micro-island merge requires one target and complete island selection')
        threshold=action.get('threshold_px2',64.0)
        if not _number(threshold) or threshold<=0:
            fail('Micro-island threshold_px2 must be finite and positive')
        action['threshold_px2']=float(threshold)
    if kind=='pack':
        if any(r['domain']!='ISLANDS' for r in selection.values()):fail('Packing requires complete islands')
        if type(action['rotate']) is not bool or type(action['scale']) is not bool or action['pin_method']!='LOCKED':
            fail('Packing requires boolean rotate/scale and pin_method=LOCKED')
    if kind=='stitch':
        if any(r['domain']!='LOOPS' for r in selection.values()):fail('Stitch requires explicit moving corner selections')
        if not _number(action['limit_px']) or action['limit_px']<=0:fail('Stitch limit_px must be finite and positive')
        if action['seam_mode'] not in ('KEEP','CLEAR_JOINED'):fail('Stitch seam_mode must be KEEP or CLEAR_JOINED')
        pairs=action['pairs']
        if not isinstance(pairs,dict) or set(pairs)!=set(selection):fail('Stitch pairs must cover exact selected objects')
        for rows in pairs.values():
            if not isinstance(rows,list) or not rows or len(rows)>1000000 or any(
                not isinstance(p,list) or len(p)!=2 or any(type(i) is not int or i<0 for i in p) for p in rows):
                fail('Stitch pairs must be [moving_loop,static_loop] integer pairs')
    binding=v.get('selection_binding')
    if binding is not None:
        if not isinstance(binding,dict) or set(binding)!={'candidate','geometry','uv_hash'} or binding['candidate']!=v['candidate']:
            fail('Invalid selection binding')
        for key in ('geometry','uv_hash'):
            if not isinstance(binding[key],dict) or set(binding[key])!=set(selection) or any(not isinstance(h,str) or re.fullmatch('[0-9a-f]{64}',h) is None for h in binding[key].values()):
                fail('Selection binding must cover the exact selected objects')
    return v

def normalize(value):
    if not isinstance(value, dict): fail("manifest must be an object")
    v = copy.deepcopy(value)
    allowed = {"version", "operation", "scene", "view_layer", "objects", "candidate", "methods",
               "slim_iterations", "slim_no_flip", "constraints", "width", "height", "margin_px",
               "border_px", "pair_budget", "density_px_m", "selection", "selection_binding", "action",
               "recipe", "settings", "seam_action", "seam_suggestions", "lightmap", "source_layers",
               "transfer", "transfer_unit_scale", "surface", "diagnostics"}
    if set(v) - allowed: fail("Unknown manifest fields: " + str(sorted(set(v) - allowed)))
    if v.get("version") != "1.0": fail("version must be 1.0")
    if v.get("operation") not in ("generate", "inspect", "adopt", "restore", "validate", "edit", "recipe", "settings", "seams", "lightmap", "copy_uv", "transfer"):
        fail("Unknown UV operation")
    for key in ("scene", "view_layer"):
        if not isinstance(v.get(key), str) or not v[key]: fail("Missing " + key)
    names = v.get("objects")
    if not isinstance(names, list) or not names or any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(names):
        fail("objects must be explicit unique object names")
    is_explicit_seam_helper = (
        v["operation"] == "seams"
        and isinstance(v.get("seam_action"), dict)
        and v["seam_action"].get("mode") in ("MARK_BY_ANGLE", "MARK_FROM_SHARP")
    )
    if v["operation"] not in ("inspect", "lightmap") and not is_explicit_seam_helper:
        if not isinstance(v.get("candidate"), str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,47}", v["candidate"]):
            fail("candidate must be a short ASCII identifier")
    elif "candidate" in v:
        if not isinstance(v["candidate"], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,47}", v["candidate"]):
            fail("candidate must be a short ASCII identifier")
    if v["operation"] == "generate" and len(v["candidate"]) > 32:
        fail("generate candidate prefix must be <= 32 characters")
    if 'seam_suggestions' in v:
        if v['operation'] != 'inspect':
            fail('seam_suggestions is an inspect-only field')
        spec = {} if v['seam_suggestions'] is True else v['seam_suggestions']
        if not isinstance(spec, dict) or set(spec) - {'angle_degrees', 'include_boundaries', 'max_examples'}:
            fail('seam_suggestions accepts optional angle_degrees, include_boundaries, max_examples')
        spec = dict(spec)
        spec.setdefault('angle_degrees', 60.0)
        spec.setdefault('include_boundaries', True)
        spec.setdefault('max_examples', 32)
        if not _number(spec['angle_degrees']) or not (0.0 <= spec['angle_degrees'] <= 180.0):
            fail('seam_suggestions.angle_degrees must be between 0 and 180')
        if type(spec['include_boundaries']) is not bool:
            fail('seam_suggestions.include_boundaries must be boolean')
        if type(spec['max_examples']) is not int or not (1 <= spec['max_examples'] <= 512):
            fail('seam_suggestions.max_examples must be 1 to 512')
        v['seam_suggestions'] = spec
    if 'surface' in v:
        if v['operation']!='inspect':fail('surface is a read-only inspect request; no UV or deformation is written')
        from .surface_contract import normalize as normalize_surface
        v['surface']=normalize_surface(v['surface'],names)
    if 'diagnostics' in v:
        spec=v['diagnostics']
        if v['operation']!='inspect' or not isinstance(spec,dict) or set(spec)-{'max_examples'}:
            fail('diagnostics is an inspect-only object with optional max_examples')
        spec.setdefault('max_examples',1000)
        if type(spec['max_examples']) is not int or not 1<=spec['max_examples']<=10000:
            fail('diagnostics.max_examples must be 1 to 10000 per issue')
    if v['operation'] in ('settings','seams'):
        field='settings' if v['operation']=='settings' else 'seam_action'
        allowed_rule={'version','operation','scene','view_layer','objects','candidate',field}
        required_rule=allowed_rule if (v['operation']=='settings' or not is_explicit_seam_helper) else (allowed_rule-{'candidate'})
        if not required_rule.issubset(v) or (set(v)-allowed_rule):fail('Rule update accepts only identity and '+field)
        if field=='settings':
            from .policies import normalize as normalize_settings
            v[field]=normalize_settings(v.get(field),names)
        else:
            action=v[field]
            mode=action.get('mode') if isinstance(action,dict) else None
            if mode in ('MARK','CLEAR','REQUIRE_CUT','REQUIRE_JOIN','FREE'):
                if set(action)!={'mode','edges'}:fail('Invalid seam action')
                if not isinstance(action['edges'],dict) or not action['edges'] or set(action['edges'])-set(names):fail('Seam edges must name explicit targets')
                for ids in action['edges'].values():
                    if not isinstance(ids,list) or not ids or any(type(i) is not int or i<0 for i in ids) or len(set(ids))!=len(ids):fail('Seam edges require unique indices')
            elif mode=='MARK_BY_ANGLE':
                if set(action)-{'mode','angle_degrees','include_boundaries','sync_sharp','clear_existing'}:fail('Invalid MARK_BY_ANGLE seam action fields')
                normalized=dict(action)
                normalized.setdefault('angle_degrees',60.0)
                normalized.setdefault('include_boundaries',True)
                normalized.setdefault('sync_sharp',False)
                normalized.setdefault('clear_existing',False)
                if not _number(normalized['angle_degrees']) or not (0.0<=normalized['angle_degrees']<=180.0):fail('seam_action.angle_degrees must be between 0 and 180')
                for bkey in ('include_boundaries','sync_sharp','clear_existing'):
                    if type(normalized[bkey]) is not bool:fail('seam_action.'+bkey+' must be boolean')
                v[field]=normalized
            elif mode=='MARK_FROM_SHARP':
                if set(action)-{'mode','include_boundaries','clear_existing'}:fail('Invalid MARK_FROM_SHARP seam action fields')
                normalized=dict(action)
                normalized.setdefault('include_boundaries',True)
                normalized.setdefault('clear_existing',False)
                for bkey in ('include_boundaries','clear_existing'):
                    if type(normalized[bkey]) is not bool:fail('seam_action.'+bkey+' must be boolean')
                v[field]=normalized
            else:
                fail('Invalid seam action')
        return v
    if v['operation']=='lightmap':
        allowed_lm={'version','operation','scene','view_layer','objects','candidate','width','height','pair_budget','lightmap'}
        extra=set(v)-allowed_lm
        if extra:fail('Lightmap operation does not accept layout/edit fields: '+str(sorted(extra)))
        spec=v.get('lightmap',{})
        if not isinstance(spec,dict) or set(spec)-{'layer','width','height','box_div','margin_div','overwrite'}:
            fail('lightmap accepts optional layer, width, height, box_div, margin_div, overwrite')
        for key,default in (('width',1024),('height',1024),('pair_budget',2000000)):
            v.setdefault(key,default)
            if type(v[key]) is not int or v[key]<=0:fail(key+' must be a positive integer')
        if max(v['width'],v['height'])>16384 or v['pair_budget']>10000000:fail('M1 request budget exceeded','RESOURCE_LIMIT')
        lm=dict(spec)
        lm.setdefault('layer','UVMap_Lightmap')
        lm.setdefault('width',v['width'])
        lm.setdefault('height',v['height'])
        lm.setdefault('box_div',12)
        lm.setdefault('margin_div',0.1)
        lm.setdefault('overwrite',False)
        if not isinstance(lm['layer'],str) or not lm['layer'] or len(lm['layer'])>64 or lm['layer'].startswith('UVW_'):
            fail('lightmap.layer must be a valid non-candidate UV layer name (max 64 chars)')
        for dim in ('width','height'):
            if type(lm[dim]) is not int or not (64<=lm[dim]<=8192):fail('lightmap.'+dim+' must be 64 to 8192')
        if type(lm['box_div']) is not int or not (1<=lm['box_div']<=48):fail('lightmap.box_div must be 1 to 48')
        if not _number(lm['margin_div']) or not (0.0<lm['margin_div']<=0.5):fail('lightmap.margin_div must be > 0 and <= 0.5')
        if type(lm['overwrite']) is not bool:fail('lightmap.overwrite must be boolean')
        v['lightmap']=lm
        return v
    if v['operation']=='recipe':
        extra=set(v)-{'version','operation','scene','view_layer','objects','candidate','recipe'}
        if extra:fail('Recipe binding does not accept layout or edit fields: '+str(sorted(extra)))
        from .recipes import normalize_recipe
        v['recipe']=normalize_recipe(v.get('recipe'),names)
        return v
    if v['operation']=='transfer':
        if 'methods' in v:fail('Transfer uses explicit correspondence; generation methods do not apply')
    else:
        v.setdefault("methods", ["SMART", "ANGLE_BASED"])
        if not isinstance(v["methods"], list) or not 1 <= len(v["methods"]) <= 3 or len(set(v["methods"])) != len(v["methods"]): fail("Choose 1 to 3 unique methods")
        if any(m not in ("SMART", "SMART_ABF", "SMART_REPAIR", "ANGLE_BASED", "CONFORMAL", "MINIMUM_STRETCH") for m in v["methods"]): fail("Unsupported method")
    if v['operation'] not in ('generate','inspect') and any(k in v for k in ('slim_iterations','slim_no_flip')):
        fail('slim_iterations and slim_no_flip only apply to generate')
    if v['operation']=='generate' and ('MINIMUM_STRETCH' in v['methods'] or any(k in v for k in ('slim_iterations','slim_no_flip'))):
        v.setdefault('slim_iterations',10)
        v.setdefault('slim_no_flip',True)
        if type(v['slim_iterations']) is not int or not (1<=v['slim_iterations']<=100):
            fail('slim_iterations must be an integer from 1 to 100')
        if type(v['slim_no_flip']) is not bool:
            fail('slim_no_flip must be boolean')
    for key, default in (("width", 2048), ("height", 2048), ("pair_budget", 2000000)):
        v.setdefault(key, default)
        if type(v[key]) is not int or v[key] <= 0: fail(key + " must be a positive integer")
    for key, default in (("margin_px", 16.0), ("border_px", 8.0)):
        v.setdefault(key, default)
        if not isinstance(v[key], (int, float)) or isinstance(v[key], bool) or not math.isfinite(v[key]) or v[key] < 0: fail("Invalid " + key)
    if max(v["width"], v["height"]) > 16384 or v["pair_budget"] > 10000000: fail("M1 request budget exceeded", "RESOURCE_LIMIT")
    if 2*v["border_px"] >= min(v["width"],v["height"]): fail("Border leaves no usable tile")
    if "density_px_m" in v and (not isinstance(v["density_px_m"], (int,float)) or not math.isfinite(v["density_px_m"]) or v["density_px_m"] <= 0): fail("Invalid density target")
    if v['operation'] in ('generate','copy_uv','transfer'):v.setdefault("constraints", {})
    if 'constraints' in v and (not isinstance(v["constraints"], dict) or set(v["constraints"]) - set(names)): fail("Constraints must name target objects")
    for name, c in v.get("constraints",{}).items():
        if not isinstance(c, dict) or set(c) - {"must_cut", "must_not_cut", "pins", "locked_faces", "source_uv"}: fail("Unknown constraint for " + name)
        for key in ("must_cut", "must_not_cut", "pins", "locked_faces"):
            c.setdefault(key, [])
            if not isinstance(c[key], list) or any(type(i) is not int or i < 0 for i in c[key]) or len(set(c[key])) != len(c[key]): fail("Invalid constraint indexes")
        if set(c["must_cut"]) & set(c["must_not_cut"]): fail("Contradictory seam constraints")
        if "source_uv" in c and not isinstance(c["source_uv"], str): fail("source_uv must be a name")
    if v['operation']=='edit':normalize_edit(v,names)
    elif v['operation']!='inspect' and any(k in v for k in ('selection','selection_binding','action')):
        fail('Selection and action fields are only valid for edit')
    if 'recipe' in v and v['operation']!='inspect':
        fail('Use operation=recipe to bind or change a purpose policy')
    if v['operation']=='copy_uv':
        layers=v.get('source_layers')
        if not isinstance(layers,dict) or set(layers)!=set(names) or any(not isinstance(n,str) or not n for n in layers.values()):fail('copy_uv requires an exact object-to-source-layer map')
    elif 'source_layers' in v and v['operation']!='inspect':fail('source_layers only applies to copy_uv')
    if v['operation']=='transfer':
        from .transfer_contract import normalize as normalize_transfer
        normalize_transfer(v,names)
    elif v['operation']!='inspect' and any(k in v for k in ('transfer','transfer_unit_scale')):
        fail('Transfer fields only apply to operation=transfer')
    if v['operation']!='inspect' and any(k in v for k in ('settings','seam_action')):fail('Use explicit settings/seams operation')
    if v['operation']!='inspect' and 'lightmap' in v:fail('Use operation=lightmap for auxiliary lightmap UV generation')
    return v

def describe():
    return {"version": "0.8.9", "milestone": "Protected micro-island merge and packing", "operations": ["generate", "inspect", "validate", "adopt", "restore", "edit", "recipe", "copy_uv", "settings", "seams", "lightmap", "transfer"],
            "edit_actions": ['translate','rotate','scale','mirror','align','straighten','density','pin','lock','relax','unwrap','stitch','pack','sync_pins','split','orient','stack','unstack','merge_micro_islands'],
            "merge_micro_islands": "2048×2048 unadopted static triangle candidate, one object, full exclusive atlas, no pins/seams/locks/reuse. Merge adjacent charts, pack and commit only with complete quality checks.",
            "pin_sync":"Explicit selected native pin import after validate; coordinates and native flags unchanged, locked-face anchors preserved",
            "split":"Move explicit FACES by delta_px, split continuous selection boundaries and mark seams; preserve prior cuts and unselected UV",
            "selection": "ISLANDS uses face seeds; FACES/LOOPS are exact indices; optional captured selection_binding must still match",
            "methods": ["SMART", "SMART_ABF", "SMART_REPAIR", "ANGLE_BASED", "CONFORMAL", "MINIMUM_STRETCH"],
            "smart_repair": {"scope":"SMART projection followed by complete per-chart checks and isolated bridge-slit ABF repair of shared-vertex open boundary fan folds; global density/pack and full quality checks still apply", "input":"explicit triangular faces only; no pins, locked faces or explicit cut/join constraints", "failures":"unsupported fold structure or incomplete pair checks refuse this candidate; no hidden fallback, source edits or ignored positive-area overlap", "fragmentation":"not a micro-island merger; new cuts may increase chart count"},
            "slim": {"method": "MINIMUM_STRETCH", "fields": {"slim_iterations": "1..100, default 10", "slim_no_flip": "bool, default True"}, "edit_unwrap_fields": {"iterations": "1..100, default 10", "no_flip": "bool, default True"}},
            "contract": "version=1.0; explicit scene/view_layer/objects; candidate id for candidate mutations",
            "method_seams": "ANGLE_BASED/CONFORMAL/MINIMUM_STRETCH use authored mesh seams, topological boundaries and explicit must_cut/must_not_cut overrides without implicit auto-seams during generate. Explicit seams operation supports MARK_BY_ANGLE / MARK_FROM_SHARP prior to or on candidates. SMART/SMART_ABF retain automatic projection planning.",
            "seam_suggestions": {"entry": "uv.inspect manifest.seam_suggestions", "fields": {"angle_degrees": "0..180, default 60.0", "include_boundaries": "bool, default True", "max_examples": "1..512, default 32"}, "scope": "Read-only angle/sharp/boundary seam suggestions; never mutates mesh or generates seams implicitly during generate"},
            "lightmap": {"operation": "lightmap", "fields": {"layer": "default UVMap_Lightmap", "width": "64..8192, default 1024", "height": "64..8192, default 1024", "box_div": "1..48, default 12", "margin_div": "0..0.5, default 0.1", "overwrite": "bool, default False"}, "semantics": "Creates or explicitly updates auxiliary non-active_render UV2 via native lightmap_pack; preserves existing active/active_render layers, seams, pins and geometry; verifies zero overlap and [0,1] bounds"},
            "protection": "New candidate layers only; original layers and render bindings retained; adoption selects editing layer and seam plan",
            "editable_deformation": "Local single-user meshes retain shape keys, vertex groups/weights, represented modifier settings, target and referenced rig animations, rest/pose data and non-UV attributes. Base-mesh solving clears only temporary copied Keys. Unsupported protected structures are refused; final evaluated quality is separately sampled",
            "geometry_diagnostics": {"entry":"uv.inspect manifest.diagnostics", "max_examples":"1 to 10000, default 1000", "scope":"Read-only base geometry; complete issue counts and bounded object/face/edge/vertex/loop locations; geometry identity and modeling handoff. No mesh repair or arbitrary geometric self-intersection proof"},
            "panel_evidence": "Read-only disk surface report with source SHA and explicit frames/modes; dirty workspace is not approved. Current-pose checker uses frozen VIEWPORT geometry. Explicit uv.run/uv.inspect disk job monitor survives file loads; job success is not quality acceptance",
            "constraints": "Generate-time must_cut, must_not_cut, pins, locked_faces and source_uv per object; existing candidates reject changed explicit constraints. Managed edit pin/lock/sync_pins, seams and settings operations update existing rules explicitly",
            "copy_uv":"Exact source_layers object-to-layer map copied to a new candidate; preserves originals, active/render bindings and pins; existing or unresolved UDIM consumers are protected",
            "transfer":{"modes":["INDEXED","EXPLICIT","PROJECTED"],"fields":["transfer_unit_scale","transfer (exact target-name map)"],
                "explicit_vertex_domain":{"field":"vertex_domain (optional per EXPLICIT row)","default":"ALL","options":["ALL","FACE_REFERENCED"],"semantics":"FACE_REFERENCED requires complete corner/face and referenced-vertex bijections; reports total/referenced/excluded vertices and preserves all geometry. Full-mesh vertex_bijection remains false if excluded vertices are unmapped. INDEXED/PROJECTED do not accept this option."},
                "bindings":"Each source_object/source_uv has source_binding geometry/mesh/uv_hash; target_binding geometry/mesh/protection/active_uv. Inspect exposes transfer_identity. Sources and targets must be disjoint in one scene/view layer",
                "semantics":"New target candidate, original UV/render and sources retained; target native pin constraints retained; no source purpose approval inherited. PROJECTED requires explicit face regions, full single-layer projected coverage and piecewise UV representability; not arbitrary nearest-surface transfer"},
            "seams":{"modes":["MARK","CLEAR","REQUIRE_CUT","REQUIRE_JOIN","FREE","MARK_BY_ANGLE","MARK_FROM_SHARP"],"scope":"explicit object-to-edge index map or geometric angle/sharp rules","semantics":"marking and hard cut/join constraints are distinct; MARK_BY_ANGLE/MARK_FROM_SHARP can also run before generate on unadopted base meshes; sync_sharp=True is explicit opt-in"},
            "settings":{"fields":["version","binding","mode","reference","reuse_groups","density","directions"],"modes":["EXCLUSIVE","TILED"],
                "semantics":"Exact geometry/unit/tile binding; complete island face membership; explicit per-island px/m and oriented-edge constraints; raw metrics retained. TILED means single-image repetition, never UDIM authoring"},
            "reuse":"Complete explicit corner bijection for stack/mirror; identical full face footprints per declared group. Raw/declared/undeclared positive pairs counted independently; same-island folds always fail. Pack requires all members of each selected group",
            "quality": "Independent checks plus exact-bound purpose policy when assigned; visual review remains separate; no universal P0 threshold",
            "recipe": {"operation":"recipe", "fields":["version=1.0","policy","binding"],
                "policy_fields":["name","purpose","reference","density_basis=ISLAND_AREA_REFERENCE|CONFIGURED_TARGETS","stretch_p95_max","stretch_max","density_relative_max","border_px_min","gap_px_min"],
                "binding_fields":["geometry (exact object-name to geometry hash map)","geometry_basis=BASE_MESH","unit_scale","width","height"],
                "semantics":"Explicit bind/change only; portable policy must be re-reviewed and explicitly rebound for a different model/unit/tile; reference is caller-supplied, never authenticated approval"},
            "surface_inspection":{"entry":"uv.inspect manifest.surface", "required":["layers","frames","modes","triangle_limit","usage=EXCLUSIVE|CANDIDATE_EXACT"],
                "optional":"policy with ISLAND_AREA_REFERENCE density; candidate required only for CANDIDATE_EXACT", "frames":"1 to 8 explicit integer frames", "modes":["VIEWPORT","RENDER"],
                "exact_reuse":"Explicit existing complete managed candidate, matching tile/unit/geometry/mesh/UV/pins/settings. EXCLUSIVE nonempty reuse only; each actual sample must have finite exact ordered geometry/topology/UV/island identity or is unknown. No changed-topology correspondence or inherited purpose approval",
                "execution":"Read-only saved-disk background worker. RENDER uses a capture-engine depsgraph; configured renderer equivalence, shader displacement and adaptive engine subdivision are not certified. No pixels or source save; base approval is not inherited"},
            "not_yet_supported": ["UDIM creation", "Trim Sheet", "geometry repair", "shared mesh mutation", "arbitrary unrepresented deformation structures", "changed-topology evaluated reuse/group correspondence", "live GUI multi-frame surface sampling", "configured-renderer displacement or adaptive subdivision equivalence"]}
