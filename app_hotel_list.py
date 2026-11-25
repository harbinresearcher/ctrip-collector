"""Collect hotel search cards shown in the Android app with their query context."""
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import TimeoutExpired
from xml.etree import ElementTree as ET

from app_hotel_snapshot import one
from core import bounds
from sight_guide import app_nodes, text
from sight_snapshot import APP, unique_value


SOURCE = 'android_hotel_list_snapshot'
SEARCH = 'htl_searchComponent_csearch_'
FILTER = 'htl_searchComponent_cfilter_filter_bar_'
CARD = 'htl_hotelCard_card_hotel_list_normal_card_'
PRESSABLE = 'htl_hotelCard_card_hotel_pressable_'
NUMBER = r'[0-9][0-9,]*(?:\.[0-9]+)?[万千]?\+?'


class QueryMismatch(ValueError):
    pass


def labels(branch):
    return [word for node in app_nodes(branch) if (word := text(node)) and any(char.isalnum() for char in word)]


def read_query(page, expected_location):
    nodes = list(app_nodes(page))
    location, stay, guests, keywords = (
        one(node for node in nodes if node.get('resource-id') == SEARCH + key)
        for key in ('search_bar_ihzbch', 'search_bar_ucegnm', 'search_bar_wzfxnk', 'keyword_search_view_msxzyf')
    )
    region = one(labels(location)) if location is not None else None
    if not region or region.removesuffix('.') != expected_location.removesuffix('.'):
        raise QueryMismatch('酒店列表搜索地区与所填名称不一致，请核对搜索栏')
    dates = labels(stay) if stay is not None else []
    dates = dates if len(dates) == 2 and all(re.fullmatch(r'[0-9]{2}-[0-9]{2}', date) for date in dates) else []
    occupancy = ''.join(labels(guests)) if guests is not None else ''
    occupancy = occupancy if re.fullmatch(r'[0-9]+间[0-9]+人', occupancy) else None
    filters = [word for node in nodes if node.get('resource-id', '').startswith(FILTER) for word in labels(node)]
    selected = [word for node in nodes if node.get('resource-id') == 'htl_x_comp_filter_list_card_exposure'
                and any(child.get('selected') == 'true' or child.get('checked') == 'true' for child in app_nodes(node))
                for word in labels(node)]
    return {'location': region, 'stay_dates': dates, 'room_guest_label': occupancy,
            'keyword_label': unique_value(labels(keywords)) if keywords is not None else None,
            'visible_filters': filters, 'confirmed_quick_filters': selected}


