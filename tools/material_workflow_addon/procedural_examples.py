"""Editable starter recipes, expressed only through the public 2.0 contract.

Coordinates are OBJECT/local, not world positions. Starter objects are expected
to use a centered local bounding box near -1..1. Height thresholds, scales,
grain direction and colors remain explicit editable signals in the manifest.
Wood uses one shared timber coordinate frame, warped growth rings and pores.
"""
from copy import deepcopy
import math

from .contract import WorkflowError

FAMILIES = ('painted_metal', 'wood', 'glass_clear', 'glass_tinted', 'glass_frosted')
GRAIN_AXES = ('X', 'Y', 'Z')
METAL_REGIONS = ('lower', 'edges', 'seam')


class _Recipe:
    def __init__(self, seed):
        self.signals = []
        self.seed = seed

    def source(self, identity, kind, parameters=None, inputs=None):
        self.signals.append({'id': identity, 'type': kind,
                             'parameters': parameters or {}, 'inputs': inputs or {}})
        return identity

    def value(self, identity, value):
        return self.source(identity, 'constant', {'value': value})

    def texture(self, identity, kind, scale, coordinate=(1, 1, 1), vector=None, **parameters):
        p = {'scale': scale, 'coordinate': {'space': 'OBJECT', 'scale': list(coordinate)}, **parameters}
        if kind in ('noise', 'wood'):
            p.setdefault('seed', (self.seed + len(self.signals) * 17) % 100001)
        return self.source(identity, kind, p, {'vector': vector} if vector else {})

    def remap(self, identity, value, low, high, out_low=0, out_high=1, smooth=True):
        return self.source(identity, 'remap', {'from_min': low, 'from_max': high,
            'to_min': out_low, 'to_max': out_high, 'clamp': True,
            'interpolation': 'SMOOTHSTEP' if smooth else 'LINEAR'}, {'value': value})

    def blend(self, identity, a, b, amount):
        return self.source(identity, 'mix', {},
            {'a': a, 'b': b, 'factor': self.value(identity + '_amount', amount)})

    def noise(self, identity, scale, coordinate=(1, 1, 1), detail=2, distortion=0):
        return self.source(identity, 'noise', {'scale': scale, 'detail': detail,
            'roughness': 0.55, 'distortion': distortion,
            'seed': (self.seed + len(self.signals) * 17) % 100001,
            'coordinate': {'space': 'OBJECT', 'scale': list(coordinate)}})

    def math(self, identity, operation, a, b, clamp=True):
        return self.source(identity, 'math', {'operation': operation, 'clamp': clamp}, {'a': a, 'b': b})

    def color(self, identity, value, a, b):
        return self.source(identity, 'ramp', {'stops': [
            {'position': 0, 'color': list(a)}, {'position': 1, 'color': list(b)}]}, {'value': value})

    def ranged(self, identity, value, low, high):
        delta = self.value(identity + '_span', high - low)
        scaled = self.math(identity + '_scaled', 'MULTIPLY', value, delta)
        return self.math(identity, 'ADD', self.value(identity + '_low', low), scaled)


def _layer(identity, name, *, mask=None, opacity=1, channels=None, **values):
    return {'id': identity, 'name': name, 'enabled': True, 'opacity': opacity,
            'mask': mask, 'channels': channels or {}, 'values': values}


