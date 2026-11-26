"""Read the visible restaurant detail page in Ctrip for Android."""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from app_restaurant_menu import parse_restaurant_menu
from core import bounds
from sight_guide import app_nodes, text
from sight_snapshot import APP, unique_value


SOURCE = 'android_restaurant_detail_snapshot'
REVIEW_COUNT = re.compile(r'[0-9][0-9,]*(?:\.[0-9]+)?[万千wW]?\+?条点评')
PRICE = re.compile(r'人均\s*[¥￥]\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?')
SCORE = re.compile(r'[0-4](?:\.[0-9])?|5(?:\.0)?')
TIME = r'(?:[01]?[0-9]|2[0-4]):[0-5][0-9]'
HOURS = re.compile(rf'{TIME}\s*[-–—~至]\s*{TIME}')
STATUS = {'营业中', '休息中', '已打烊', '暂停营业', '歇业中'}
FEATURE_LABELS = ('餐厅特色', '亮点', '服务设施')
MAX_RECOMMENDATION_SCREENS = 20


def words(branch):
    return [value for node in app_nodes(branch) if node.get('class') == 'android.widget.TextView'
            and (value := text(node)) and any(char.isalnum() for char in value)
            and (box := bounds(node))[0] < box[2] and box[1] < box[3]]


def recommendation_view(scroll, root):
    result = {'dish_count_display': None, 'recommended_dishes': []}
    sections = [node for node in app_nodes(scroll) if node.get('resource-id') == 'o_gs_tuijiancai']
    if len(sections) != 1:
        return result, None, None
    section = sections[0]
    counts = [int(match[1]) for value in words(section)
              if (match := re.fullmatch(r'推荐菜\s*[(（]([0-9]{1,5})[)）]', value))]
    if len(counts) == 1:
        result['dish_count_display'] = counts[0]
    rows = [node for node in app_nodes(section) if node.get('class') == 'android.widget.HorizontalScrollView']
    if len(rows) != 1:
        return result, None, None
    groups = [node for node in rows[0] if node.get('package') == APP]
    if len(groups) != 1:
        return result, None, None
    dishes = []
    for card in groups[0]:
        labels = words(card)
        if len(labels) == 1 and (card.get('content-desc') == labels[0]
                                or any(n.get('class') == 'android.widget.ImageView' for n in app_nodes(card))):
            dishes.append(labels[0])
    result['recommended_dishes'] = list(dict.fromkeys(dishes))
    carousel = bounds(rows[0]) if rows[0].get('scrollable') == 'true' else None
    links = [bounds(node) for parent in app_nodes(section) if parent.get('clickable') == 'true'
             and '查看全部' in parent.get('content-desc', '').split(', ')
             for node in parent if text(node) == '查看全部' and node.get('package') == APP
             and node.get('class') == 'android.widget.TextView']
    link = None
    if len(links) == 1:
        left, top, right, bottom = links[0]
        x, y = (left + right) // 2, (top + bottom) // 2
        overlays = [bounds(node) for window in root for node in app_nodes(window)
                    if node.get('content-desc') == 'gm_food_detail_advertisingFloatIcon']
        if right - left >= 30 and bottom - top >= 24 and not any(
                x1 <= x < x2 and y1 <= y < y2 for x1, y1, x2, y2 in overlays):
            link = (x, y)
    return result, carousel, link


def booking_fields(scroll):
    branches = [branch for row in scroll for branch in row if '立即订座' in words(branch)]
    if len(branches) != 1:
        return {}
    visible = words(branches[0])
    result = {}
    if earliest := unique_value(w for w in visible if re.fullmatch(r'最早\s*.{1,25}可订', w)):
        result['earliest_display'] = earliest
    if booked := unique_value(w for w in visible if re.fullmatch(r'[0-9][0-9,]*\+?人已订', w)):
        result['booked_count_display'] = booked
    parties = [f'{count} 人用餐' for count, label in zip(visible, visible[1:])
               if count.isdecimal() and label == '人用餐']
    if len(parties) == 1:
        result['party_size_display'] = parties[0]
    slots = list(dict.fromkeys(w for w in visible if re.fullmatch(TIME, w)))
    if slots:
        result['time_slots_display'] = slots
    return result


