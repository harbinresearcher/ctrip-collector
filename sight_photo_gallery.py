"""Collect displayed main photos from a sight's public Android gallery."""
import hashlib
import io
import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from PIL import Image, ImageStat

from core import bounds
from gallery_capture import read_gallery_screenshot
from sight_ai import read_sight_ai
from sight_guide import app_nodes, text
from workbench_result_files import open_output_file


SOURCE = 'android_sight_photo_gallery'
GRID_TILE = 'o_cc_travelSleuthPicture_picture'
VIEWER_PHOTO = 'o_cc_picture_show'
MAX_REPORT_BYTES = 1_000_000


def read_checkpoint(folder, name, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id):
        raise ValueError('上次图集任务编号无效')
    stem = f'sight-photo-gallery-{run_id}'
    archive_name = stem + '.zip'
    try:
        with open_output_file(folder, folder / (stem + '.json')) as report:
            data = report.read(MAX_REPORT_BYTES + 1)
        if len(data) > MAX_REPORT_BYTES:
            raise ValueError('上次图集报告超过大小限制')
        previous = json.loads(data)
    except (OSError, ValueError) as exc:
        raise ValueError('无法读取输出目录中的上次图集报告') from exc
    if (not isinstance(previous, dict) or previous.get('kind') != 'sight'
            or previous.get('source') != 'ctrip_android_ui' or previous.get('source_method') != SOURCE
            or previous.get('run_id') != run_id or previous.get('archive') != archive_name):
        raise ValueError('上次图集报告缺少对应的照片压缩包')
    if previous.get('name') != name:
        raise ValueError('上次图集报告的景点名称与当前任务不一致')
    total = previous.get('gallery_total')
    pages = previous.get('pages')
    entry_hash = previous.get('entry_raw_hash')
    if (type(total) is not int or total < 1 or not isinstance(pages, list) or not 1 <= len(pages) <= 500
            or previous.get('page_count') != len(pages) or not isinstance(entry_hash, str)
            or not re.fullmatch(r'[a-f0-9]{64}', entry_hash)):
        raise ValueError('上次图集报告缺少有效的页码记录')
    ordinals = [row.get('page') for row in pages if isinstance(row, dict)]
    if (len(ordinals) != len(pages) or any(type(page) is not int for page in ordinals)
            or not 1 <= ordinals[0] <= ordinals[-1] <= total
            or ordinals != list(range(ordinals[0], ordinals[0] + len(pages)))
            or previous.get('first_page', ordinals[0]) != ordinals[0]
            or previous.get('last_page', ordinals[-1]) != ordinals[-1]):
        raise ValueError('上次图集报告的页码不连续')
    last = ordinals[-1]
    if last == total:
        raise ValueError('上次采集已到达图集最后一页')
    digest = pages[-1].get('sha256')
    if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('上次图集报告缺少末张图片的校验值')
    member = f'photos/{digest}.png'
    if pages[-1].get('file') != member:
        raise ValueError('上次图集报告的末张图片记录无效')
    try:
        with open_output_file(folder, folder / archive_name) as bundle, ZipFile(bundle) as archive:
            if archive.getinfo('manifest.json').file_size > MAX_REPORT_BYTES:
                raise ValueError('图集清单过大')
            manifest = json.loads(archive.read('manifest.json'))
            if manifest != {key: value for key, value in previous.items() if key != 'file'}:
                raise ValueError('图集清单与报告不一致')
            photo = archive.getinfo(member)
            if not 0 < photo.file_size <= 64 * 1024 * 1024:
                raise ValueError('末张图片大小无效')
            with archive.open(photo) as image:
                if hashlib.file_digest(image, 'sha256').hexdigest() != digest:
                    raise ValueError('末张图片校验失败')
    except (OSError, ValueError, KeyError, RuntimeError, BadZipFile) as exc:
        raise ValueError('上次图集的 ZIP 与报告无法核对') from exc
    return total, last, digest, entry_hash


