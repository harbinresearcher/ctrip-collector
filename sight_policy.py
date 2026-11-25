"""Parse fully visible policy cells using their actual enclosing rectangles."""
import logging
import xml.etree.ElementTree as ET
from core import bounds

logger = logging.getLogger(__name__)


def parse_policy(xml, layout=None):
    # /* ===== 步骤1：定位表格；数据源：携程UI树；操作：1) 核对三列表头 2) 获取可视边界 ===== */
    logger.info('开始定位优待政策表')
    root = ET.fromstring(xml)
    parents = {c: p for p in root.iter() for c in p}
    headers = [n for n in root.iter('node') if n.get('package') == 'ctrip.android.view' and n.get('text') == '适用人群']
    if not headers and layout:
        markers = [n for n in root.iter('node') if n.get('package') == 'ctrip.android.view' and n.get('resource-id') == 'gs_sightmoredetail_view_preferential']
        candidates = [c for m in markers for c in parents[m] if len(c) > 0 and c.get('package') == 'ctrip.android.view']
        if len(candidates) != 1:
            raise ValueError('无表头页面无法唯一定位政策表')
        table = candidates[0]
        if (bounds(table)[0], bounds(table)[2]) != tuple(layout['horizontal_bounds']):
            raise ValueError('政策表布局发生变化')
    elif len(headers) != 1:
        raise ValueError('未找到唯一政策表头')
    else:
        table = parents[headers[0]]
    nodes = [n for n in table if n.get('package') == 'ctrip.android.view']
    columns = []
    # 1.1 表头文字只用于定位列，不将屏幕宽度写死。
    for title in (('适用人群', '适用条件', '优待政策') if headers else ()):
        matches = [n for n in nodes if n.get('text') == title]
        if len(matches) != 1:
            raise ValueError('政策表列结构未知')
        columns.append(bounds(matches[0])[0])
    if not headers:
        columns = layout['columns']
    top, bottom = bounds(table)[1], bounds(table)[3]
    parent = table
    while parent in parents:
        parent = parents[parent]
        box = bounds(parent)
        if box[3] > box[1]:
            top, bottom = max(top, box[1]), min(bottom, box[3])
    header_bottom = bounds(headers[0])[3] if headers else top
    logger.info('政策表定位完成')
    # /* ===== 步骤2：关联单元格；数据源：空容器边框与文本；操作：1) 最小包含框 2) 跨行关联 ===== */
    logger.info('开始关联政策单元格')
    cells = [bounds(n) for n in nodes if not n.get('text') and bounds(n)[2] > bounds(n)[0]]
    groups, conditions, policies = [], [], []
    unresolved = 0
    for node in nodes:
        text = node.get('text', '').strip()
        box = bounds(node)
        if not text or text in {'适用人群', '适用条件', '优待政策'}:
            continue
        column = min(range(3), key=lambda i: abs(box[0]-columns[i]))
        if box[1] <= max(top, header_bottom) or box[3] >= bottom:
            unresolved += int(column == 1)
            continue
        # 2.1 使用最小包含框；跨行人群的单元格自然覆盖多个条件行。
        enclosing = [c for c in cells if c[0] <= box[0] and c[1] <= box[1] and c[2] >= box[2] and c[3] >= box[3]]
        if not enclosing:
            unresolved += int(column == 1)
            continue
        cell = min(enclosing, key=lambda c: (c[2]-c[0])*(c[3]-c[1]))
        if cell[1] <= top or cell[3] >= bottom:
            unresolved += int(column == 1)
            continue
        (groups, conditions, policies)[column].append((text, cell))
    rows = []
    for condition, cell in conditions:
        # 2.2 两侧必须唯一匹配，不猜测缺失或被遮挡的优惠。
        # 2.3 真机左右列存在1像素取整差异，容差不改变唯一匹配要求。
        group = [t for t, c in groups if c[1] <= cell[1] + 4 and c[3] >= cell[3] - 4]
        policy = [t for t, c in policies if abs(c[1]-cell[1]) <= 4 and abs(c[3]-cell[3]) <= 4]
        if len(group) == len(policy) == 1:
            rows.append({'group': group[0], 'condition': condition, 'policy': policy[0]})
        else:
            unresolved += 1
    # The ticket notes share the policy section's parent in the historical XML.
    # Text exposed by Android can extend outside the visible clip; it remains a page note, not a full row.
    notes = []
    section = parents.get(table)
    if section is not None and section.get('package') == 'ctrip.android.view':
        for sibling in section:
            if sibling.get('package') != 'ctrip.android.view' or sibling.get('resource-id') != 'gs_sightmoredetail_view_ticket':
                continue
            for node in sibling.iter('node'):
                text = node.get('text', '').strip()
                if node.get('package') == 'ctrip.android.view' and text.startswith(('补充说明：', '补充说明:')):
                    if note := text[len('补充说明：'):].strip():
                        notes.append(note)
    logger.info('政策单元格关联完成：%s行', len(rows))
    return {'rows': rows, 'complete': False, 'scope': 'fully_visible_cells_only',
            'supplementary_notes': list(dict.fromkeys(notes)), 'unresolved_condition_count': unresolved,
            'layout': {'columns': columns, 'horizontal_bounds': [bounds(table)[0], bounds(table)[2]]}}


def parse_policy_screen(xml, expected_name, layout=None):
    texts = [node.get('text', '').strip() for node in ET.fromstring(xml).iter('node')
             if node.get('package') == 'ctrip.android.view']
    if not expected_name or expected_name not in texts:
        raise ValueError('政策页面景点名称不匹配')
    return parse_policy(xml, layout)


def merge_policy_screens(screens, expected_name):
    # /* ===== 步骤1：合并政策页面；数据源：同景点连续屏幕；操作：1) 核对名称 2) 沿用列布局并去重 ===== */
    logger.info('开始合并政策屏幕')
    layout, rows, seen, notes = None, [], set(), {}
    for xml in screens:
        # 1.1 每一屏必须保留景点标题；无标题时不猜测所属对象。
        result = parse_policy_screen(xml, expected_name, layout)
        layout = result['layout']
        notes.update(dict.fromkeys(result['supplementary_notes']))
        for row in result['rows']:
            key = (row['group'], row['condition'], row['policy'])
            if key not in seen:
                seen.add(key)
                rows.append(row)
    logger.info('政策屏幕合并完成：%s行', len(rows))
    return {'name': expected_name, 'rows': rows, 'complete': False, 'scope': 'observed_screens_only',
            'supplementary_notes': list(notes)}