def header_fields(header):
    result = {}
    rows = [words(branch) for branch in header]
    rating_rows = [row for row in rows if any(REVIEW_COUNT.fullmatch(w) or PRICE.fullmatch(w) for w in row)]
    if len(rating_rows) == 1:
        row = rating_rows[0]
        for key, pattern in (('score_display', SCORE), ('review_count_display', REVIEW_COUNT),
                             ('per_person_price_display', PRICE)):
            if value := unique_value(w for w in row if pattern.fullmatch(w)):
                result[key] = value
        if score := result.get('score_display'):
            index = row.index(score) + 1
            if index < len(row) and row[index] in {'超棒', '棒', '好', '不错', '一般'}:
                result['rating_label'] = row[index]
        anchor = result.get('per_person_price_display') or result.get('review_count_display')
        if anchor and row.index(anchor) == len(row) - 2 and re.fullmatch(r'[\u3400-\u9fff]{2,18}', row[-1]):
            result['cuisine_display'] = row[-1]
    if rank := unique_value(w for row in rows for w in row if re.fullmatch(r'(?:.+榜\s+No\.\d+|入选.+榜)', w)):
        result['ranking_display'] = rank
    if badge := unique_value(w for row in rows for w in row if w in {'铂金', '金牌', '银牌', '钻石'}):
        result['rank_badge'] = badge

    open_cards = [words(n) for n in app_nodes(header) if n is not header and len(n)]
    hours_cards = [row for row in open_cards if len(row) == 2 and row[0] in STATUS and HOURS.search(row[1])]
    if status := unique_value(row[0] for row in hours_cards):
        result['opening_status_display'] = status
    if hours := unique_value(row[1] for row in hours_cards):
        result['opening_hours_display'] = hours
    result['features'] = {label: value for label in FEATURE_LABELS
                          if (value := unique_value(row[1] for row in open_cards if len(row) == 2 and row[0] == label))}

    navigate = [bounds(n) for n in header if 'gm_food_detail_navigationIcon' in n.get('content-desc', '').split(', ')]
    if len(navigate) == 1:
        x, y, _, bottom = navigate[0]
        addresses = []
        for branch in header:
            left, top, right, lower = bounds(branch)
            values = words(branch)
            if (left < right <= x and top < bottom and y < lower
                    and branch.get('content-desc') == ', '.join(values)
                    and (len(values) == 1 or len(values) == 2 and values[1].startswith('距'))):
                addresses.append(values)
        if len(addresses) == 1:
            result['address_display'] = addresses[0][0]
            if len(addresses[0]) == 2:
                result['nearby_display'] = addresses[0][1]
    return result


def detail_view(xml, expected_name):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError('餐厅详情页面读取失败') from exc
    scrollers = [node for window in root for node in app_nodes(window)
                 if node.get('resource-id') == 'gm_food_detail_scroll' and node.get('class') == 'android.widget.ScrollView']
    if len(scrollers) != 1:
        raise ValueError('请打开餐厅详情顶部')
    scroll = scrollers[0]
    headers = [branch for branch in app_nodes(scroll) if len(branch)
               and branch[0].get('package') == APP and branch[0].get('class') == 'android.widget.TextView'
               and text(branch[0]) == expected_name and expected_name
               and any(child.get('package') == APP and (
                   child.get('resource-id') == 'o_gs_newlable'
                   or 'gm_food_detail_navigationIcon' in child.get('content-desc', '').split(', ')) for child in branch)]
    if len(headers) != 1:
        raise ValueError('餐厅详情顶部名称与任务名称不一致，或名称不可见')
    result = {'kind': 'restaurant', 'mode': 'app_restaurant_detail',
              'source': 'ctrip_android_ui', 'source_method': SOURCE, 'name': expected_name,
              'identity_verification': 'operator_name_exact_match',
              'raw_hash': hashlib.sha256(xml.encode('utf-8')).hexdigest(),
              'status': 'partial', 'complete': False}
    result.update(header_fields(headers[0]))
    recommendations, carousel, menu_link = recommendation_view(scroll, root)
    result.update(recommendations)
    result['booking_preview'] = booking_fields(scroll)
    return result, carousel, menu_link


def parse_restaurant_detail(xml, expected_name):
    return detail_view(xml, expected_name)[0]


def moved_view(device, stop, action, settle):
    device.ensure_foreground()
    if stop.is_set():
        return None
    if action:
        device.run('shell', 'input', *action)
    if stop.wait(settle):
        return None
    xml = device.snapshot()
    device.ensure_foreground()
    return None if stop.is_set() else xml


def read_menu_transition(device, stop, tap):
    # The WebView may replace the detail before its list appears.
    action = tap
    for settle in (1.2, .8, .8, .8):
        xml = moved_view(device, stop, action, settle)
        if xml is None:
            return None, None
        try:
            return xml, parse_restaurant_menu(xml)
        except ValueError:
            action = None
    return xml, None