def contains(outer, inner):
    left, top, right, bottom = inner
    return outer[0] <= left < right <= outer[2] and outer[1] <= top < bottom <= outer[3]


def class_is(node, name):
    return node.get('class') == 'android.widget.' + name


def app_window(xml):
    windows = [list(app_nodes(n)) for n in ET.fromstring(xml)]
    visible = [nodes for nodes in windows if nodes and bounds(nodes[0])[:2] == [0, 0]]
    if len(visible) != 1:
        raise ValueError('无法确认景点图集窗口')
    return visible[0], bounds(visible[0][0])


def entry_box(xml, name):
    summary, _, viewport, _ = read_sight_ai(xml, name)
    total = summary['ai_selected_images'].get('display_count')
    if not total:
        raise ValueError('请在该景点「用户点评」显示 AI 晒图入口及照片总数')
    nodes = [n for window in ET.fromstring(xml) for n in app_nodes(window)]
    boxes = [bounds(n) for n in nodes if text(n) == f'共{total}张' and contains(viewport, bounds(n))]
    if len(boxes) != 1:
        raise ValueError('无法确认景点晒图入口的位置')
    return total, boxes[0], summary['raw_hash']


def first_grid_box(xml):
    nodes, viewport = app_window(xml)
    titles = [n for n in nodes if text(n) == '用户实拍照片' and class_is(n, 'TextView')]
    filters = [n for n in nodes if text(n) == '全部' and class_is(n, 'TextView')]
    if len(titles) != 1 or len(filters) != 1 or not contains(viewport, bounds(titles[0])):
        raise ValueError('未进入「用户实拍照片」网格顶部')
    scrollers = [n for n in nodes if class_is(n, 'ScrollView') and filters[0] in app_nodes(n)]
    if len(scrollers) != 1 or not contains(bounds(scrollers[0]), bounds(filters[0])):
        raise ValueError('未识别到照片网格的「全部」入口')
    candidates = [n for n in app_nodes(scrollers[0]) if n.get('resource-id') == GRID_TILE]
    tiles = [bounds(n) for n in candidates
             if contains(bounds(scrollers[0]), bounds(n)) and bounds(filters[0])[3] <= bounds(n)[1]
             and any(class_is(child, 'ImageView') and child.get('resource-id') == 'image'
                     and bounds(child) == bounds(n) for child in app_nodes(n) if child is not n)]
    if not tiles:
        raise ValueError('照片网格中没有可核验的完整照片')
    return min(tiles, key=lambda box: (box[1], box[0]))


def read_viewer(xml, expected_total):
    nodes, viewport = app_window(xml)
    photos = [n for n in nodes if n.get('resource-id') == VIEWER_PHOTO]
    if len(photos) != 1:
        raise ValueError('未识别到景点图集主图')
    photo = photos[0]
    crop = bounds(photo)
    scrollers = [n for n in nodes if class_is(n, 'HorizontalScrollView') and photo in app_nodes(n)]
    row = next(n for n in nodes if photo in n)
    siblings = list(row)
    position = siblings.index(photo)
    # The accessibility marker precedes its image in the photo row.
    images = [n for n in siblings[position + 1:] if n.get('package') == photo.get('package')
              and class_is(n, 'ImageView') and n.get('resource-id') == 'image' and bounds(n) == crop]
    if (len(scrollers) != 1 or not contains(bounds(scrollers[0]), crop) or not contains(viewport, crop)
            or len(images) != 1):
        raise ValueError('主图位置与照片浏览区域不匹配')
    positions = []
    for n in nodes:
        for current, total in zip(n, list(n)[1:]):
            if not (class_is(current, 'TextView') and class_is(total, 'TextView')
                    and re.fullmatch(r'[1-9][0-9]{0,8}', text(current))
                    and text(total) == f'/{expected_total}'):
                continue
            left = bounds(current)
            right = bounds(total)
            if (contains(viewport, left) and contains(viewport, right)
                    and crop[3] < min(left[1], right[1]) and left[2] == right[0]
                    and max(left[1], right[1]) < min(left[3], right[3])):
                positions.append(int(text(current)))
    if len(positions) != 1 or positions[0] > expected_total:
        raise ValueError('图集页码或照片总数不明确')
    return positions[0], tuple(crop), (viewport[2], viewport[3])


