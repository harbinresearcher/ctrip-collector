"""Record review controls visible in the observed Android layouts."""
import re
from decimal import ROUND_HALF_UP, Decimal

from mobile_review_fields import SHOPPING_DIMENSION, SHOPPING_RATING_FIELDS, SHOPPING_SCORE_VALUE

APP = 'ctrip.android.view'
FILTERS = {'全部', '好评', '中评', '差评', '有图·视频', '图片/视频', '消费后评价'}
RESTAURANT_FILTERS = FILTERS | {'图/视频', '带图'}
SHOPPING_FILTERS = FILTERS | {'只看笔记'}
COUNT = re.compile(r'(?:[0-9]+(?:\.[0-9]+)?|[0-9]{1,3}(?:,[0-9]{3})+)[万千wW]?\+?')
EXACT_COUNT = re.compile(r'(?:[0-9]{1,12}|[0-9]{1,3}(?:,[0-9]{3}){1,3})')
COUNT_SCALE = {'万': 10000, 'w': 10000, 'W': 10000, '千': 1000}


def active(node):
    return node.get('selected') == 'true' or node.get('checked') == 'true'


def normalize_count(text):
    """把筛选计数归一成整数：「4.3w」→43000、「3.4万」→34000、「588」→588；不匹配返回 None。"""
    if not COUNT.fullmatch(text):
        return None
    number = text.rstrip('+')
    scale = COUNT_SCALE.get(number[-1], 1)
    if scale != 1:
        number = number[:-1]
    return int((Decimal(number.replace(',', '')) * scale).to_integral_value(rounding=ROUND_HALF_UP))


def tab_selected(group, label_node, count_node):
    """标签页是否选中：只认显式 selected/checked。装饰节点不能证明哪个筛选生效，不做猜测。"""
    return active(group) or active(label_node) or active(count_node)


def adjacent_filter_entries(nodes, labels):
    """同一父节点下相邻的两个 TextView（标签 + 计数）；返回 (标签, 原文, 整数计数, 是否选中)。"""
    entries = []
    for group in nodes:
        children = list(group)
        for label_node, count_node in zip(children, children[1:]):
            if (label_node.get('package') != APP or count_node.get('package') != APP
                    or len(label_node) or len(count_node)):
                continue
            label = (label_node.get('text') or '').strip()
            count = (count_node.get('text') or '').strip()
            if label not in labels or not COUNT.fullmatch(count):
                continue
            entries.append((label, count, normalize_count(count), tab_selected(group, label_node, count_node)))
    return entries


def shopping_review_overview(nodes, parents):
    labels = [node for node in nodes if node.get('text', '').strip() == '总评分' and node in parents]
    if len(labels) != 1:
        return {}
    label = labels[0]
    row = [child for child in parents[label] if child.get('package') == APP]
    values = [child.get('text', '').strip() for child in row[row.index(label) + 1:]]
    if not values or not re.fullmatch(SHOPPING_SCORE_VALUE, values[0]):
        return {}
    dimensions = {}
    for dimension in SHOPPING_RATING_FIELDS:
        scores = {float(match[2]) for word in values[1:]
                  if (match := SHOPPING_DIMENSION.fullmatch(word)) and match[1] == dimension}
        if len(scores) == 1:
            dimensions[dimension] = scores.pop()
    return {'overall_score': float(values[0]), 'rating_dimensions': dimensions}