def _metal(recipe, weathering, region_name):
    r = recipe
    one = r.value('one', 1)
    coarse = r.texture('moisture_patches', 'noise', 3.2, (1, 1, .75), detail=2, roughness=.55)
    mid = r.texture('broken_oxide_edges', 'noise', 19, detail=3, roughness=.65)
    fine = r.texture('oxide_granules', 'noise', 115, detail=2, roughness=.62)
    mixture = r.blend('macro_and_edge_noise', coarse, mid, .32)
    patches = r.remap('patch_distribution', mixture, .42, .62)
    h = r.source('bottom_distance', 'height_band', {'axis': 'Z', 'low': -.92,
        'high': -.30 if region_name == 'lower' else .15, 'coordinate': {'space': 'OBJECT'}})
    bottom = r.math('lower_contact_region', 'SUBTRACT', one, h)
    edge = r.source('contact_edges', 'edge', {'width': .006, 'samples': 8, 'gain': 24})
    cavity = r.source('sheltered_recess', 'cavity', {'distance': .035, 'samples': 8, 'only_local': True})
    if region_name == 'seam':
        region = r.value('rolled_seam_exposure', .30)
    elif region_name == 'edges':
        region = r.math('top_rim_region', 'MAXIMUM', edge, cavity)
    else:
        region = r.math('contact_and_moisture', 'MAXIMUM', bottom, edge)
    susceptibility = r.math('localized_weathering', 'MULTIPLY', region, patches)
    chip = r.remap('coating_loss', susceptibility, .25, .28)
    rust = r.remap('oxide_coverage', susceptibility, .25, .28)
    paint = r.math('intact_coating', 'SUBTRACT', one, chip)
    oxidecolor = r.source('oxide_palette', 'ramp', {'stops': [
        {'position': .25, 'color': [.033, .008, .003, 1]},
        {'position': .43, 'color': [.11, .023, .006, 1]},
        {'position': .60, 'color': [.24, .063, .014, 1]},
        {'position': .76, 'color': [.38, .13, .027, 1]}]}, {'value': mid})
    paint_rough = r.remap('paint_roughness', fine, .15, .85, .30, .39)
    rust_rough = r.remap('oxide_roughness', fine, .15, .85, .68, .90)
    rust_h = r.remap('oxide_relief', fine, .15, .85, .04, .40, False)
    paint_h = r.remap('paint_microtexture', fine, .1, .9, -.006, .006, False)
    dust = r.math('recess_dust', 'MULTIPLY', cavity, r.remap('dust_breakup', coarse, .30, .75))
    # Append the master after the tested fields so automatic seed assignments
    # and all color/frequency choices remain unchanged. At .4 the gain is 1.
    master = r.value('weathering_master', weathering)
    relative = r.math('weathering_relative', 'MULTIPLY', master,
        r.value('weathering_reference_gain', 2.5), clamp=False)
    driven = r.math('weathering_susceptibility', 'MULTIPLY', susceptibility, relative)
    weathered_dust = r.math('weathering_dust', 'MULTIPLY', dust, relative)
    for identity in (chip, rust):
        next(s for s in r.signals if s['id'] == identity)['inputs']['value'] = driven
    return [
        _layer('steel', 'Steel exposed along coating loss', metallic=1, roughness=.34,
               base_color=[.26, .28, .3, 1], height=-.07),
        _layer('paint', 'Intact industrial enamel', mask=paint, metallic=0,
               base_color=[.025, .12, .095, 1], coat_weight=.12, coat_roughness=.25,
               channels={'roughness': paint_rough, 'height': paint_h}),
        _layer('oxide', 'Localized porous oxide', mask=rust, metallic=0,
               channels={'base_color': oxidecolor, 'roughness': rust_rough, 'height': rust_h}),
        _layer('dust', 'Protected recess dust', mask=weathered_dust, opacity=.12,
               metallic=0, roughness=.85, base_color=[.12, .095, .06, 1], height=.02),
    ]


