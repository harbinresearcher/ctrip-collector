"""Extract public fields from the visible Android shopping detail page."""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from sight_guide import app_nodes, text
from sight_snapshot import APP, unique_value, visible_words


SOURCE = 'android_shopping_detail_snapshot'
HERO = 'shopPOI_headPictureModule_exp'
RATING = 'shopPOI_moreReviewsExp'
BASIC = 'shopPOI_moreBasicInfoExp'
MAP = 'shopPOI_mapExp'
COUPONS = 'C_GO_POI_module_exposure'
OPENING_STATUS = ('营业中', '休息中', '暂停营业', '暂未营业', '暂停开放', '已打烊', '歇业中')
CLOCK = r'(?:[01]?[0-9]|2[0-4]):[0-5][0-9]'


def marked_child(parent, marker):
    return unique_value(node for node in parent if node.get('package') == APP and node.get('resource-id') == marker)


def coupon_titles(content):
    sections = [branch for branch in content if marked_child(branch, COUPONS) is not None
                and any(text(node) == '优惠卡券' for node in branch)]
    if len(sections) != 1:
        return []
    section = sections[0]
    parents = {child: parent for parent in app_nodes(section) for child in parent}
    titles = []
    for control in app_nodes(section):
        if text(control) not in {'免费领取', '立即购买', '立即抢购', '立即领取'}:
            continue
        actions = parents.get(control)
        row = parents.get(actions)
        if row is None:
            continue
        labels = [word for branch in row if branch is not actions for word in visible_words(branch) if len(word) >= 3]
        if labels:
            titles.append(labels[0])
    return list(dict.fromkeys(titles))


def parse_shopping_detail(xml, expected_name):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError('购物详情页面读取失败') from exc
    nodes = [node for window in root for node in app_nodes(window)]
    parents = {child: parent for parent in nodes for child in parent}
    candidates = []
    for scroll in nodes:
        if scroll.get('class') != 'android.widget.ScrollView':
            continue
        root_boxes = [child for child in scroll if child.get('package') == APP]
        if len(root_boxes) != 1:
            continue
        content = root_boxes[0]
        markers = [n for n in app_nodes(content) if n.get('resource-id') == HERO]
        if len(markers) != 1:
            continue
        headers = [node for node in app_nodes(content) if marked_child(node, BASIC) is not None
                   and (marked_child(node, RATING) is not None or marked_child(node, MAP) is not None)]
        if len(headers) == 1 and parents.get(headers[0]) is not None:
            candidates.append((content, headers[0]))
    if len(candidates) != 1:
        raise ValueError('请打开购物对象的详情顶部')
    content, header = candidates[0]
    direct = [text(node) for node in header if node.get('package') == APP and text(node)
              and any(char.isalnum() for char in text(node))]
    if not expected_name or not direct or direct[0] != expected_name:
        raise ValueError('购物详情名称与任务名称不一致，或名称不可见')
    result = {'kind': 'shopping', 'mode': 'app_shopping_detail',
              'source': 'ctrip_android_ui', 'source_method': SOURCE, 'name': expected_name,
              'identity_verification': 'operator_name_exact_match',
              'raw_hash': hashlib.sha256(xml.encode('utf-8')).hexdigest(),
              'status': 'partial', 'complete': False, 'category_labels': [],
              'badges': [], 'offers': coupon_titles(content)}
    if english := unique_value(s for s in direct[1:] if re.fullmatch(r'[A-Za-z][A-Za-z0-9 .&()/+’\x27-]{2,100}', s)):
        result['english_name'] = english
    if tagline := unique_value(s.lstrip('·• ').strip() for s in direct[1:] if s.startswith(('· ', '• '))):
        result['tagline_display'] = tagline
    result['badges'] = list(dict.fromkeys(label for branch in header for label in visible_words(branch)
                                         if label in {'优享购'}))

    rating = marked_child(header, RATING)
    if rating is not None:
        labels = visible_words(rating)
        result['review_entry_visible'] = True
        if score := unique_value(value for value in labels if re.fullmatch(r'[0-4]\.[0-9]|5\.0', value)):
            result['score_display'] = score
        if count := unique_value(value for value in labels
                                 if re.fullmatch(r'[0-9][0-9,]*(?:\.[0-9]+)?[万千wW]?\+?条点评', value)):
            result['review_count_display'] = count
    basic = unique_value(visible_words(marked_child(header, BASIC)))
    if basic:
        result['basic_info_display'] = basic
        opening, separator, categories = basic.replace('｜', '|').partition('|')
        if status := next((label for label in OPENING_STATUS if opening.startswith(label)), None):
            result['opening_status_display'] = status
        if hours := re.search(rf'{CLOCK}\s*[-–—~至]\s*{CLOCK}(?:\s*开放)?', opening):
            result['opening_hours_display'] = hours.group()
        if separator:
            result['category_labels'] = [label.strip() for label in categories.split('·') if label.strip()]
    mapping = marked_child(header, MAP)
    if mapping is not None:
        labels = [value for value in visible_words(mapping) if value not in {'地图', '导航'}]
        if len(labels) == 1 or len(labels) == 2 and labels[1].startswith('距'):
            result['address_display'] = labels[0]
            if len(labels) == 2:
                result['nearby_display'] = labels[1]
    return result


def capture_shopping_detail(device, output, name):
    device.ensure_foreground()
    captured_at = datetime.now(timezone.utc).isoformat()
    xml = device.snapshot()
    device.ensure_foreground()
    report = parse_shopping_detail(xml, name)
    folder = Path(output).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'app-shopping-detail-{uuid.uuid4().hex}.json'
    report.update(file=str(path), captured_at=captured_at)
    staged = path.with_suffix('.part')
    try:
        staged.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)
    return report