def tap(device, box):
    left, top, right, bottom = box
    device.ensure_foreground()
    device.run('shell', 'input', 'tap', str((left + right) // 2), str((top + bottom) // 2))


def stable_crop(device, stop, total, state):
    _, crop, size = state
    frames = []
    for delay in (0, 0.35):
        if stop.wait(delay):
            raise InterruptedError
        device.ensure_foreground()
        with Image.open(io.BytesIO(read_gallery_screenshot(device))) as frame:
            if frame.format != 'PNG' or frame.size != size or frame.width * frame.height > 20_000_000:
                raise ValueError('截图尺寸与图集窗口不一致')
            frames.append(frame.crop(crop).convert('RGB'))
    device.ensure_foreground()
    if read_viewer(device.snapshot(), total) != state:
        raise ValueError('截图前后图集页码或主图位置变化')
    first, second = frames
    if (first.tobytes() != second.tobytes()
            or max(ImageStat.Stat(second.resize((32, 32))).stddev) < 4):
        return None
    payload = io.BytesIO()
    Image.frombytes('RGB', second.size, second.tobytes()).save(payload, format='PNG')
    return payload.getvalue()


def read_stable_photo(device, stop, total, state):
    payload = stable_crop(device, stop, total, state)
    if payload is None:
        payload = stable_crop(device, stop, total, state)
    return payload


def swipe_next(device, state, stop):
    if stop.is_set():
        raise InterruptedError('图集采集已取消')
    device.ensure_foreground()
    _, crop, (width, _) = state
    y = str((crop[1] + crop[3]) // 2)
    device.run('shell', 'input', 'swipe', str(width * 4 // 5), y, str(width // 5), y, '330')


def open_first_photo(device, name, stop):
    device.ensure_foreground()
    total, entry, entry_hash = entry_box(device.snapshot(), name)
    if stop.is_set():
        raise InterruptedError('图集采集已取消')
    tap(device, entry)
    for attempt in range(10):
        if stop.wait(0.9):
            raise InterruptedError('图集采集已取消')
        device.ensure_foreground()
        try:
            first = first_grid_box(device.snapshot())
            break
        except (ValueError, ET.ParseError):
            if attempt == 9:
                raise
    if stop.is_set():
        raise InterruptedError('图集采集已取消')
    tap(device, first)
    return total, entry_hash


def resume_next_photo(device, folder, name, run_id, stop):
    total, page, digest, entry_hash = read_checkpoint(folder, name, run_id)
    device.ensure_foreground()
    try:
        state = read_viewer(device.snapshot(), total)
    except (ValueError, ET.ParseError) as exc:
        raise ValueError(f'请在同一景点图集打开上次保存的第 {page} 张大图') from exc
    if state[0] != page:
        raise ValueError(f'请打开上次保存的第 {page} 张大图，当前是第 {state[0]} 张')
    payload = read_stable_photo(device, stop, total, state)
    if payload is None or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError('当前图片与上次保存的图片不一致，请核对景点、页码和显示设置')
    swipe_next(device, state, stop)
    return total, page + 1, entry_hash


def wait_for_page(device, stop, total, index):
    attempts = 10 if index == 1 else 5
    for attempt in range(attempts):
        if stop.wait(0.85):
            raise InterruptedError('图集采集已取消')
        device.ensure_foreground()
        try:
            state = read_viewer(device.snapshot(), total)
        except (ValueError, ET.ParseError):
            if attempt == attempts - 1:
                raise
            continue
        if state[0] == index:
            return state
        if state[0] != index - 1:
            break
    raise ValueError('图集页码未按顺序前进')


def capture_sight_photo_gallery(device, output, name, stop, *, max_pages, resume_run=None):
    if type(max_pages) is not int or not 1 <= max_pages <= 500:
        raise ValueError('景点图集页数上限须为 1 至 500')
    if stop.is_set():
        return {'reason': 'cancelled', 'complete': False}
    folder = Path(output).resolve()
    try:
        if resume_run is None:
            total, entry_hash = open_first_photo(device, name, stop)
            first_page = 1
        else:
            total, first_page, entry_hash = resume_next_photo(device, folder, name, resume_run, stop)
    except InterruptedError:
        return {'reason': 'cancelled', 'complete': False}
    end_page = min(total, first_page + max_pages - 1)

    run_id = uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=True)
    report = folder / f'sight-photo-gallery-{run_id}.json'
    archive = report.with_suffix('.zip')
    staged_archive = archive.with_suffix('.zip.part')
    staged_report = report.with_suffix('.json.part')
    pages = []
    digests = set()
    reason = 'gallery_end_reached' if end_page == total else 'page_budget_reached'
    error = ''
    archive_written = report_written = False
    try:
        with ZipFile(staged_archive, 'x') as zip_file:
            for index in range(first_page, end_page + 1):
                try:
                    state = wait_for_page(device, stop, total, index)
                    payload = read_stable_photo(device, stop, total, state)
                    if stop.is_set():
                        raise InterruptedError
                except InterruptedError:
                    reason = 'cancelled'
                    break
                except (ValueError, ET.ParseError) as exc:
                    reason = 'screen_changed'
                    error = str(exc)
                    break
                except (RuntimeError, OSError, subprocess.TimeoutExpired):
                    reason = 'device_error'
                    break
                if payload is None:
                    reason = 'image_not_ready'
                    break
                _, crop, _ = state
                digest = hashlib.sha256(payload).hexdigest()
                asset = f'photos/{digest}.png'
                if digest not in digests:
                    zip_file.writestr(asset, payload)
                    digests.add(digest)
                pages.append({'page': index, 'file': asset, 'sha256': digest, 'crop_bounds': list(crop)})
                if index < end_page:
                    try:
                        swipe_next(device, state, stop)
                    except InterruptedError:
                        reason = 'cancelled'
                        break
                    except (RuntimeError, OSError, subprocess.TimeoutExpired):
                        reason = 'device_error'
                        break
            result = {'kind': 'sight', 'name': name, 'source': 'ctrip_android_ui', 'source_method': SOURCE,
                      'gallery_total': total, 'page_count': len(pages), 'unique_image_count': len(digests),
                      'reason': reason, 'error': error, 'complete': False, 'original_files': False,
                      'entry_raw_hash': entry_hash,
                      'identity_verification': ('matched_previous_sight_gallery_page' if resume_run
                                                else 'verified_sight_title_at_gallery_entry'),
                      'captured_at': datetime.now(timezone.utc).isoformat(), 'run_id': run_id, 'pages': pages,
                      'first_page': pages[0]['page'] if pages else None,
                      'last_page': pages[-1]['page'] if pages else None, 'previous_run_id': resume_run,
                      'archive': archive.name if pages else None, 'file': str(report)}
            if pages:
                zip_file.writestr('manifest.json', json.dumps({k: v for k, v in result.items() if k != 'file'},
                                                             ensure_ascii=False, indent=2))
        if pages:
            staged_archive.replace(archive)
            archive_written = True
        staged_report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        staged_report.replace(report)
        report_written = True
    finally:
        staged_archive.unlink(missing_ok=True)
        staged_report.unlink(missing_ok=True)
        if archive_written and not report_written:
            archive.unlink(missing_ok=True)
    return result
