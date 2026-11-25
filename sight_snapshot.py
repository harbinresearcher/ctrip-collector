"""Extract visible sight facts without inferring inventory or object IDs."""
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)
APP = 'ctrip.android.view'
BOOKING = {'可订今日', '可订明日', '有条件退', '不可退', '随时退', '无需取票'}
TICKET_BADGES = {'好评最多', '销量最高'}


def visible_words(node):
    return [text for child in node.iter('node') if child.get('package') == APP
            and (text := child.get('text', '').strip()) and any(char.isalnum() for char in text)]


def unique_value(values):
    unique = list(dict.fromkeys(values))
    return unique[0] if len(unique) == 1 else None


def ticket_cards(nodes, parents):
    logger.info('开始提取景点页面可见票品卡片')
    cards = []
    seen = set()
    for action in nodes:
        if not action.get('resource-id', '').startswith('shelf-card-button-'):
            continue
        button = parents.get(action)
        card = parents.get(button)
        if card is None or button.get('package') != APP or card.get('package') != APP or card in seen:
            continue
        seen.add(card)
        # The title and action belong to the smallest card. Never borrow a price from its neighbours.
        arrows = [node for node in card.iter('node') if node.get('package') == APP
                  and node.get('resource-id', '').endswith('-标题箭头')]
        if len(arrows) != 1:
            continue
        title = unique_value(child.get('text', '').strip() for child in parents[arrows[0]]
                             if child.get('package') == APP and (child.get('text', '').strip())
                             and any(char.isalnum() for char in child.get('text', '')))
        if not title:
            continue
        prices = []
        for price in card.iter('node'):
            price_id = price.get('resource-id', '')
            value = price.get('text', '').strip()
            if price.get('package') != APP or not price_id.endswith('-price') or not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', value):
                continue
            container = parents.get(price)
            if container is None or container.get('package') != APP:
                continue
            parts = {child.get('resource-id', ''): child.get('text', '').strip()
                     for child in container if child.get('package') == APP}
            if 'shelf_card_price_container' not in parts:
                continue
            base = price_id.removesuffix('-price')
            currency, suffix = parts.get(base + '-currency'), parts.get(base + '-price-suffix', '')
            if currency in ('¥', '￥') and suffix in ('', '起'):
                prices.append(currency + value + suffix)
        words = visible_words(card)
        cards.append({'title': title, 'display_price': unique_value(prices),
                      'booking_conditions': list(dict.fromkeys(text for text in words if text in BOOKING)),
                      'badges': list(dict.fromkeys(text for text in words if text in TICKET_BADGES)),
                      'sold_label': unique_value(text for text in words if re.fullmatch(r'已售\s*[0-9]+(?:\.[0-9]+)?[万千]?\+?', text))})
    logger.info('景点票品卡片提取完成：%s张', len(cards))
    return cards


def sight_header(nodes, parents, title_node):
    header = parents.get(title_node)
    if header is None or header.get('package') != APP:
        return {}
    fields = {}
    scores = []
    for node in header.iter('node'):
        count = node.get('text', '').strip()
        if node.get('package') != APP or not re.fullmatch(r'[0-9０-９,.]+[万千]?\+?条点评', count):
            continue
        container = parents.get(node)
        if container is None or container.get('package') != APP:
            continue
        labels = [child.get('text', '').strip() for child in container if child.get('package') == APP]
        score = unique_value(text for text in labels if re.fullmatch(r'[0-5](?:\.[0-9])?', text) and float(text) <= 5)
        if score and '/5' in labels:
            scores.append({'score_text': score, 'scale_text': '/5', 'count_text': count})
    if len(scores) == 1:
        fields['review_display'] = scores[0]
    rank = [text for node in header.iter('node') if node.get('package') == APP
            and node.get('resource-id') == 'c_spotdetail_ranking_view' for text in visible_words(node)]
    if rank:
        fields['ranking_labels'] = list(dict.fromkeys(rank))
    surrounding = parents.get(header)
    if surrounding is not None and surrounding.get('package') == APP:
        parent_sight = unique_value(text.split('：', 1)[1].strip() for node in surrounding
                                   if node.get('resource-id') == 'gs_spotdetail_top_father_tips_expo'
                                   for text in visible_words(node) if text.startswith('所属景区：'))
        if parent_sight:
            fields['parent_sight_name'] = parent_sight
    addresses = []
    for node in header.iter('node'):
        if node.get('package') == APP and node.get('resource-id') == 'gs_spotdetail_view_map_button':
            container = parents.get(node)
            if container is not None and container.get('package') == APP:
                addresses.extend(text for sibling in container if sibling is not node
                                 and sibling.get('package') == APP for text in visible_words(sibling))
    if address := unique_value(addresses):
        fields['address_text'] = address
    return fields