def _wood(recipe, weathering, grain_axis, timber_scale):
    r = recipe
    rotation = [0, math.pi/2, 0] if grain_axis == 'Z' else [0, 0, -math.pi/2] if grain_axis == 'Y' else [0, 0, 0]
    coordinates = r.source('timber_coordinates', 'coordinate', {'coordinate': {
        'space': 'OBJECT', 'rotation': rotation, 'scale': list(timber_scale)}})
    warped = r.source('growth_warp', 'warp', {'scale': 1.6, 'detail': 2,
        'roughness': .52, 'seed': r.seed, 'sample_scale': [.4, 1, 1],
        'strength': [0, .18, .4]}, {'vector': coordinates})
    rings = r.texture('growth_rings', 'wood', 10, (.45, .9, .9), warped,
        rings=True, direction='X', profile='SAW', distortion=0, detail=2)
    r.signals[-1]['parameters']['coordinate']['translation'] = [0, 0, .65]
    late = r.remap('latewood_band', rings, .76, .91)
    fibre = r.texture('long_fibres', 'noise', 4.5, (.45, 8, 8), warped, detail=3, roughness=.55)
    grain = r.blend('growth_and_fibres', fibre, late, .17)
    color = r.source('warm_timber_colors', 'ramp', {'stops': [
        {'position': .18, 'color': [.26, .12, .038, 1]},
        {'position': .44, 'color': [.22, .092, .028, 1]},
        {'position': .63, 'color': [.17, .063, .018, 1]},
        {'position': .86, 'color': [.11, .035, .009, 1]},
    ]}, {'value': grain})
    pores = r.texture('vessel_distance', 'voronoi', 1, (.8, 70, 70), warped, feature='F1', randomness=1)
    holes = r.remap('vessel_openings', pores, .08, .20, 1, 0)
    density = r.remap('earlywood_pore_bias', rings, .10, .55, 1, .15)
    pore_mask = r.math('pores_in_growth', 'MULTIPLY', holes, density)
    pore_color = r.blend('subtle_pore_color', color, r.value('pore_tone', [.055, .016, .004, 1]), .22)
    rough = r.remap('satin_roughness', fibre, .15, .85, .32, .43)
    pore_height = r.remap('recessed_pores', pore_mask, 0, 1, 0, -.10, False)
    micro = r.texture('micro_fibres', 'noise', 16, (1, 15, 15), warped, detail=2)
    fine_h = r.remap('micro_relief', micro, .1, .9, -.006, .006, False)
    height = r.math('timber_height', 'ADD', pore_height, fine_h, clamp=False)
    finalcolor = r.source('timber_color', 'mix', {}, {'a': color, 'b': pore_color, 'factor': pore_mask})
    layers = [_layer('timber', 'Satin timber with coherent end grain', metallic=0,
        coat_weight=.12, coat_roughness=.26,
        channels={'base_color': finalcolor, 'roughness': rough, 'height': height})]
    if weathering > 0:
        edge = r.source('edge_wear', 'edge', {'width': .003, 'samples': 8, 'gain': 22})
        wear = r.math('wood_wear_mask', 'MULTIPLY', edge, r.remap('wood_wear_breakup', fibre, .35, .75))
        cavity = r.source('wood_recesses', 'cavity', {'distance': .035, 'samples': 8, 'only_local': True})
        dust = r.math('wood_dust_mask', 'MULTIPLY', cavity, r.remap('wood_dust_breakup', fibre, .30, .70))
        layers.extend([
            _layer('wood_wear', 'Faint exposed edge grain', mask=wear, opacity=weathering * .15,
                base_color=[.24, .12, .04, 1], roughness=.5, metallic=0, height=.005, coat_weight=.08),
            _layer('wood_dust', 'Faint protected recess dust', mask=dust, opacity=weathering * .04,
                base_color=[.10, .072, .043, 1], roughness=.7, metallic=0, height=.01)])
    return layers


def _glass(recipe, family, weathering):
    r = recipe
    frosted = family == 'glass_frosted'
    tint = [0.64, 0.85, 0.72, 1] if family == 'glass_tinted' else [0.97, 0.99, 1, 1]
    fine = r.noise('glass_micro_surface', 110, detail=2)
    rough = r.ranged('glass_roughness', fine, 0.26 if frosted else 0.018, 0.37 if frosted else 0.035)
    height = r.math('glass_micro_height', 'MULTIPLY', fine,
        r.value('glass_micro_amplitude', 0.035 if frosted else 0.001))
    streak_noise = r.noise('vertical_scratch_noise', 1, coordinate=(90, 90, 0.18), detail=2)
    scratches = r.math('scratch_lines', 'GREATER_THAN', streak_noise, r.value('scratch_cutoff', 0.71))
    dirt_noise = r.noise('glass_stain_noise', 10, coordinate=(1, 1, 0.4), detail=2)
    upper = r.source('rim_height', 'height_band', {'axis': 'Z', 'low': 0.65, 'high': 0.98,
        'coordinate': {'space': 'OBJECT', 'scale': [1, 1, 1]}})
    stains = r.math('rim_stain_mask', 'MULTIPLY', upper,
        r.math('stain_fragments', 'GREATER_THAN', dirt_noise, r.value('stain_cutoff', 0.57)))
    optical = {'alpha': 1, 'transmission': 1, 'ior': 1.45, 'metallic': 0}
    return [
        _layer('glass', 'Frosted glass' if frosted else 'Tinted glass' if family == 'glass_tinted' else 'Clear glass',
               base_color=tint, channels={'roughness': rough, 'height': height}, **optical),
        _layer('scratches', 'Sparse longitudinal micro-scratches', mask=scratches,
               opacity=weathering * 0.28, base_color=tint, roughness=0.40, height=0.04, **optical),
        _layer('stains', 'Faint rim handling residue', mask=stains, opacity=weathering * 0.16,
               base_color=[0.84, 0.88, 0.79, 1], roughness=0.22, height=0.005, **optical),
    ]


