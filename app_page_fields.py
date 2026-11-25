"""Extract operator-selected text labels from public Android app pages."""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from core import bounds
from mobile_scoped_store import MOBILE_REVIEW_RUN_ID
from sight_guide import app_nodes
from workbench_result_files import open_output_file


SOURCE = 'android_selected_page_fields'
SCREEN_FILE = re.compile(r'[0-9]{5,6}\.xml')
XML_LIMIT = 8 * 1024 * 1024


class UnmatchedTitle(ValueError):
    pass


def validate_labels(labels):
    if (not isinstance(labels, list) or not 1 <= len(labels) <= 30
            or any(not isinstance(label, str) or not 1 <= len(label.strip()) <= 60
                   or any(char in label for char in '\r\n：:') for label in labels)):
        raise ValueError('请逐行输入 1 至 30 个可见标签，标签本身无需冒号')
    cleaned = [label.strip() for label in labels]
    if len(set(cleaned)) != len(cleaned):
        raise ValueError('请删除重复的页面标签')
    return cleaned


def page_fields(xml, title, labels):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError('原始页面 XML 无法解析') from exc
    candidates = []
    for window in root:
        left, top, right, bottom = bounds(window)
        texts = {}
        for node in app_nodes(window):
            if (node.get('password') == 'true' or node.get('class') == 'android.widget.EditText'
                    or node.get('visible-to-user') == 'false'):
                continue
            value = node.get('text', '').replace('\ufffc', '').strip()
            x1, y1, x2, y2 = bounds(node)
            if (value and any(char.isalnum() for char in value)
                    and left <= x1 < x2 <= right and top <= y1 < y2 <= bottom):
                texts[node] = value
        exact_matches = sum(value == title for value in texts.values())
        if exact_matches:
            candidates.append((texts, exact_matches))
    if len(candidates) != 1 or candidates[0][1] != 1:
        raise UnmatchedTitle('找不到唯一的页面名称，请确认名称与当前公开页面完全一致')
    texts = candidates[0][0]
    parents = {child: parent for parent in root.iter() for child in parent if child in texts}
    fields = []
    for label in labels:
        pattern = re.compile(re.escape(label) + r'\s*[:：]\s*(\S.*)', re.S)
        values = []
        for node, word in texts.items():
            if match := pattern.fullmatch(word):
                values.append(match[1].strip())
            elif word.rstrip('：:').strip() == label and (parent := parents.get(node)) is not None:
                siblings = [child for child in parent.iter('node') if child in texts]
                # Only use a separate value when the immediate container has exactly two visible texts.
                if len(siblings) == 2 and siblings[0] is node:
                    value = texts[siblings[1]]
                    if value.rstrip('：:').strip() not in labels:
                        values.append(value)
        values = list(dict.fromkeys(values))
        fields.append({'label': label, 'values': values,
                       'status': 'not_found' if not values else 'matched' if len(values) == 1 else 'multiple'})
    return {'raw_hash': hashlib.sha256(xml.encode('utf-8')).hexdigest(), 'fields': fields}


def raw_run_folder(output, run):
    root = Path(output).resolve() / 'raw'
    path = root / run
    if not (path.is_dir() and path.resolve() == path and not (path.is_junction() or root.is_junction())):
        raise ValueError('当前目录没有这个原始页面任务')
    return path


def list_raw_page_runs(output):
    raw = Path(output).resolve() / 'raw'
    if not raw.is_dir() or raw.is_symlink() or raw.is_junction():
        return []
    result = []
    for entry in raw.iterdir():
        if MOBILE_REVIEW_RUN_ID.fullmatch(entry.name) and entry.is_dir() and not (entry.is_junction() or entry.is_symlink()):
            result.append({'name': entry.name, 'modified': entry.stat().st_mtime})
    return sorted(result, key=lambda row: (row['modified'], row['name']), reverse=True)[:100]


def collect_page_fields(output, title, labels, run, stop, emit, *, device=None):
    screens, skipped = [], []
    if run:
        archive = raw_run_folder(output, run)
        files = sorted((entry for entry in archive.iterdir() if SCREEN_FILE.fullmatch(entry.name)),
                       key=lambda path: int(path.stem))
        if not files:
            raise ValueError('所选任务没有已保存的原始页面')
        for index, path in enumerate(files, 1):
            if stop.is_set():
                break
            number = int(path.stem) + 1
            try:
                with open_output_file(archive, path) as stream:
                    raw = stream.read(XML_LIMIT + 1)
                if len(raw) > XML_LIMIT:
                    raise ValueError('页面文件超过大小限制')
                result = page_fields(raw.decode('utf-8-sig'), title, labels)
            except UnmatchedTitle:
                skipped.append({'screen': number, 'reason': 'title_mismatch'})
            except (OSError, ValueError):
                skipped.append({'screen': number, 'reason': 'unreadable_screen'})
            else:
                screens.append({'screen': number, **result})
            if index % 25 == 0:
                emit(f'已复查 {index} / {len(files)} 屏')
        total = len(files)
    else:
        if stop.is_set():
            return {'reason': 'cancelled', 'complete': False}
        if device is None:
            raise ValueError('需要配置 Android 设备')
        device.ensure_foreground()
        observed_at = datetime.now(timezone.utc).isoformat()
        xml = device.snapshot()
        device.ensure_foreground()
        if stop.is_set():
            return {'reason': 'cancelled', 'complete': False}
        screens.append({'screen': 1, 'observed_at': observed_at, **page_fields(xml, title, labels)})
        total = 1
    result = {'source_method': SOURCE, 'name': title, 'input_source': 'saved_android_pages' if run else 'live_android',
              'archive_run': run or None, 'field_labels': labels, 'total_screens': total,
              'matched_screens': len(screens), 'skipped_screens': skipped, 'screens': screens,
              'reason': 'cancelled' if stop.is_set() else 'read', 'complete': False,
              'processed_at': datetime.now(timezone.utc).isoformat(),
              'identity_verification': 'unique_visible_text_match'}
    folder = Path(output).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'app-page-fields-{uuid.uuid4().hex}.json'
    temporary = path.with_suffix('.part')
    result['report_file'] = str(path)
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    emit(f'已核对 {len(screens)} 屏，跳过 {len(skipped)} 屏')
    return result
