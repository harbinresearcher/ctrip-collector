"""Read hotel header and room cards from the observed Android detail layout."""
import hashlib
import json
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

from core import bounds

logger = logging.getLogger(__name__)
APP = 'ctrip.android.view'
SOURCE = 'android_hotel_detail_snapshot'
DETAIL_PREFIX = 'htl_hoteldetail_rootNode_DetailPage_'
ROOM_PREFIX = 'htl_x_dtl_rmlist_mbRmCard_exposure'
OFFER_PREFIX = 'htl_x_dtl_rmlist_rmCard_promotionBt_exposure'
ROOM_KEYS = ('title', 'bed_text', 'area_text', 'occupancy_text', 'floor_text')


def nodes(root, prefix=''):
    return [node for node in root.iter('node') if node.get('package') == APP
            and node.get('resource-id', '').startswith(prefix)]


def texts(items):
    return [text for node in items if node.get('package') == APP
            and (text := node.get('text', '').strip())]


def one(values):
    values = list(values)
    return values[0] if len(values) == 1 else None


def room_filters(detail, parents):
    options = nodes(detail, 'htl_x_comp_filter_list_card_exposure')
    button = one(node for node in nodes(detail, 'htl_x_comp_fastfilter_filterBt_exposure')
                 if '筛选' in texts(node))
    if button is None:
        return []
    groups = dict.fromkeys(parents[option] for option in options)
    group = one(candidate for candidate in groups
                if (scroller := parents.get(candidate)) is not None
                and scroller.get('class') == 'android.widget.HorizontalScrollView'
                and parents.get(scroller) is parents[button])
    if group is None:
        return []
    rows = []
    for option in options:
        if parents[option] is not group:
            continue
        label = one(node for node in option if node.get('package') == APP and node.get('text', '').strip())
        if label is None or (text := label.get('text').strip()) == '筛选':
            continue
        selected = any(node.get('selected') == 'true' or node.get('checked') == 'true' for node in (option, label))
        rows.append({'label': text, 'selected': True if selected else None})
    return rows


def room_cards(root):
    cards = []
    for card in nodes(root, ROOM_PREFIX):
        image = one(child for child in card if child.get('package') == APP
                    and child.get('resource-id', '').startswith('htl_hoteldetail_roomList_RoomImage_'))
        info = one(child for child in card if child.get('package') == APP and child is not image)
        if image is None or info is None:
            continue
        offers = [child for child in info if child.get('package') == APP
                  and child.get('resource-id', '').startswith(OFFER_PREFIX)]
        groups = [words for child in info if child.get('package') == APP and child not in offers
                  and (words := [text for text in texts(nodes(child)) if any(char.isalnum() for char in text)])]
        if not groups:
            continue
        title = one(groups[0])
        if not title:
            continue
        attributes = groups[1] if len(groups) > 1 else []
        row = {'title': title}
        for key, pattern in (
            ('bed_text', r'[0-9]+张.+床'), ('area_text', r'[0-9]+(?:\.[0-9]+)?\s*(?:㎡|平方米)'),
            ('occupancy_text', r'[0-9]+人入住'), ('floor_text', r'[0-9]+(?:[-–—][0-9]+)?层')
        ):
            if match := one(text for text in attributes if re.fullmatch(pattern, text)):
                row[key] = match
        # The room layout places features after dimensions; offers are read only in the labeled subtree.
        if len(groups) == 3:
            features = groups[2]
            row['features'] = [text for text in features if re.fullmatch(r'[\u4e00-\u9fff]{1,12}', text)]
        for offer in offers:
            labels = texts(offer)
            price = one(label for label in labels if re.fullmatch(r'[¥￥][0-9]+(?:\.[0-9]{1,2})?起', label))
            if not price:
                continue
            display = {'starting_price_display': price}
            if label := one(value for value in labels if value == '特惠一口价'):
                display['offer_label_display'] = label
            if discount := one(value for value in labels if re.fullmatch(r'优惠[0-9]+(?:\.[0-9]{1,2})?', value)):
                display['discount_display'] = discount
            if display not in row.setdefault('offer_displays', []):
                row['offer_displays'].append(display)
        cards.append(row)
    return cards