def make_recipe(family, context, target, material_id, name=None, *, seed=7, weathering=0.4,
                grain_axis='X', metal_region='lower', timber_scale=(1, 1, 1)):
    """Return a fresh raw manifest; validation uses the final public contract.

    ``target`` and ``context`` are caller-owned values and are copied. This
    function never searches Blender objects, changes UVs or guesses dimensions.
    grain_axis is the local timber direction, mapped once into common X space.
    timber_scale is an explicit local sampling multiplier, applied before that
    axis rotation. Callers choose it from their model units and desired sampling
    scale; no object transform or physical unit is inferred. Defaults preserve
    the unit coordinate frame used by earlier callers.
    metal_region names the lower contact, edge or modeled seam exposure.
    """
    if family not in FAMILIES:
        raise WorkflowError('invalid_recipe', 'Unknown recipe family: ' + str(family))
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 100000:
        raise WorkflowError('invalid_recipe', 'seed must be an integer from 0 to 100000')
    if isinstance(weathering, bool) or not isinstance(weathering, (int, float)) or not 0 <= weathering <= 1 or not math.isfinite(weathering):
        raise WorkflowError('invalid_recipe', 'weathering must be finite from 0 to 1')
    if not isinstance(grain_axis, str) or grain_axis not in GRAIN_AXES:
        raise WorkflowError('invalid_recipe', 'grain_axis must be X, Y or Z')
    if not isinstance(metal_region, str) or metal_region not in METAL_REGIONS:
        raise WorkflowError('invalid_recipe', 'metal_region must be lower, edges or seam')
    if not isinstance(timber_scale, (tuple, list)) or len(timber_scale) != 3:
        raise WorkflowError('invalid_recipe', 'timber_scale must contain three positive finite numbers')
    for value in timber_scale:
        try:
            valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                     and value > 0 and math.isfinite(value))
        except (OverflowError, TypeError):
            valid = False
        if not valid:
            raise WorkflowError('invalid_recipe', 'timber_scale must contain three positive finite numbers')
    recipe = _Recipe(seed)
    layers = _metal(recipe, weathering, metal_region) if family == 'painted_metal' else _wood(recipe, weathering, grain_axis, timber_scale) if family == 'wood' else _glass(recipe, family, weathering)
    manifest = {'schema_version': '2.0', 'context': deepcopy(context), 'target': deepcopy(target),
        'material': {'id': material_id, 'name': name or family.replace('_', ' ').title(), 'template': 'procedural_pbr_v1'},
        'signals': recipe.signals, 'layers': layers,
        'height_distance': 0.001 if family in ('painted_metal', 'wood') else 0.00015,
        'mesh_map_size': 256 if family in ('painted_metal', 'wood') else 512,
        'preview': {'engine': 'CYCLES', 'device': {'backend': 'CPU'}, 'width': 512, 'height': 512,
                    'samples': 16, 'denoise': False, 'color': {'view': 'AgX', 'exposure': 0, 'gamma': 1},
                    'margin': 1.15, 'views': [
                        {'id': 'soft', 'azimuth': 45, 'elevation': 20, 'lighting': 'soft'},
                        {'id': 'raking', 'azimuth': -45, 'elevation': 10, 'lighting': 'raking'}]}}
    from .procedural_contract import normalize_manifest
    normalize_manifest(deepcopy(manifest))
    return manifest
