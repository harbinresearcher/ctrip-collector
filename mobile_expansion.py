"""Optional single-card expansion in the observed hotel Android review layout."""
import logging
import re
import xml.etree.ElementTree as ET

from core import bounds, parse_reviews

logger = logging.getLogger(__name__)


def expansion_target(rows, screen):
    for row in rows:
        control = row.get('body_control')
        if not control or control[0] != '展开':
            continue
        left, top, right, bottom = control[1]
        if screen[0] <= left < right <= screen[2] and screen[1] <= top < bottom <= screen[3]:
            return row
    return None


def card_identity(row):
    return tuple(row[key] for key in ('author', 'date', 'rating', 'date_kind'))


def expand_card(adb, target, stop):
    logger.info('开始核对待展开的酒店评论')
    adb.ensure_foreground()
    before = adb.snapshot()
    root = ET.fromstring(before)
    screen_node = next((node for node in root.iter('node') if node.get('package') == 'ctrip.android.view'), None)
    if screen_node is None or not is_review_page(root):
        raise ValueError('展开前已离开酒店评论页')
    matches = [row for row in parse_reviews(before, 'hotel', locate_expansion=True)
               if card_identity(row) == card_identity(target) and row['content'] == target['content']]
    current = expansion_target(matches, bounds(screen_node)) if len(matches) == 1 else None
    if current is None:
        return 'unverified', None, None
    # Coordinates are taken from the fresh snapshot, not reused after scrolling.
    left, top, right, bottom = current['body_control'][1]
    if stop.is_set():
        raise InterruptedError('已停止评论展开')
    adb.ensure_foreground()
    adb.run('shell', 'input', 'tap', str((left + right) // 2), str((top + bottom) // 2))
    if stop.wait(0.8) or stop.is_set():
        raise InterruptedError('已停止评论展开')
    adb.ensure_foreground()
    after = adb.snapshot()
    if not is_review_page(ET.fromstring(after)):
        raise ValueError('展开后已离开酒店评论页，停止采集')
    rows = parse_reviews(after, 'hotel', locate_expansion=True)
    if stop.is_set():
        raise InterruptedError('已停止评论展开')
    candidates = [row for row in rows if card_identity(row) == card_identity(target)]
    if len(candidates) != 1:
        return 'unverified', after, None
    row = candidates[0]
    if row.get('body_control', ('',))[0] != '收起':
        return 'unverified', after, None
    prefix = re.sub(r'(?:…+|\.{3,})$', '', target['content'].rstrip())
    if len(prefix) < 20 or len(row['content']) <= len(target['content']) or not row['content'].startswith(prefix):
        return 'unverified', after, None
    logger.info('已从同一酒店评论卡片补采到更长正文')
    return 'accepted', after, row


def is_review_page(root):
    texts = {node.get('text', '') for node in root.iter('node') if node.get('package') == 'ctrip.android.view'}
    return ('全部' in texts and bool(texts & {'好评', '差评', '有图·视频'})
            and not texts & {'安全验证', '请完成验证', '拖动滑块完成拼图', '请先登录'})
