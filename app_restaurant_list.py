"""Read visible restaurant cards from the Android city food list."""
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
from sight_snapshot import APP, unique_value


SOURCE = 'android_restaurant_list_snapshot'
CARD = 'o_gs_gourmethome_card'
FILTERS = {'o_gs_gourmethome_shaixuan', 'o_gs_gourmethome_kuaishai'}
TITLE_CLASS = 'android.widget.TextView'
RANKING_ICON = 'gm_food_detail_koubeiRankingIcon'
SCORE = re.compile(r'([0-4](?:\.[0-9])?|5(?:\.0)?)(?:\s+[\u3400-\u9fff]{1,8})?')
COUNT = re.compile(r'[0-9][0-9,]*(?:\.[0-9]+)?[万千]?\+?条(?:点评|评论)?')
PRICE = re.compile(r'[¥￥][0-9][0-9,]*(?:\.[0-9]{1,2})?/人')
OPENING = re.compile(r'(?:[0-2][0-9]:[0-5][0-9](?:营业|开门)|营业中|休息中|已打烊|已休息|暂停营业|歇业中)')


class CityMismatch(ValueError):
    pass


def within(outer, inner):
    return outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]


def same_row(left, right):
    return left[2] <= right[0] and (left[1], left[3]) == (right[1], right[3])


