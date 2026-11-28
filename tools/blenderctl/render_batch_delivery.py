"""C5 frame products and resource estimates shared by scheduler and worker."""
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
import re

from protocol import Failure


PROFILES = {
    'REVIEW_PNG8': ('PNG', '8', '.png'),
    'MASTER_PNG16': ('PNG', '16', '.png'),
    'MASTER_EXR32': ('OPEN_EXR_MULTILAYER', '32', '.exr'),
}
PASS_NAME = re.compile(r'[^./\\\x00-\x1f]{1,64}\Z')
CRYPTO_CHANNEL_GROUP = re.compile(r'\.(?:CryptoObject|CryptoMaterial|CryptoAsset)\d{2}\.')


def dimensions(value, *, review=False):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(item) is not int or not 16 <= item <= 8192 for item in value)
            or (review and any(item % 2 for item in value))):
        raise Failure('INVALID_REQUEST', 'Resolution requires two integers in 16–8192. H.264 review dimensions must be even.')
    return tuple(value)


def frame_rate(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(item) is not int or not 1 <= item <= 120000 for item in value)):
        raise Failure('INVALID_REQUEST', 'Use a positive rational frame rate [numerator, denominator].')
    rate = Fraction(*value)
    if not 1 <= rate <= 120:
        raise Failure('INVALID_REQUEST', 'Review frame rate must be within 1–120 fps.')
    return rate


def normalize_shot_delivery(shot, delivery):
    profile = shot.setdefault('delivery_profile', delivery.get('delivery_profile', 'REVIEW_PNG8'))
    if not isinstance(profile, str) or profile not in PROFILES:
        raise Failure('INVALID_REQUEST', 'Unknown delivery_profile.')
    shot['resolution'] = list(dimensions(shot.get('resolution', (delivery['width'], delivery['height']))))
    names = shot.setdefault('output_passes', [])
    if (not isinstance(names, list) or len(names) > 64
            or any(not isinstance(name, str) or not PASS_NAME.fullmatch(name) for name in names)
            or len(set(names)) != len(names)):
        raise Failure('INVALID_REQUEST', 'Use at most 64 unique output_passes names without path separators or dots.')
    if names and profile != 'MASTER_EXR32':
        raise Failure('INVALID_REQUEST', 'Separate output passes require MASTER_EXR32.')
    if profile == 'MASTER_EXR32' and 'Combined' not in names:
        shot['output_passes'] = ['Combined', *names]
    shot.setdefault('compositor_enabled', False)
    if type(shot['compositor_enabled']) is not bool:
        raise Failure('INVALID_REQUEST', 'compositor_enabled must be Boolean.')
    codec = shot.setdefault('exr_codec', 'ZIP')
    if codec not in ('ZIP', 'DWAA'):
        raise Failure('INVALID_REQUEST', 'Use ZIP or DWAA for EXR.')
    override = shot.get('adaptive_sampling')
    if override is not None:
        if (not isinstance(override, dict) or set(override) != {'enabled', 'threshold', 'min_samples'}
                or type(override['enabled']) is not bool or type(override['threshold']) not in (int, float)
                or not 0 <= override['threshold'] <= 1 or type(override['min_samples']) is not int
                or not 0 <= override['min_samples'] <= 4096):
            raise Failure('INVALID_REQUEST', 'adaptive_sampling requires enabled, threshold [0,1], and min_samples [0,4096].')


def validate_channels(images, pass_names, depth, layer):
    channels = {channel for image in images for channel in image['channels']}
    for name in pass_names:
        if not any(channel.startswith(f'{layer}.{name}.') for channel in channels):
            raise Failure('VALIDATION_FAILED', f'Missing requested EXR layer/pass: {name}')
    if depth == '32' and any(item['format'] != 'float' or any(fmt != 'float' for fmt in item.get('channel_formats', [])) for item in images):
        raise Failure('VALIDATION_FAILED', 'EXR channel depth differs from float32.')


def validate_dwaa_crypto_storage(images):
    """Verify Blender uses ZIP for native Cryptomatte parts in a DWAA delivery."""
    for image in images:
        ids=[channel for channel in image['channels'] if CRYPTO_CHANNEL_GROUP.search(channel)]
        if not ids:
            continue
        metadata=image.get('metadata',{})
        names={channel.rsplit('.',1)[0] for channel in ids}
        expected={name+'.'+suffix for name in names for suffix in ('r','g','b','a')}
        if (set(ids)!=expected or image['format']!='float' or metadata.get('colorInteropID')!='data'
                or metadata.get('compression')!='zip'):
            raise Failure('VALIDATION_FAILED','DWAA delivery requires lossless native ZIP Cryptomatte parts')


def frame_bytes(size, channel_count, depth):
    width, height = size
    per_channel = {'8': 1, '16': 2, '32': 4}[depth]
    return width * height * max(4, channel_count) * per_channel + 65536


def hash_frames(candidates, digest, threads=4):
    def verified(item):
        frame, path, record = item
        try:
            if path.is_file() and digest(path) == record['sha256']:
                return frame, record
        except (OSError, KeyError):
            pass
        return frame, None

    with ThreadPoolExecutor(max_workers=threads) as pool:
        return {frame: record for frame, record in pool.map(verified, candidates) if record is not None}