def scan_recommendations(device, report, carousel, menu_link, stop):
    dishes = report['recommended_dishes']
    previous = dishes.copy()
    expected = report['dish_count_display']
    screens = int(bool(dishes))
    idle_screens = 0
    method = 'carousel'
    reason = 'screen_limit'
    while screens < MAX_RECOMMENDATION_SCREENS:
        if stop.is_set():
            reason = 'cancelled'
            break
        if dishes and len(dishes) == expected:
            reason = 'expected_count_reached'
            break
        opening_menu = bool(menu_link and dishes and expected)
        if opening_menu:
            action = ('tap', *map(str, menu_link))
            menu_link = None
        else:
            if not carousel or not previous:
                reason = 'no_new_dishes' if method == 'menu' else 'missing_carousel'
                break
            left, top, right, bottom = carousel
            width, height = right - left, bottom - top
            if width < 180 or height < 100:
                reason = 'missing_carousel'
                break
            if method == 'menu':
                x = left + width * 60 // 100
                start, end = top + height * 85 // 100, top + height * 45 // 100
                action = ('swipe', *map(str, (x, start, x, end)), '530')
            else:
                start, end = left + width * 78 // 100, left + width * 35 // 100
                y = top + height * 62 // 100
                action = ('swipe', *map(str, (start, y, end, y)), '520')
        try:
            if opening_menu:
                xml, menu = read_menu_transition(device, stop, action)
            else:
                xml, menu = moved_view(device, stop, action, .4), None
        except Exception:
            # ADB and UI automation use different exception types. Keep the verified names on I/O failure.
            reason = 'device_error'
            break
        if xml is None:
            reason = 'cancelled'
            break

        if method == 'menu':
            try:
                menu = parse_restaurant_menu(xml)
            except ValueError:
                reason = 'context_changed'
                break
        if menu is not None:
            count, visible, list_area = menu
            if count != expected:
                reason = 'count_changed'
                break
            if opening_menu and (visible[:len(previous)] != previous
                                 or len(set(visible)) != len(visible) or len(visible) > count):
                reason = 'unverified_transition'
                break
            method, carousel = 'menu', list_area
            if opening_menu:
                screens = 0  # Menu report counts the validated list screens.
        if method == 'carousel':
            try:
                current, carousel, _ = detail_view(xml, report['name'])
            except ValueError:
                reason = 'context_changed'
                break
            if (report.get('address_display') and current.get('address_display')
                    and current['address_display'] != report['address_display']):
                reason = 'context_changed'
                break
            if current['dish_count_display'] != expected:
                reason = 'count_changed'
                break
            visible = current['recommended_dishes']
            if opening_menu and visible != previous:
                reason = 'unverified_transition'
                break
            if opening_menu:
                continue

        # A suffix/prefix match proves the new window did not jump over cards.
        overlap = next((count for count in range(min(len(previous), len(visible)), 0, -1)
                        if previous[-count:] == visible[:count]), 0)
        additions = visible[overlap:]
        if not overlap or len(set(visible)) != len(visible) or any(dish in dishes for dish in additions) or (
                expected is not None and len(dishes) + len(additions) > expected):
            reason = 'unverified_transition'
            break
        dishes.extend(additions)
        previous = visible
        screens += 1
        if len(dishes) == expected:
            reason = 'expected_count_reached'
            break
        idle_screens = 0 if additions else idle_screens + 1
        if idle_screens >= 2:
            reason = 'no_new_dishes'
            break

    result = {'reason': reason, 'screens': screens, 'complete': reason == 'expected_count_reached'}
    if method == 'menu':
        result['method'] = 'menu'
    return result


def capture_restaurant_detail(device, output, name, *, recommendation_stop=None):
    device.ensure_foreground()
    captured = datetime.now(timezone.utc).isoformat()
    xml = device.snapshot()
    device.ensure_foreground()
    report, carousel, menu_link = detail_view(xml, name)
    if recommendation_stop is not None:
        scan = scan_recommendations(device, report, carousel, menu_link, recommendation_stop)
        report['recommendation_scan'] = scan
        if scan['reason'] == 'cancelled':
            report['status'] = 'cancelled'
        elif scan['reason'] == 'device_error':
            report.update(status='error', error='Android 页面读取中断，已保存本次核对的菜品。')
    folder = Path(output).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f'app-restaurant-detail-{uuid.uuid4().hex}.json'
    report.update(file=str(path), captured_at=captured)
    staged = path.with_suffix('.part')
    try:
        staged.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)
    return report