def parse_hotel_snapshot(xml, expected_name):
    logger.info('开始核对手机酒店详情及房型')
    root = ET.fromstring(xml)
    parents = {child: parent for parent in root.iter() for child in parent}
    detail = one(nodes(root, DETAIL_PREFIX))
    if detail is None:
        raise ValueError('没有识别到携程酒店详情页')
    review = one(nodes(detail, 'htl_hoteldetail_hotelInfoCard_CommentInfoWrapper_'))
    address = one(nodes(detail, 'htl_hoteldetail_hotelInfoCard_AddressWrapper_'))
    header = parents.get(review)
    address_parent = parents.get(address)
    if (header is None or header.get('package') != APP or address_parent is None
            or address_parent.get('package') != APP
            or (address_parent is not header and parents.get(address_parent) is not header)):
        raise ValueError('未识别到酒店名称所在的资料卡，请回到酒店详情顶部')
    title = next(iter(texts(header)), '').replace('\ufffc', '').strip()
    if not expected_name or title != expected_name:
        raise ValueError('酒店名称与详情页抬头不一致')
    report = {'name': title, 'source_method': SOURCE, 'identity_verification': 'screen_header_name_only',
              'raw_hash': hashlib.sha256(xml.encode('utf-8')).hexdigest(), 'complete': False,
              'limitations': ['仅读取已显示的酒店资料和房型，未核对平台对象编号。',
                              '酒店与房型起价分别记录，均为页面展示，不代表实时报价或库存。']}
    score_labels = texts(review)
    score = one(text for text in score_labels if re.fullmatch(r'[0-5](?:\.[0-9])?', text) and float(text) <= 5)
    count = one(text for text in score_labels if re.fullmatch(r'[0-9]+(?:\.[0-9]+)?[万千]?\+?条', text))
    if score and count:
        report['review_display'] = {'score_text': score, 'count_text': count}
        between = score_labels[score_labels.index(score) + 1:score_labels.index(count)]
        if label := one(text for text in between if re.fullmatch(r'[\u4e00-\u9fff]{1,8}', text)):
            report['review_display']['label'] = label
    if quote := one(text for text in score_labels if re.fullmatch(r'“[^”]+”', text)):
        report['review_snippet_display'] = quote
    address_labels = texts(address)
    if distance := one(text for text in address_labels
                       if re.fullmatch(r'距.+?\d+(?:\.\d+)?\s*(?:公里|千米|米)', text)):
        report['distance_display'] = distance
    addresses = [text for text in address_labels if text != '地图' and not text.startswith('距')
                 and any(char.isalnum() for char in text)]
    if addr := one(addresses):
        report['address_text'] = addr
    facilities = nodes(header, 'htl_hoteldetail_hotelInfoCard_FacilityRecommend_qrjsxf')
    report['facility_highlights'] = list(dict.fromkeys(
        text for facility in facilities for text in texts(nodes(facility)) if any(char.isalnum() for char in text)))
    report['opening_labels'] = [text for text in texts(nodes(header)) if re.fullmatch(r'[0-9]{4}年(?:开业|装修)', text)]
    bar = one(nodes(detail, 'htl_x_dtl_btbar_exposure'))
    if bar is not None:
        price = texts(bar)
        if len(price) == 3 and price[0] in ('¥', '￥') and re.fullmatch(r'[0-9]+(?:\.[0-9]{1,2})?', price[1]) and price[2] == '起':
            report['hotel_starting_price'] = ''.join(price)
    calendar = one(nodes(detail, 'htl_hoteldetail_calendar_Calendar_wqcojr'))
    if calendar is not None:
        label = ''.join(text for text in texts(calendar) if re.fullmatch(r'[0-9]+|月|日|-', text))
        if re.fullmatch(r'[0-9]{1,2}月[0-9]{1,2}日-[0-9]{1,2}月[0-9]{1,2}日', label):
            report['stay_dates_visible'] = label
        date_filter = parents[calendar]
        if date_filter.get('resource-id') == 'htl_c_app_dtl_filter_exposure':
            nights = one(child for child in date_filter
                         if child.get('package') == APP
                         and child.get('resource-id', '').startswith('htl_hoteldetail_calendar_NightCount_'))
            if nights is not None:
                count = one(match for text in texts(nights)
                            if (match := re.fullmatch(r'共([1-9][0-9]{0,2})晚', text)))
                if count:
                    report['stay_nights_visible'] = int(count[1])
    report['room_filter_options'] = room_filters(detail, parents)
    report['room_cards'] = room_cards(detail)
    logger.info('手机酒店详情读取完成：%s张房型卡', len(report['room_cards']))
    return report