def read_review_context(root, kind):
    parents = {child: parent for parent in root.iter() for child in parent}
    nodes = [node for node in root.iter('node') if node.get('package') == APP]
    if kind == 'shopping':
        filters, selected = {}, []
        for node in nodes:
            text = node.get('text', '').strip()
            parent = parents.get(node)
            if (parent is None or parent.get('package') != APP or parent.get('content-desc', '').strip() != text
                    or len(parts := text.rsplit(maxsplit=1)) != 2):
                continue
            label, count = parts
            if label in SHOPPING_FILTERS and COUNT.fullmatch(count):
                filters.setdefault((label, count), {'label': label, 'display_count': count})
                if active(node) or active(parent):
                    selected.append(label)
        context = {'visible_filters': list(filters.values()),
                   'selected_filter': selected[0] if len(selected) == 1 else None,
                   'latest_control_visible': False, 'sort_order': None}
        if overview := shopping_review_overview(nodes, parents):
            context['shopping_review_overview'] = overview
        return context
    hotel_filter_rows = {parents.get(node) for node in nodes
                         if node.get('resource-id') == 'htl_x_htlcmtlist_tab_SubTab_exposure'}
    filters, selected = {}, []
    # 3.3 真机景点筛选用「标签 + 计数」两个相邻 TextView（全部 4.3w），不带 filter1 嵌套；先按相邻关系收一遍。
    if kind == 'sight':
        for label, text, value, is_selected in adjacent_filter_entries(nodes, FILTERS):
            filters.setdefault((label, text), {'label': label, 'display_count': value,
                                                'display_count_text': text})
            if is_selected and label not in selected:
                selected.append(label)
    for group in nodes:
        identifier = group.get('resource-id', '')
        if kind == 'hotel':
            header = parents.get(group)
            if header is None or header.get('package') != APP or header not in hotel_filter_rows:
                continue
        elif kind not in ('sight', 'restaurant') or identifier != 'o_gs_comments_newlist_filter1':
            continue
        children = [node for node in group if node.get('package') == APP and node.get('text', '').strip()]
        if len(children) != 2:
            continue
        label, count = (node.get('text').strip() for node in children)
        labels = RESTAURANT_FILTERS if kind == 'restaurant' else FILTERS
        if label not in labels or not COUNT.fullmatch(count):
            continue
        if kind == 'hotel' and [text.strip() for text in group.get('content-desc', '').split(',', 1)] != [label, count]:
            continue
        if kind == 'sight':
            filters.setdefault((label, count), {'label': label, 'display_count': normalize_count(count),
                                                'display_count_text': count})
        else:
            filters.setdefault((label, count), {'label': label, 'display_count': count})
        if active(group) or active(children[0]):
            if label not in selected:
                selected.append(label)
    latest_id = {'hotel': 'htl_x_htlcmtlist_tab_tag_exposure',
                 'restaurant': 'o_gs_comments_newlist_filter2'}.get(kind)
    latest = [node for node in nodes if latest_id
              and node.get('resource-id') == latest_id
              and any(child.get('package') == APP and child.get('text', '').strip() == '最新' for child in node)]
    explicit_latest = len(latest) == 1 and (active(latest[0]) or any(
        child.get('package') == APP and child.get('text', '').strip() == '最新' and active(child)
        for child in latest[0]))
    return {'visible_filters': list(filters.values()), 'selected_filter': selected[0] if len(selected) == 1 else None,
            'latest_control_visible': bool(latest), 'sort_order': '最新' if explicit_latest else None}


def exact_count(count):
    """筛选计数是否是可比较的精确整数；新路径存整数，旧路径存原文。"""
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    return int(count.replace(',', '')) if isinstance(count, str) and EXACT_COUNT.fullmatch(count) else None


def summarize_review_count(selected_filter, selection_confirmed, visible_counts, unique_count):
    if unique_count is None:
        state, display_total = 'current_run_count_unavailable', None
    elif not selected_filter or not selection_confirmed:
        state, display_total = 'selection_unconfirmed', None
    elif not visible_counts:
        state, display_total = 'display_count_unavailable', None
    elif None in {exact_count(count) for count in visible_counts}:
        state, display_total = 'approximate_count', None
    else:
        totals = {exact_count(count) for count in visible_counts}
        state = 'compared_display_only' if len(totals) == 1 else 'count_not_stable'
        display_total = next(iter(totals)) if len(totals) == 1 else None
    return {'scope': 'current_run_fingerprints_vs_app_filter_label',
            'filter': selected_filter, 'current_run_unique': unique_count,
            'display_count_labels': sorted(str(count) for count in visible_counts),
            'display_total': display_total,
            'state': state, 'difference': display_total - unique_count if display_total is not None else None}
