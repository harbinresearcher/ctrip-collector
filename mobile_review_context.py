"""Record review controls visible in the observed Android layouts."""
import re

from mobile_review_fields import SHOPPING_DIMENSION, SHOPPING_RATING_FIELDS, SHOPPING_SCORE_VALUE

APP = 'ctrip.android.view'
FILTERS = {'全部', '好评', '中评', '差评', '有图·视频', '图片/视频', '消费后评价'}
RESTAURANT_FILTERS = FILTERS | {'图/视频', '带图'}
SHOPPING_FILTERS = FILTERS | {'只看笔记'}
COUNT = re.compile(r'(?:[0-9]+(?:\.[0-9]+)?|[0-9]{1,3}(?:,[0-9]{3})+)[万千]?\+?')
EXACT_COUNT = re.compile(r'(?:[0-9]{1,12}|[0-9]{1,3}(?:,[0-9]{3}){1,3})')


def active(node):
    return node.get('selected') == 'true' or node.get('checked') == 'true'


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
        filters.setdefault((label, count), {'label': label, 'display_count': count})
        if active(group) or active(children[0]):
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


def summarize_review_count(selected_filter, selection_confirmed, visible_counts, unique_count):
    if unique_count is None:
        state, display_total = 'current_run_count_unavailable', None
    elif not selected_filter or not selection_confirmed:
        state, display_total = 'selection_unconfirmed', None
    elif not visible_counts:
        state, display_total = 'display_count_unavailable', None
    elif any(not EXACT_COUNT.fullmatch(count) for count in visible_counts):
        state, display_total = 'approximate_count', None
    else:
        totals = {int(count.replace(',', '')) for count in visible_counts}
        state = 'compared_display_only' if len(totals) == 1 else 'count_not_stable'
        display_total = next(iter(totals)) if len(totals) == 1 else None
    return {'scope': 'current_run_fingerprints_vs_app_filter_label',
            'filter': selected_filter, 'current_run_unique': unique_count,
            'display_count_labels': sorted(visible_counts), 'display_total': display_total,
            'state': state, 'difference': display_total - unique_count if display_total is not None else None}