def scroll_room_list(device, xml, stop):
    detail = one(nodes(ET.fromstring(xml), DETAIL_PREFIX))
    if detail is None:
        return False
    scroller = one(node for node in nodes(detail) if node.get('class') == 'android.widget.ScrollView'
                   and node.get('scrollable') == 'true' and nodes(node, ROOM_PREFIX))
    if scroller is None:
        return False
    left, top, right, bottom = bounds(scroller)
    height = bottom - top
    if right <= left or height < 200 or stop.is_set():
        return False
    # Start inside a visible room but clear of the overlaid hotel footer.
    usable_bottom = bottom - height * 8 // 100
    starts = []
    for card in nodes(scroller, ROOM_PREFIX):
        card_left, card_top, card_right, card_bottom = bounds(card)
        x_left, x_right = max(left, card_left), min(right, card_right)
        y_top, y_bottom = max(top, card_top), min(usable_bottom, card_bottom)
        if x_right - x_left >= 40 and y_bottom - y_top >= 40:
            starts.append(((x_left + x_right) // 2, (y_top + y_bottom) // 2))
    if not starts:
        return False
    x, start = max(starts, key=lambda point: point[1])
    end = max(top + height * 16 // 100, start - height * 31 // 100)
    if start - end < 40:
        return False
    device.ensure_foreground()
    if stop.is_set():
        return False
    device.run('shell', 'input', 'swipe', str(x), str(start), str(x), str(end), '500')
    return True


def filter_selection(report):
    return frozenset(row['label'] for row in report['room_filter_options'] if row['selected'])


def capture_hotel_snapshot(device, output, name, stop, *, max_pages=1, emit=lambda _: None):
    logger.info('开始保存手机酒店详情')
    if type(max_pages) is not int or not 1 <= max_pages <= 20:
        raise ValueError('房型采集屏数须为1至20')
    folder = Path(output)
    path = folder / f'app-hotel-{uuid.uuid4().hex}.json'
    temporary = path.with_suffix('.part')

    def save():
        folder.mkdir(parents=True, exist_ok=True)
        try:
            temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    report = None
    rooms, hashes, context = {}, set(), {}
    no_change = 0
    reason = 'page_limit'
    for index in range(max_pages):
        if stop.is_set():
            reason = 'cancelled'
            break
        try:
            device.ensure_foreground()
            timestamp = datetime.now(timezone(timedelta(hours=8))).isoformat()
            xml = device.snapshot()
            device.ensure_foreground()
            if stop.is_set():
                reason = 'cancelled'
                break
            current = parse_hotel_snapshot(xml, name)
        except (ValueError, ET.ParseError) as exc:
            if report is None:
                raise
            reason = 'hotel_context_changed'
            report['stop_detail'] = str(exc)
            break
        except Exception as exc:
            if report is None:
                raise
            reason = 'error'
            report['error'] = str(exc)
            break
        digest = current['raw_hash']
        if digest in hashes:
            reason = 'repeated_screen'
            break
        hashes.add(digest)
        observed = {key: current[key] for key in ('address_text', 'stay_dates_visible', 'stay_nights_visible')
                    if key in current}
        if selected := filter_selection(current):
            observed['room_filter_selection'] = selected
        if any(key in context and context[key] != value for key, value in observed.items()):
            reason = 'hotel_context_changed'
            break
        context.update(observed)
        current_rooms = current.pop('room_cards')
        if report is None:
            report = current
            report.update(captured_at=timestamp, report_file=str(path.resolve()), pages=[], room_cards=[])
        new_count, changed = 0, False
        for room in current_rooms:
            key = tuple(room.get(field) for field in ROOM_KEYS)
            if key not in rooms:
                rooms[key] = room
                report['room_cards'].append(room)
                new_count += 1
                changed = True
                continue
            previous = rooms[key]
            for field in ('features', 'offer_displays'):
                for observation in room.get(field, []):
                    if observation not in previous.setdefault(field, []):
                        previous[field].append(observation)
                        changed = True
        report['pages'].append({'captured_at': timestamp, 'raw_hash': digest,
                                'observed_room_count': len(current_rooms), 'new_room_count': new_count})
        report.update(status='running', reason='reading_screens')
        save()
        emit(f'已保存第 {index + 1} 屏 · {len(rooms)} 种房型展示')
        no_change = 0 if changed else no_change + 1
        if index + 1 == max_pages:
            break
        if no_change >= 2:
            reason = 'no_new_rooms'
            break
        try:
            if not current_rooms or not scroll_room_list(device, xml, stop):
                reason = 'cancelled' if stop.is_set() else 'room_scroll_unavailable'
                break
            if stop.wait(1.2):
                reason = 'cancelled'
                break
        except Exception as exc:
            reason = 'error'
            report['error'] = str(exc)
            break
    if report is None:
        raise InterruptedError('手机酒店详情读取已取消')
    status = reason if reason in {'error', 'cancelled'} else 'partial'
    report.update(status=status, reason=reason)
    save()
    logger.info('手机酒店详情保存完成')
    return report
