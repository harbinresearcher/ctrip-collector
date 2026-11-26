"""Export the App's own saved images from one verified restaurant review."""
import hashlib
import json
import logging
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from core import parse_reviews
from mobile_scoped_store import MobileScopedStore
from restaurant_gallery_download import download_visible_photo
from restaurant_gallery_scene import read_photo_viewer, read_review_note
from runner import restaurant_review_screen


SOURCE = 'android_restaurant_review_gallery'
logger = logging.getLogger(__name__)


def snapshot(device):
    try:
        device.ensure_foreground()
        return device.snapshot()
    except Exception as exc:
        raise RuntimeError('无法读取前台的携程页面') from exc


def scoped_rows(xml, name):
    root = ET.fromstring(xml)
    title, scroll, _ = restaurant_review_screen(root)
    if title != name or scroll is None:
        raise ValueError('请打开名称与任务匹配的餐厅点评列表')
    return parse_reviews(ET.tostring(scroll, encoding='unicode'), 'restaurant', locate_media=True)


def active(stop):
    if stop.is_set():
        raise InterruptedError('餐厅点评大图采集已停止')


def tap(device, box, stop):
    device.ensure_foreground()
    active(stop)
    x, y, r, b = box
    device.run('shell', 'input', 'tap', str((x + r) // 2), str((y + b) // 2))


def wait_scene(device, stop, reader, page):
    detail = '没有读到要核对的页码'
    read_screen = False
    for attempt in range(7):
        if stop.wait(0 if attempt == 0 else .45):
            active(stop)
        try:
            xml = snapshot(device)
        except RuntimeError:
            continue
        read_screen = True
        try:
            state = reader(xml)
        except ValueError as exc:
            detail = str(exc)
            continue
        if state[0] == page:
            return state
        if state[0] > page:
            raise ValueError('大图页码跳过了目标页')
    if not read_screen:
        raise RuntimeError('打开点评大图期间无法读取前台页面')
    raise ValueError(detail)


def collect_pages(device, stop, name, review, entry_box, max_pages, archive, report, emit):
    reason = 'entry_unverified'
    try:
        tap(device, entry_box, stop)
        displayed = review.get('card_fields', {}).get('photo_count_display')
        _, total, photo_tap = wait_scene(
            device, stop, lambda xml: read_review_note(xml, name, review['author'], review['content'], displayed), 1,
        )
        tap(device, photo_tap, stop)
        report['gallery_total'] = total
        end = min(total, max_pages)
        assets = set()
        for index in range(1, end + 1):
            reason = 'viewer_changed'
            state = wait_scene(device, stop, lambda xml: read_photo_viewer(xml, total), index)
            _, _, photo, download = state

            def verify_page():
                active(stop)
                if read_photo_viewer(snapshot(device), total) != state:
                    raise ValueError('保存前后浏览器页码或位置发生变化')

            reason = 'download_unverified'
            payload, extension, dimensions = download_visible_photo(device, stop, download, verify_page)
            digest = hashlib.sha256(payload).hexdigest()
            asset = f'photos/{digest}.{extension}'
            if asset not in assets:
                archive.writestr(asset, payload)
                assets.add(asset)
            report['pages'].append({'page': index, 'file': asset, 'sha256': digest,
                                    'width': dimensions[0], 'height': dimensions[1]})
            emit(f'已保存点评大图 {index}/{total}')
            if index < end:
                reason = 'viewer_changed'
                verify_page()
                left, top, right, bottom = photo
                inset = (right - left) * 18 // 100
                y = top + (bottom - top) * 49 // 100
                # A brisk flick advances the pager even after the Download button has taken focus.
                active(stop)
                device.run('shell', 'input', 'swipe', str(right - inset), str(y),
                           str(left + inset), str(y), '150')
        return ('gallery_end_reached' if end == total else 'page_budget_reached'), ''
    except InterruptedError:
        return 'cancelled', ''
    except ValueError as exc:
        return reason, str(exc)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        logger.warning('点评大图任务在 %s 阶段中断', reason, exc_info=True)
        return 'device_error', 'Android 设备读取或保存图片时中断'


def capture_restaurant_review_gallery(device, output, name, object_id, stop, *, max_pages, emit):
    if type(max_pages) is not int or not 1 <= max_pages <= 50:
        raise ValueError('点评大图页数须为 1 至 50')
    if stop.is_set():
        return {'reason': 'cancelled', 'complete': False}
    choices = [row for row in scoped_rows(snapshot(device), name) if row.get('media_tile_bounds')]
    if not choices:
        raise ValueError('请让餐厅点评正文、日期和完整图片同时可见')
    review = min(choices, key=lambda row: (row['media_tile_bounds'][0][1], row['media_tile_bounds'][0][0]))
    entry_box = review['media_tile_bounds'][0]

    folder = Path(output).resolve()
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:6]
    basename = f'restaurant-review-gallery-{run_id}'
    report_path = folder / f'{basename}.json'
    archive_path = folder / f'{basename}.zip'
    temp_report = report_path.with_suffix('.json.part')
    temp_archive = archive_path.with_suffix('.zip.part')
    store = MobileScopedStore(folder, 'restaurant', object_id)
    try:
        selected_key = store.review_key(review)
        entry_xml = snapshot(device)
        matches = [row for row in scoped_rows(entry_xml, name) if store.review_key(row) == selected_key]
        if len(matches) != 1 or entry_box not in matches[0].get('media_tile_bounds', []):
            raise ValueError('点评或图片位置已变化，请在页面稳定后重试')
        if stop.is_set():
            return {'reason': 'cancelled', 'complete': False}
        review = matches[0]
        raw = folder / 'raw' / run_id / '00000.xml'
        raw.parent.mkdir(parents=True)
        raw.write_text(entry_xml, 'utf-8')
        store.start_run(run_id, name)
        observed = {}
        store.save(name, [review], raw.relative_to(folder).as_posix(), observed_keys=observed, run_id=run_id)
        sample_csv = store.export(run_id=run_id, observed_keys=observed)
        report = {'kind': 'restaurant', 'name': name, 'source': 'ctrip_android_ui', 'source_method': SOURCE,
                  'run_id': run_id, 'gallery_total': None, 'pages': [], 'file': str(report_path),
                  'sample_csv': str(sample_csv), 'sample_csv_count': 1, 'sample_order': 1,
                  'review_key': next(iter(observed)), 'object_id': store.object_id,
                  'identity_id_verified': False, 'entry_raw_hash': hashlib.sha256(entry_xml.encode('utf-8')).hexdigest(),
                  'original_files_verified': False, 'complete': False, 'acquisition': 'app_album_download'}
        archive_written = False
        try:
            with ZipFile(temp_archive, 'x') as archive:
                reason, error = collect_pages(device, stop, name, review, entry_box, max_pages, archive, report, emit)
                pages = report['pages']
                report.update(reason=reason, error=error, page_count=len(pages),
                              unique_image_count=len({page['sha256'] for page in pages}),
                              gallery_complete=reason == 'gallery_end_reached',
                              archive=archive_path.name if pages else None)
                if pages:
                    manifest = {key: value for key, value in report.items() if key != 'file'}
                    manifest['sample_csv'] = Path(sample_csv).name
                    archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            if pages:
                temp_archive.replace(archive_path)
                archive_written = True
            store.finish_run(run_id, reason)
            temp_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
            temp_report.replace(report_path)
        finally:
            temp_report.unlink(missing_ok=True)
            temp_archive.unlink(missing_ok=True)
            if archive_written and not report_path.exists():
                archive_path.unlink(missing_ok=True)
    finally:
        store.db.close()
    return report