def read_hotel(card):
    content = one(node for node in app_nodes(card) if node.get('resource-id', '').startswith(PRESSABLE))
    if content is None:
        return None
    children = [node for node in content if node.get('package') == APP]
    title = children[0] if children else None
    if title is None or title.get('class') != 'android.widget.TextView':
        return None
    name = text(title).replace('\ufffc', '').strip()
    if not 2 <= len(name) <= 100 or not any(char.isalpha() for char in name):
        return None
    _, card_top, _, _ = bounds(content)
    _, title_top, _, title_bottom = bounds(title)
    if not 0 < title_top - card_top <= title_bottom - title_top:
        return None
    direct = [text(node) for node in children if text(node)]
    score = unique_value(word for word in direct if re.fullmatch(r'(?:[0-4]\.[0-9]|5\.0)', word))
    counts = [match for word in direct
              if (match := re.fullmatch(fr'({NUMBER}点评)(?:\s*·\s*({NUMBER}收藏))?', word))]
    count = one(counts)
    prices = [currency + amount + suffix for currency, amount, suffix in zip(direct, direct[1:], direct[2:])
              if currency in {'¥', '￥'} and re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', amount) and suffix == '起']
    words = labels(content)
    offer = one(node for node in app_nodes(content)
                if node.get('resource-id', '').startswith('htl_hotelCard_card_hotel_price_info_'))
    offers = labels(offer) if offer is not None else []
    fields = {'name': name, 'score_display': score, 'review_count_display': count[1] if count else None,
              'favorites_count_display': count[2] if count else None,
              'distance_display': unique_value(word for word in words if re.fullmatch(r'距.+[0-9](?:\.[0-9]+)?(?:公里|千米|米)', word)),
              'nearby_display': unique_value(word for word in words if word.startswith('近')),
              'starting_price_display': unique_value(prices),
              'offer_label_display': '特惠一口价' if '特惠一口价' in offers else None,
              'discount_display': unique_value(word for word in offers if re.fullmatch(r'优惠[0-9]+(?:\.[0-9]{1,2})?', word))}
    return {key: value for key, value in fields.items() if value is not None}


def parse_screen(xml):
    root = ET.fromstring(xml)
    page = one(node for window in root for node in app_nodes(window)
               if node.get('resource-id') == 'hotel_list_page_view_root')
    if page is None:
        raise ValueError('未识别到携程酒店搜索列表，请先打开酒店搜索结果')
    scroller = one(node for node in app_nodes(page)
                   if node.get('class') == 'android.widget.ScrollView'
                   and any(card.get('resource-id', '').startswith(CARD) for card in app_nodes(node)))
    if scroller is None:
        raise ValueError('当前酒店列表未显示可识别的酒店卡片')
    cards = [node for node in app_nodes(scroller) if node.get('resource-id', '').startswith(CARD)]
    hotels, advertised, unrecognized = [], 0, 0
    for card in cards:
        if '广告' in labels(card):
            advertised += 1
        elif hotel := read_hotel(card):
            hotels.append(hotel)
        else:
            unrecognized += 1
    return page, scroller, hotels, advertised, unrecognized


def swipe_results(device, scroller, stop):
    left, top, right, bottom = bounds(scroller)
    height = bottom - top
    if scroller.get('scrollable') != 'true' or right - left < 100 or height < 350:
        return False
    device.ensure_foreground()
    if stop.is_set():
        return False
    x = str((left + right) // 2)
    device.run('shell', 'input', 'swipe', x, str(top + height * 72 // 100),
               x, str(top + height * 23 // 100), '520')
    return True


def capture_hotel_list(device, output, location, stop, *, max_pages, emit):
    if type(max_pages) is not int or not 1 <= max_pages <= 20:
        raise ValueError('酒店列表读取屏数须为1至20')
    folder = Path(output)
    path = folder / f'app-hotel-list-{uuid.uuid4().hex}.json'
    temporary = path.with_suffix('.part')
    report = None
    seen = {}
    unchanged = 0

    def save():
        folder.mkdir(parents=True, exist_ok=True)
        try:
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    reason = 'page_limit'
    for index in range(1, max_pages + 1):
        if stop.is_set():
            reason = 'cancelled'
            break
        try:
            device.ensure_foreground()
            captured_at = datetime.now(timezone(timedelta(hours=8))).isoformat()
            xml = device.snapshot()
            device.ensure_foreground()
            if stop.is_set():
                reason = 'cancelled'
                break
            page, scroller, cards, ads, skipped = parse_screen(xml)
            if report is not None:
                # Preserve a successfully saved screen when the subsequent one changes or is unavailable.
                query = read_query(page, report['query']['location'])
                if query != report['query']:
                    reason = 'query_changed'
                    break
            else:
                query = read_query(page, location)
                report = {'source_method': SOURCE, 'status': 'running', 'query': query,
                          'report_file': str(path.resolve()), 'captured_at': captured_at,
                          'hotels': [], 'screens': [], 'complete': False,
                          'limitations': ['只包含已读取的搜索结果，排除明确标注广告的卡片。',
                                          '同样的展示记录合并，未核对平台酒店编号。价格为当时页面起价。']}
        except (ValueError, ET.ParseError) as exc:
            if report is None:
                raise
            reason = 'query_changed' if isinstance(exc, QueryMismatch) else 'page_unavailable'
            break
        except (OSError, RuntimeError, TimeoutExpired):
            if report is None:
                raise
            reason = 'device_unavailable'
            break
        previous = len(seen)
        for card in cards:
            key = tuple(card.items())
            if key not in seen:
                row = {**card, 'screens_seen': []}
                seen[key] = row
                report['hotels'].append(row)
            if index not in seen[key]['screens_seen']:
                seen[key]['screens_seen'].append(index)
        unchanged = unchanged + 1 if previous == len(seen) else 0
        report['screens'].append({'index': index, 'captured_at': captured_at,
                                  'raw_hash': hashlib.sha256(xml.encode('utf-8')).hexdigest(),
                                  'hotel_cards': len(cards), 'advertised_cards': ads, 'unrecognized_cards': skipped})
        save()
        emit(f'已保存第 {index} 屏，累计 {len(seen)} 条酒店展示')
        if index == max_pages:
            break
        if not query['stay_dates'] or not query['room_guest_label']:
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
