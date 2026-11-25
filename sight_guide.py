"""Read the current screen of Ctrip's App sight guide."""
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from sight_snapshot import APP, unique_value


SECTION = 'gs_sightmoredetail_view_'
PERIOD = re.compile(r'全年|[0-9]{1,2}月[0-9]{1,2}日.*[0-9]{1,2}月[0-9]{1,2}日')
HOURS = re.compile(r'\d{1,2}:\d{2}\s*[-–—~至]\s*\d{1,2}:\d{2}')


def app_nodes(branch):
    if branch.tag == 'node' and branch.get('package') == APP:
        yield branch
        for child in branch:
            yield from app_nodes(child)


def text(node):
    return node.get('text', '').strip()


def section_nodes(guide, parents, name):
    anchors = [node for node in app_nodes(guide) if node.get('resource-id') == SECTION + name]
    if len(anchors) != 1:
        return []
    anchor = anchors[0]
    siblings = list(parents[anchor])
    nodes = list(app_nodes(anchor))
    # Android exposes some section text as siblings of the empty section marker.
    for branch in siblings[siblings.index(anchor) + 1:]:
        if branch.get('resource-id', '').startswith(SECTION):
            break
        nodes.extend(app_nodes(branch))
    return nodes


def parse_sight_guide(xml, expected_name):
    root = ET.fromstring(xml)
    parents = {child: node for node in root.iter() for child in node}
    nodes = [node for window in root for node in app_nodes(window)]
    navigation = [node for node in nodes if node.get('resource-id') == 'gs_sightmoredetail_navlist']
    first_tabs = [node for node in nodes if node.get('content-desc') == '图片/视频'
                  and (parent := parents.get(node)) is not None and parent.get('package') == APP
                  and any(tab.get('content-desc') == '必看指南' and tab.get('package') == APP for tab in parent)]
    if len(navigation) != 1 or len(first_tabs) != 1:
        raise ValueError('未找到景点「必看指南」，请打开该标签后再试')
    first = first_tabs[0]
    tabs_parent = parents[first]
    if navigation[0] not in tabs_parent.iter():
        raise ValueError('无法确认「必看指南」属于当前景点页面')
    toolbar_branches = list(tabs_parent)[:list(tabs_parent).index(first)]
    toolbar = [word for branch in toolbar_branches for node in app_nodes(branch)
               if (word := text(node)) and any(char.isalnum() for char in word)]
    if not expected_name or toolbar != [expected_name]:
        raise ValueError('景点指南标题与所填名称不匹配')

    guide = parents[navigation[0]]
    opening = section_nodes(guide, parents, 'opentime')
    ticket = section_nodes(guide, parents, 'ticket')
    open_words = list(dict.fromkeys(text(node) for node in opening if text(node)))
    ticket_words = list(dict.fromkeys(text(node) for node in ticket if text(node)))
    periods = []
    for container in opening:
        words = [text(node) for node in container if node.get('package') == APP and text(node)]
        if len(words) == 2 and PERIOD.fullmatch(words[0]) and HOURS.search(words[1]):
            item = {'period_text': words[0], 'hours_text': words[1]}
            if item not in periods:
                periods.append(item)
    paired_hours = {item['hours_text'] for item in periods}
    duration = unique_value(word for word in open_words if word.startswith('建议时长'))
    status = unique_value(word for word in open_words
                          if re.match(r'^(今日|今天|明日|明天).*(开放|闭园|关闭|休园)', word))
    other_hours = [word for word in open_words if word not in paired_hours and word != status and HOURS.search(word)]
    notes = [word for word in open_words if '公示' in word or '当地时间' in word]
    prices = [word for word in ticket_words if re.fullmatch(r'门票\s*[¥￥]\s*[0-9]+(?:\.[0-9]{1,2})?\s*起?', word)]
    booking_notes = [word for word in ticket_words if word.startswith(('补充说明：', '补充说明:'))]
    if not any((duration, status, periods, other_hours, notes, prices, booking_notes)):
        raise ValueError('当前屏未读到开放时间或门票信息，请滚到「必看指南」上方再试')
    return {
        'name': expected_name, 'source_method': 'android_sight_guide_snapshot',
        'identity_verification': 'operator_name_exact_match', 'complete': False,
        'raw_hash': hashlib.sha256(xml.encode('utf-8')).hexdigest(),
        'visit_duration_text': duration, 'opening_status_text': status, 'opening_periods': periods,
        'other_opening_hours_texts': other_hours, 'opening_notes': notes,
        'ticket_price_texts': prices, 'booking_notes': booking_notes,
        'limitations': ['仅记录当前屏的 App 展示；未读预约库存和其他日期详情。',
                        '今日、明日等保留原文；票价与开放状态以采集时的页面展示为准。'],
    }


def capture_sight_guide(device, output, name):
    device.ensure_foreground()
    captured_at = datetime.now(timezone(timedelta(hours=8))).isoformat()
    xml = device.snapshot()
    device.ensure_foreground()
    result = parse_sight_guide(xml, name)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / ('sight-guide-' + uuid.uuid4().hex + '.json')
    result.update(captured_at=captured_at, file=str(path.resolve()))
    staged = path.with_suffix('.part')
    staged.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    staged.replace(path)
    return result
