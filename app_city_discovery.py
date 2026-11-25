"""Read visible city recommendations from the observed Android search surface."""
import hashlib
import json
import logging
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
APP = 'ctrip.android.view'
SOURCE = 'android_city_mustsee_snapshot'
SECTION = 'gs_searchlist_view_mustsees'


def parse_city_recommendations(xml, expected_city):
    logger.info('开始核对手机城市推荐页')
    root = ET.fromstring(xml)
    parents = {child: parent for parent in root.iter() for child in parent}
    nodes = [node for node in root.iter('node') if node.get('package') == APP]
    sections = [node for node in nodes if node.get('resource-id') == SECTION]
    markers = [node for node in nodes if node.get('resource-id') == 'gs_searchlist_view_changecity']
    if len(sections) != 1 or len(markers) != 1:
        raise ValueError('未识别到城市搜索页的必看推荐区域')
    section, marker = sections[0], markers[0]
    container = parents.get(marker)
    if container is None or container.get('package') != APP or parents.get(section) is not container:
        raise ValueError('城市与推荐区域不在同一页面容器')
    children = list(container)
    marker_index, section_index = children.index(marker), children.index(section)
    search_children = children[marker_index + 1:section_index]
    city_labels = [node.get('content-desc', '').strip() for node in search_children
                   if node.get('package') == APP and node.get('class') == 'android.widget.Button'
                   and node.get('content-desc', '').strip()]
    if city_labels != [expected_city] or not any(node.get('package') == APP and node.get('text') == '必看推荐'
                                                 for node in search_children):
        raise ValueError('城市标签与填写的城市不一致，或必看推荐标题不可见')
    objects = []
    skipped = 0
    # The observed tracking marker is an empty sibling of the actual carousel.
    carousel = section
    if not len(section):
        following = children[section_index + 1:section_index + 2]
        if not following or (scroll := following[0]).get('package') != APP or (
                scroll.get('class') != 'android.widget.HorizontalScrollView'
                or not section.get('bounds') or scroll.get('bounds') != section.get('bounds')):
            raise ValueError('无法确认必看推荐卡片所在区域')
        carousel = scroll
    entries = [node for node in carousel.iter('node') if node.get('package') == APP
               and node.get('resource-id') == SECTION + '_item']
    for index, card in enumerate(entries, start=1):
        name = card.get('content-desc', '').strip()
        texts = [node.get('text', '').strip() for node in card.iter('node') if node.get('package') == APP]
        if not name or len(name) > 100 or name not in texts:
            skipped += 1
            continue
        objects.append({'kind': 'sight', 'name': name, 'visible_order': index,
                        'nearby_label_visible': '周边' in texts, 'identity_status': 'name_only_no_object_id'})
    logger.info('手机城市推荐核对完成：读到%s条，跳过%s条', len(objects), skipped)
    return {'source_method': SOURCE, 'city': expected_city, 'section': '必看推荐',
            'objects': objects, 'skipped_cards': skipped,
            'reason': 'recommendations_partial' if skipped or not objects else 'recommendations_read',
            'raw_hash': hashlib.sha256(xml.encode('utf-8')).hexdigest(),
            'complete': False, 'limitations': ['仅当前推荐区域；周边条目可能不在该城市。',
                                              '卡片仅有显示名称，未核验对象编号、链接或完整目录。']}


def capture_city_recommendations(device, output, city, stop):
    logger.info('开始读取手机当前城市推荐')
    device.ensure_foreground()
    timestamp = datetime.now(timezone.utc).isoformat()
    xml = device.snapshot()
    device.ensure_foreground()
    if stop.is_set():
        raise InterruptedError('城市推荐读取已取消')
    report = parse_city_recommendations(xml, city)
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'app-city-{uuid.uuid4().hex}.json'
    report.update(captured_at=timestamp, report_file=str(path.resolve()))
    temp = path.with_suffix('.part')
    try:
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if stop.is_set():
            raise InterruptedError('城市推荐读取已取消')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    logger.info('手机城市推荐报告保存完成')
    return report
