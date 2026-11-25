"""Read public shopping venue cards from an Android city shopping list."""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from subprocess import TimeoutExpired
from xml.etree import ElementTree as ET

from app_hotel_list import swipe_results
from app_hotel_snapshot import one
from core import bounds
from sight_guide import app_nodes, text
from sight_snapshot import APP


SOURCE = 'android_shopping_list_snapshot'
FILTER_MARKER = '261342'
TITLE_CLASS = 'android.widget.TextView'
VIEW_GROUP = 'android.view.ViewGroup'
RATING = re.compile(
    r'(?:(?P<score>[0-4](?:\.[0-9])?|5(?:\.0)?)分)?\s*'
    r'(?P<count>[0-9][0-9,]*(?:\.[0-9]+)?[万千wW]?\+?条点评|暂无点评)?'
)
LOCATION = re.compile(r'距.{0,20}?[0-9][0-9,.]*\s*(?:km|m|公里|米)(?:\s*·\s*\S.*)?', re.IGNORECASE)
CATEGORY = re.compile(r'[\u3400-\u9fffA-Za-z0-9+·/、 -]{2,48}')


class ShoppingCityMismatch(ValueError):
    pass


def within(outer, inner):
    return outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]


def visible_text(node, viewport):
    if (node.get('package') == APP and node.get('class') == TITLE_CLASS
            and node.get('visible-to-user') != 'false' and within(viewport, bounds(node))):
        return text(node)
    return ''


