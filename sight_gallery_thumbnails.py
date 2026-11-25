"""Save visible, complete sight gallery tiles from a confirmed Android screen."""
import hashlib
import io
import json
import subprocess
from collections import Counter
from xml.etree.ElementTree import ParseError
from zipfile import ZipFile

from PIL import Image, ImageStat

from core import bounds
from gallery_capture import read_gallery_screenshot
from sight_guide import app_nodes, text
from sight_snapshot import APP, unique_value


def locate_tiles(scroller):
    if scroller is None:
        return []
    wrappers = [node for node in scroller if node.get('package') == APP
                and node.get('class') == 'android.view.ViewGroup']
    if len(wrappers) != 1:
        return []
    left, top, right, bottom = bounds(scroller)
    tiles = []
    for tile in wrappers[0]:
        if tile.get('package') != APP:
            continue
        images = [node for node in tile if node.get('package') == APP
                  and node.get('class') == 'android.widget.ImageView' and node.get('resource-id') == 'image']
        if len(images) != 1:
            continue
        box = bounds(images[0])
        x, y, x2, y2 = box
        width, height = x2 - x, y2 - y
        if (box != bounds(tile) or not (left <= x < x2 <= right and top <= y < y2 <= bottom)
                or min(width, height) < 64 or abs(width - height) > 2):
            continue
        label = unique_value(word for node in app_nodes(tile) if (word := text(node)) and any(
            char.isalnum() for char in word))
        tiles.append((tuple(box), label))
    counts = Counter(box for box, _ in tiles)
    return sorted((tile for tile in tiles if counts[tile[0]] == 1), key=lambda item: item[0])


def capture_thumbnails(device, xml, tiles, viewport, stop):
    if not tiles:
        return 'no_visible_tiles', []
    try:
        device.ensure_foreground()
        if stop.is_set():
            return 'cancelled', []
        data = read_gallery_screenshot(device)
        device.ensure_foreground()
        if stop.is_set():
            return 'cancelled', []
        if device.snapshot() != xml:
            return 'screen_changed', []
        pictures = []
        with Image.open(io.BytesIO(data)) as frame:
            if frame.format != 'PNG' or frame.width * frame.height > 20_000_000:
                return 'unsupported_image', []
            if list(viewport) != [0, 0, frame.width, frame.height]:
                return 'dimension_mismatch', []
            for box, label in tiles:
                if stop.is_set():
                    return 'cancelled', []
                x, y, right, bottom = box
                if not (0 <= x < right <= frame.width and 0 <= y < bottom <= frame.height):
                    continue
                cropped = frame.crop(box).convert('RGB')
                if max(ImageStat.Stat(cropped.resize((32, 32))).stddev) < 4:
                    continue
                buffer = io.BytesIO()
                Image.frombytes('RGB', cropped.size, cropped.tobytes()).save(buffer, format='PNG')
                pictures.append((box, label, buffer.getvalue()))
        return ('saved' if pictures else 'no_rendered_tiles'), pictures
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired, ParseError, Image.DecompressionBombError):
        return 'capture_unavailable', []


def write_thumbnails(path, result, report_name, pictures):
    with ZipFile(path, 'x') as archive:
        manifest = {'source_method': 'android_sight_ai_display_crops', 'name': result['name'],
                    'captured_at': result['captured_at'], 'raw_hash': result['raw_hash'],
                    'report_file': report_name, 'identity_verification': result['identity_verification'],
                    'original_files': False, 'complete': False, 'thumbnails': []}
        for index, (box, label, data) in enumerate(pictures, start=1):
            entry = f'thumbnails/{index:02d}.png'
            archive.writestr(entry, data)
            manifest['thumbnails'].append({'file': entry, 'label': label, 'crop_bounds': list(box),
                                           'sha256': hashlib.sha256(data).hexdigest()})
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