def parse_restaurant_card(card, viewport):
    box = bounds(card)
    if not within(viewport, box):
        return None
    children = [node for node in card if node.get('package') == APP]
    thumbnail = one(bounds(node) for group in children for node in group
                    if node.get('package') == APP and node.get('class') == 'android.widget.ImageView'
                    and (image := bounds(node))[2] - image[0] >= (box[2] - box[0]) // 5
                    and image[2] <= (box[0] + box[2]) // 2 and within(box, image))
    if thumbnail is None:
        return None
    words = [(word, bounds(node)) for node in children if node.get('class') == TITLE_CLASS
             and within(box, bounds(node)) and (word := text(node))]
    title = one((word, area) for word, area in words if thumbnail[2] <= area[0]
                and area[1] == thumbnail[1] and 2 <= len(word) <= 100
                and any(char.isalpha() for char in word))
    if title is None:
        return None
    name, title_box = title
    result = {'name': name}
    ratings = []
    for word, area in words:
        if area[0] != title_box[0] or area[1] <= title_box[3]:
            continue
        parts = [value.strip() for value in word.split('|')]
        score = unique_value(match[1] for value in parts if (match := SCORE.fullmatch(value)))
        count = unique_value(value for value in parts if COUNT.fullmatch(value))
        price = unique_value(value for value in parts if PRICE.fullmatch(value))
        if score or count or price:
            ratings.append((area, score, count, price))
    rating = one(ratings)
    if rating is not None:
        rating_box, score, count, price = rating
        result.update(score_display=score, review_count_display=count, per_person_price_display=price,
                      opening_status_display=unique_value(word for word, area in words
                                                          if same_row(rating_box, area) and OPENING.fullmatch(word)))
        for word, area in words:
            if area[0] != title_box[0] or not rating_box[3] < area[1] <= rating_box[3] + (area[3] - area[1]):
                continue
            location = unique_value(value for value, right in words if same_row(area, right)
                                    and 1 <= len(value) <= 30 and any(char.isalpha() for char in value))
            parts = word.split(maxsplit=1)
            nearby = parts[1] if len(parts) == 2 and parts[1].startswith(('近', '距')) else None
            if (nearby or location) and re.fullmatch(r'[\u3400-\u9fff]{1,16}', parts[0]):
                result.update(cuisine_display=parts[0], nearby_display=nearby, location_display=location)
    ranking = one(node for node in app_nodes(card) if node.get('resource-id') == RANKING_ICON)
    if ranking is not None and within(box, bounds(ranking)):
        icon = bounds(ranking)
        result['ranking_display'] = unique_value(text(node) for node in app_nodes(card)
                                                 if node.get('class') == TITLE_CLASS and within(box, bounds(node))
                                                 and same_row(icon, bounds(node))
                                                 and re.fullmatch(r'.{1,36}榜(?:\s+No\.\d+)?', text(node)))
    return {key: value for key, value in result.items() if value is not None}


def parse_restaurant_list(xml, city):
    root = ET.fromstring(xml)
    nodes = [node for window in root for node in app_nodes(window)]
    parents = {child: parent for parent in root.iter() for child in parent}
    scroller = one(node for node in nodes if node.get('class') == 'android.widget.ScrollView'
                   and any(card.get('resource-id') == CARD for card in app_nodes(node)))
    if scroller is None:
        raise ValueError('未识别到携程城市餐厅列表，请打开城市的「美食」页面')
    parent = parents[scroller]
    left, top, right, bottom = bounds(parent)
    toolbar = one(node for node in parent if node.get('class') == 'android.view.ViewGroup'
                  and bounds(node)[:3] == [left, top, right]
                  and top < bounds(node)[3] <= top + (bottom - top) // 5)
    title = one(text(node) for node in app_nodes(toolbar) if node.get('class') == TITLE_CLASS
                and any(char.isalnum() for char in text(node))) if toolbar is not None else None
    if title != city or not city:
        raise CityMismatch('餐厅列表顶部的城市与所填名称不一致')
    scroller_box = bounds(scroller)
    viewport = (scroller_box[0], max(scroller_box[1], bounds(toolbar)[3]), scroller_box[2], scroller_box[3])
    filters = [label for node in app_nodes(parent) if node.get('resource-id') in FILTERS
               if (label := one(text(child) for child in app_nodes(node) if any(char.isalnum() for char in text(child))))]
    cards = [node for node in app_nodes(scroller) if node.get('resource-id') == CARD]
    restaurants = [restaurant for card in cards if (restaurant := parse_restaurant_card(card, viewport)) is not None]
    return scroller, {'city': title, 'visible_filters': filters}, restaurants, len(cards) - len(restaurants)


def capture_restaurant_list(device, output, city, stop, *, max_pages):
    if type(max_pages) is not int or not 1 <= max_pages <= 20:
        raise ValueError('餐厅列表读取屏数须为1至20')
    if not isinstance(city, str) or not city.strip():
        raise ValueError('请填写餐厅列表顶部显示的城市名称')
    city = city.strip()
    path = Path(output).resolve() / f'app-restaurant-list-{uuid.uuid4().hex}.json'
    temporary = path.with_suffix('.part')
    seen = {}
    report = None
    reason = 'page_limit'
    unchanged = 0

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
            scroller, query, cards, skipped = parse_restaurant_list(xml, city)
        except (ValueError, ET.ParseError) as exc:
            if report is None:
                raise
            reason = 'query_changed' if isinstance(exc, CityMismatch) else 'page_unavailable'
            break
        except (OSError, RuntimeError, TimeoutExpired):
            if report is None:
                raise
            reason = 'device_unavailable'
            break
        if report is None:
            report = {'kind': 'restaurant', 'mode': 'app_restaurant_list', 'source': 'ctrip_android_ui',
                      'source_method': SOURCE, 'status': 'running', 'city': city, 'query': query,
                      'captured_at': captured_at, 'report_file': str(path), 'restaurants': [], 'screens': [],
                      'complete': False, 'limitations': ['只包含已读取的可见列表；当前城市及筛选由页面显示确认。',
                                                        '相同展示记录合并。未核对平台餐厅编号或全城餐厅总数。']}
        elif query != report['query']:
            reason = 'query_changed'
            break
        previous = len(seen)
        for card in cards:
            key = tuple(card.items())
            if key not in seen:
                seen[key] = {**card, 'screens_seen': []}
                report['restaurants'].append(seen[key])
            if index not in seen[key]['screens_seen']:
                seen[key]['screens_seen'].append(index)
        unchanged = unchanged + 1 if previous == len(seen) else 0
        report['screens'].append({'index': index, 'captured_at': captured_at,
                                  'raw_hash': hashlib.sha256(xml.encode('utf-8')).hexdigest(),
                                  'restaurant_cards': len(cards), 'unrecognized_cards': skipped})
        save()
        if index == max_pages:
            break
        if not query['visible_filters']:
            reason = 'query_incomplete'
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