def parse_sight_snapshot(xml, expected_name):
    # /* ===== 步骤1：核对页面；数据源：携程UI树；操作：1) 限定应用 2) 验证名称和详情标签 ===== */
    logger.info('开始核对景点详情')
    # 1.1 不读取其他应用节点；名称为操作者提供，不推断业务ID。
    root = ET.fromstring(xml)
    nodes = [n for n in root.iter('node') if n.get('package') == APP]
    parents = {child: parent for parent in root.iter() for child in parent}
    texts = [n.get('text', '').strip() for n in nodes if n.get('text', '').strip()]
    titles = [node for node in nodes if node.get('resource-id') == 'gs_spotdetail_view_name']
    title_node = titles[0] if len(titles) == 1 else None
    names = [node.get('text', '').strip() for node in title_node
             if node.get('package') == APP and node.get('text', '').strip()] if title_node is not None else []
    valid_name = names == [expected_name] if titles else expected_name in texts
    if not expected_name or not valid_name or not {'点评/笔记', '须知', '预订'}.issubset(texts):
        raise ValueError('景点名称或详情页标识不匹配')
    logger.info('景点详情核对完成')
    # /* ===== 步骤2：提取展示字段；数据源：当前一屏；操作：1) 保留相对日期原文 2) 明示入口未展开 ===== */
    logger.info('开始提取景点展示字段')
    # 2.1 区分景区概览价和同一卡片明确展示的票品价。
    result = {'name': expected_name, 'source_channel': 'android_ui_snapshot',
              'identity_verification': 'operator_name_exact_match',
              'raw_hash': hashlib.sha256(xml.encode('utf-8')).hexdigest(),
              'opening_texts': [t for t in texts if '闭园' in t or ('开放' in t and re.search(r'\d{1,2}:\d{2}', t))],
              'price_texts': [t for t in texts if re.match(r'^门票[¥￥]\d', t)],
              'ticket_categories': list(dict.fromkeys(n.get('text', '').strip() for n in nodes if n.get('resource-id', '').startswith('左侧分类-') and n.get('text', '').strip())),
              'booking_conditions_visible': [t for t in texts if t in BOOKING],
              'policy_status': 'entry_visible_content_not_captured' if '优待政策' in texts else 'not_visible',
              'complete': False,
              'limitations': ['仅当前屏幕；不代表所有票种、日期或库存。', '明日等相对日期依赖原始采集时间，不自动换算。']}
    if title_node is not None:
        result.update(sight_header(nodes, parents, title_node))
    result['ticket_cards'] = ticket_cards(nodes, parents)
    logger.info('景点展示字段提取完成')
    return result


def capture_sight_snapshot(device, output, expected_name):
    # /* ===== 步骤1：采集当前详情；数据源：已连接设备；操作：1) 前后核验前台 2) 原子保存 ===== */
    logger.info('开始采集当前景点详情')
    # 1.1 时间记录在读取前，不用历史文件时间冒充实时采集。
    device.ensure_foreground()
    captured_at = datetime.now(timezone(timedelta(hours=8))).isoformat()
    xml = device.snapshot()
    device.ensure_foreground()
    result = parse_sight_snapshot(xml, expected_name)
    result['captured_at'] = captured_at
    # 1.2 每次独立文件，不覆盖其他来源，不保存完整屏幕或其他应用内容。
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / ('sight-detail-' + uuid.uuid4().hex + '.json')
    result['file'] = str(path.resolve())
    temporary = path.with_suffix('.part')
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    logger.info('当前景点详情保存完成')
    return result


if __name__ == '__main__':
    # /* ===== 步骤1：启动实时入口；数据源：命令行；操作：1) 连接唯一设备 2) 自动释放 ===== */
    import argparse
    from runner import Adb
    logging.basicConfig(level=logging.INFO)
    logger.info('开始景点实时采集入口')
    # 1.1 独立入口只执行当前屏采集，不触发预订或支付。
    parser = argparse.ArgumentParser(description='采集携程App当前景点详情屏')
    parser.add_argument('--adb', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with Adb(args.adb, ui_backend='uiautomator2') as device:
        device.connect()
        print(json.dumps(capture_sight_snapshot(device, args.output, args.name), ensure_ascii=True, indent=2))
    logger.info('景点实时采集入口结束')