def shop_summary(card, viewport):
    card_box = bounds(card)
    if card.get('class') != VIEW_GROUP or not within(viewport, card_box):
        return None
    left, top, right, _ = card_box
    width = right - left
    summary = one(child for child in card if child.get('package') == APP and child.get('class') == VIEW_GROUP
                  and within(card_box, bounds(child)) and (box := bounds(child))[1] == top
                  and left + width // 5 <= box[0] <= left + width // 2 and box[2] == right)
    if summary is None:
        return None
    boundary = bounds(summary)[0]
    if not any(node.get('class') == 'android.widget.ImageView' and within(card_box, bounds(node))
               and (box := bounds(node))[2] <= boundary and box[2] - box[0] >= width // 5
               for node in app_nodes(card)):
        return None
    return summary


def parse_shopping_card(summary):
    viewport = bounds(summary)
    children = [node for node in summary if node.get('package') == APP]
    entries = [(node, label) for node in children if (label := visible_text(node, viewport))
               and any(char.isalnum() for char in label)]
    if not entries:
        return None
    name_node, name = entries[0]
    if not 2 <= len(name) <= 100 or not any(char.isalpha() for char in name):
        return None
    result = {'name': name}
    left = bounds(name_node)[0]
    if len(entries) > 1:
        node, value = entries[1]
        if (bounds(node)[0] == left and re.search(r'[A-Za-z]', value)
                and not re.search(r'[\u3400-\u9fff]', value)):
            result['english_name'] = value

    descriptions = [value for node, value in entries[1:] if (index := children.index(node)) > 0
                    and (background := children[index - 1]).get('class') == VIEW_GROUP
                    and bounds(background)[0] == left and within(bounds(background), bounds(node))]
    if len(descriptions) == 1:
        result['tagline_display'] = descriptions[0]

    ratings = [(node, match) for node, value in entries[1:] if (match := RATING.fullmatch(value))
               and any(match.groups()) and bounds(node)[0] == left]
    locations = [(node, value) for node, value in entries[1:]
                 if LOCATION.fullmatch(value) and bounds(node)[0] == left]
    if len(ratings) == 1:
        for key, value in zip(('score_display', 'review_count_display'), ratings[0][1].groups()):
            if value:
                result[key] = value
    if len(locations) == 1:
        result['location_display'] = locations[0][1]
    if len(ratings) == 1 or len(locations) == 1:
        anchor = ratings[0][0] if len(ratings) == 1 else locations[0][0]
        prev = children[children.index(anchor) - 1]
        label = visible_text(prev, viewport)
        if (bounds(prev)[0] == left and label not in descriptions
                and label != result.get('english_name') and CATEGORY.fullmatch(label)
                and re.search(r'[\u3400-\u9fff]', label) and not RATING.fullmatch(label)):
            result['category_display'] = label

    offers = []
    for background, badge, content in zip(children, children[1:], children[2:]):
        if (background.get('class') != 'android.view.View' or bounds(background)[0] != left
                or not within(bounds(background), bounds(badge))
                or not re.fullmatch(r'[\u3400-\u9fff]', visible_text(badge, viewport))):
            continue
        x1, y1, _, _ = bounds(content)
        _, top, right, bottom = bounds(background)
        if right == x1 and top <= y1 < bottom and (value := visible_text(content, viewport)):
            offers.append(value)
    if offers:
        result['offers'] = offers
    return result


def parse_shopping_list(xml, city):
    root = ET.fromstring(xml)
    nodes = [node for window in root for node in app_nodes(window)]
    parents = {child: parent for parent in root.iter() for child in parent}
    marker = one(node for node in nodes if node.get('resource-id') == FILTER_MARKER
                 and node.get('class') == VIEW_GROUP)
    if marker is None:
        raise ValueError('未识别到携程购物列表，请打开城市的「购物」页面')
    section = parents[marker]
    filter_row = one(node for node in section if node.get('package') == APP
                     and node.get('class') == 'android.widget.HorizontalScrollView'
                     and bounds(node)[:3] == bounds(marker)[:3])
    scroller = section
    while scroller in parents and scroller.get('class') != 'android.widget.ScrollView':
        scroller = parents[scroller]
    if (filter_row is None or scroller.get('class') != 'android.widget.ScrollView'
            or scroller.get('package') != APP):
        raise ValueError('未识别到携程购物列表的筛选栏')
    scene = parents[scroller]
    left, top, right, bottom = bounds(scene)
    toolbar = one(node for node in scene if node.get('package') == APP
                  and node.get('class') == VIEW_GROUP and bounds(node)[:3] == [left, top, right]
                  and top < bounds(node)[3] <= top + (bottom - top) // 5)
    title = one(label for node in app_nodes(toolbar) if (label := visible_text(node, bounds(toolbar)))
                and any(char.isalnum() for char in label)) if toolbar is not None else None
    if title != city or not city:
        raise ShoppingCityMismatch('购物列表顶部的城市与所填名称不一致')
    labels, selected = [], []
    for node in app_nodes(filter_row):
        label = visible_text(node, bounds(filter_row))
        if not label or not any(char.isalnum() for char in label):
            continue
        labels.append(label)
        if any(candidate.get(state) == 'true' for candidate in (node, parents[node])
               for state in ('selected', 'checked')):
            selected.append(label)
    if not labels:
        raise ValueError('未读取到购物列表的可见筛选设置')
    x1, y1, x2, y2 = bounds(scroller)
    viewport = (x1, max(y1, bounds(toolbar)[3], bounds(filter_row)[3]), x2, y2)
    section_children = list(section)
    branches = section_children[section_children.index(filter_row) + 1:]
    scroll_content = parents.get(section)
    if not branches and scroll_content is not None and parents.get(scroll_content) is scroller:
        # Once the filter bar becomes sticky, the App gives it its own sibling of the card body.
        content_children = list(scroll_content)
        body = one(node for node in content_children[content_children.index(section) + 1:]
                   if node.get('package') == APP and node.get('class') == VIEW_GROUP
                   and bounds(node) == bounds(scroller) and len(node))
        if body is not None:
            branches = list(body)
    rows = [node for node in branches if node.get('class') == VIEW_GROUP and node.get('package') == APP and len(node)]
    shops = []
    for candidate in rows:
        summary = shop_summary(candidate, viewport)
        if summary is not None and (shop := parse_shopping_card(summary)) is not None:
            shops.append(shop)
    return scroller, {'city': title, 'visible_filters': labels, 'selected_filters': selected}, shops, len(rows)


def capture_shopping_list(device, output, city, stop, *, max_pages):
    if type(max_pages) is not int or not 1 <= max_pages <= 20:
        raise ValueError('购物列表读取屏数须为1至20')
    if not isinstance(city, str) or not city.strip():
        raise ValueError('请填写购物列表顶部显示的城市名称')
    city = city.strip()
    path = Path(output).resolve() / f'app-shopping-list-{uuid.uuid4().hex}.json'
    temporary = path.with_suffix('.part')
    report, seen, unchanged = None, {}, 0
    reason = 'page_limit'

    def save():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    for index in range(1, max_pages + 1):
        if stop.is_set():
            reason = 'cancelled'
            break
        try:
            device.ensure_foreground()
            captured_at = datetime.now(timezone.utc).isoformat()
            xml = device.snapshot()
            device.ensure_foreground()
            if stop.is_set():
                reason = 'cancelled'
                break
            scroller, query, shops, candidate_count = parse_shopping_list(xml, city)
        except (ValueError, ET.ParseError) as exc:
            if report is None:
                raise
            reason = 'query_changed' if isinstance(exc, ShoppingCityMismatch) else 'page_unavailable'
            break
        except (OSError, RuntimeError, TimeoutExpired):
            if report is None:
                raise
            reason = 'device_unavailable'
            break
        if report is None:
            report = {'kind': 'shopping', 'mode': 'app_shopping_list', 'source': 'ctrip_android_ui',
                      'source_method': SOURCE, 'status': 'running', 'city': city, 'query': query,
                      'captured_at': captured_at, 'report_file': str(path), 'shops': [], 'screens': [],
                      'complete': False, 'limitations': ['只包含已读取的可见商铺卡片。城市和可见筛选按页面核对。',
                                                        '相同展示记录合并，未核对商铺 ID、总数或优惠有效性。']}
        elif report['query'] != query:
            reason = 'query_changed'
            break
        before = len(seen)
        for shop in shops:
            key = json.dumps(shop, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen[key] = {**shop, 'screens_seen': []}
                report['shops'].append(seen[key])
            if index not in seen[key]['screens_seen']:
                seen[key]['screens_seen'].append(index)
        unchanged = unchanged + 1 if len(seen) == before else 0
        report['screens'].append({'index': index, 'captured_at': captured_at,
                                  'raw_hash': hashlib.sha256(xml.encode('utf-8')).hexdigest(),
                                  'shop_cards': len(shops), 'unrecognized_cards': candidate_count - len(shops)})
        save()
        if index == max_pages:
            break
        if unchanged >= 2:
            reason = 'no_new_cards'
            break
        try:
            if not swipe_results(device, scroller, stop):
                reason = 'cancelled' if stop.is_set() else 'not_scrollable'
                break
        except (OSError, RuntimeError, TimeoutExpired):
            reason = 'device_unavailable'
            break
        if stop.wait(0.65):
            reason = 'cancelled'
            break
    if report is None:
        return {'reason': reason, 'complete': False}
    report.update(status='partial', reason=reason)
    save()
    return report
