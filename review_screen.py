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


SEPARATORS = ' ·（(【[-—~～'


def _name_state(texts, expected, allow_missing_name):
    """返回 (name_seen, name_match, matched_text)。

    规则按真机踩过的坑定：
    - 完全相等 → exact；
    - 以目标名开头、紧跟分隔符（门店名/分店名常见写法）→ prefix，例如
      「琼州糟粕醋·海南酸汤火锅(清水湾东榕环球美食店)」对目标「琼州糟粕醋」；
    - 目标名只出现在**中间** → contains，判失败：把「汉拿山温泉」认成
      「长白山万达瑞景汉拿山温泉酒店」会把别的对象的数据记错账；
    - 页面上根本没有名字 → absent（酒店点评页确实不显示店名，调用方决定是否接受）。
    """
    if not expected:
        return None, None, None
    expected = expected.strip()
    exact = [item for item in texts if item == expected]
    if exact:
        return True, 'exact', exact[0]
    prefix = [item for item in texts
              if item.startswith(expected) and len(item) > len(expected) and item[len(expected)] in SEPARATORS]
    if prefix:
        return True, 'prefix', prefix[0]
    middle = [item for item in texts if item != expected and expected in item]
    if middle:
        return False, 'contains', middle[0]
    if allow_missing_name:
        return None, 'absent', None
    return False, 'none', None


def review_screen_state(xml, expected_name=None, allow_missing_name=False):
    # /* ===== 步骤1：判断是否停在目标点评页；数据源：屏幕 XML；操作：1) 锚点 2) 名字 3) 全部计数 ===== */
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {'arrived': False, 'reason': 'invalid_xml', 'anchor': False, 'name_seen': None,
                'name_match': None, 'matched_text': None,
                'all_count': None, 'all_count_text': None, 'texts': []}
    texts = _texts(root)
    anchor = any(_anchor(node) for node in root.iter('node'))
    name_seen, name_match, matched_text = _name_state(texts, expected_name, allow_missing_name)
    total, total_text = _all_count(root, texts)
    if not anchor:
        reason = 'no_review_anchor'
    elif name_match == 'contains':
        reason = 'name_partial_match'
    elif name_match == 'none':
        reason = 'name_mismatch'
    elif total is None:
        reason = 'no_all_count'
    else:
        reason = 'arrived'
    return {'arrived': reason == 'arrived', 'reason': reason, 'anchor': anchor, 'name_seen': name_seen,
            'name_match': name_match, 'matched_text': matched_text,
            'all_count': total, 'all_count_text': total_text, 'texts': texts[:12]}