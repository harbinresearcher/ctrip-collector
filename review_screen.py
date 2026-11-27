"""判断「手机现在是不是停在这个对象的点评列表上」。

导航是手机采集里最容易出错的一步：应用商店式的搜索结果会先给套餐/商品页，
点偏一次就跑到别的页。这里把「到了没到」写成可判定的三条：

1. 屏幕上出现点评列表锚点（`o_gs_comments*` 或 `htl_x_htlcmtlist*`）；
2. 屏幕文本里能看到对象名（酒店点评页不放名字时只记录、不因此判失败）；
3. 能看到「全部」计数（景点/餐饮是「全部 + 数字」，酒店是「N条」）。

三条齐全才算到达；缺哪条就报哪条，不做「大概到了」的猜测。
"""
import re
import xml.etree.ElementTree as ET

from layout_fingerprint import REVIEW_ANCHORS

COUNT = re.compile(r'([0-9]+(?:\.[0-9]+)?|[0-9]{1,3}(?:,[0-9]{3})+)[万千wW]?\+?')
HOTEL_TOTAL = re.compile(r'([0-9]+(?:\.[0-9]+)?[万千wW]?)\s*条')
SCALE = {'万': 10000, 'w': 10000, 'W': 10000, '千': 1000}


def count_int(text):
    """「4.3w」→43000、「5722」→5722；不是计数返回 None。"""
    if not isinstance(text, str):
        return None
    match = COUNT.fullmatch(text.strip().rstrip('+'))
    if not match:
        return None
    raw = match.group(1).replace(',', '')
    tail = text.strip().rstrip('+')[-1]
    scale = SCALE.get(tail, 1)
    try:
        return int(round(float(raw) * scale))
    except ValueError:
        return None


def _anchor(node):
    return (node.get('resource-id') or '').rsplit(':', 1)[-1].startswith(REVIEW_ANCHORS)


def _texts(root):
    return [(node.get('text') or '').strip() for node in root.iter('node') if (node.get('text') or '').strip()]


def _all_count(root, texts):
    """点评总数：优先「全部 + 数字」的相邻组合，其次是酒店页的「N条」。"""
    parents = {child: parent for parent in root.iter() for child in parent}
    nodes = [node for node in root.iter('node') if (node.get('text') or '').strip()]
    for index, node in enumerate(nodes[:-1]):
        if (node.get('text') or '').strip() != '全部':
            continue
        sibling = nodes[index + 1]
        if parents.get(node) is not parents.get(sibling):
            continue
        value = count_int((sibling.get('text') or '').strip())
        if value is not None:
            return value, (sibling.get('text') or '').strip()
    for text in texts:
        match = HOTEL_TOTAL.fullmatch(text)
        if match:
            value = count_int(match.group(1))
            if value is not None:
                return value, text
    return None, None


def review_screen_state(xml, expected_name=None):
    # /* ===== 步骤1：判断是否停在目标点评页；数据源：屏幕 XML；操作：1) 锚点 2) 名字 3) 全部计数 ===== */
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {'arrived': False, 'reason': 'invalid_xml', 'anchor': False, 'name_seen': None,
                'all_count': None, 'all_count_text': None, 'texts': []}
    texts = _texts(root)
    anchor = any(_anchor(node) for node in root.iter('node'))
    name_seen = None
    if expected_name:
        expected = expected_name.strip()
        name_seen = any(expected == text or expected in text or text in expected for text in texts)
    total, total_text = _all_count(root, texts)
    if not anchor:
        reason = 'no_review_anchor'
    elif name_seen is False:
        reason = 'name_mismatch'
    elif total is None:
        reason = 'no_all_count'
    else:
        reason = 'arrived'
    return {'arrived': reason == 'arrived', 'reason': reason, 'anchor': anchor, 'name_seen': name_seen,
            'all_count': total, 'all_count_text': total_text, 'texts': texts[:12]}