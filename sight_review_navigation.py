"""Open a known sight's review page using the observed Ctrip search controls."""

from xml.etree import ElementTree as ET

from core import bounds
from runner import AndroidReadinessError, sight_screen_title
from sight_snapshot import APP, parse_sight_snapshot


HOME_GUIDE = 'ctrip.android.view:id/home_grid_guide_widget'
DETAIL_TITLE = 'gs_spotdetail_view_name'
MANUAL_PROMPTS = {'安全验证', '请完成验证', '拖动滑块完成拼图', '请先登录'}
REVIEW_NOT_CONFIRMED = '未确认已进入目标景点的用户点评，请核对后重试。'


def text(node):
    return node.get('text', '').strip()


def app_nodes(root):
    return [node for node in root.iter('node') if node.get('package') == APP]


def visible_in(node, container):
    left, top, right, bottom = bounds(container)
    x1, y1, x2, y2 = bounds(node)
    return (node.get('package') == APP and node.get('enabled') == 'true'
            and node.get('visible-to-user') != 'false'
            and left <= x1 < x2 <= right and top <= y1 < y2 <= bottom)


def cancellation_check(stop):
    if stop.is_set():
        raise InterruptedError('已取消打开景点点评')


def read_screen(device, stop):
    cancellation_check(stop)
    device.ensure_foreground()
    cancellation_check(stop)
    try:
        root = ET.fromstring(device.snapshot())
    except ET.ParseError as exc:
        raise RuntimeError('无法读取携程页面，请等待页面加载后重试。') from exc
    if any(text(node) in MANUAL_PROMPTS for node in app_nodes(root)):
        raise ValueError('携程要求登录或验证，请在处理后重新打开采集页面。')
    return root


def wait_for(device, stop, find, message):
    for _ in range(12):
        try:
            root = read_screen(device, stop)
        except AndroidReadinessError as exc:
            if exc.state != 'focus_unknown':
                raise
        else:
            if (result := find(root)) is not None:
                return result
        if stop.wait(0.45):
            cancellation_check(stop)
    raise RuntimeError(message)


def tap(device, node, stop):
    cancellation_check(stop)
    device.ensure_foreground()
    cancellation_check(stop)
    left, top, right, bottom = bounds(node)
    device.run('shell', 'input', 'tap', str((left + right) // 2), str((top + bottom) // 2))


def home_search(root):
    window = root.find('node')
    nodes = app_nodes(root)
    if window is None or not any(node.get('resource-id') == HOME_GUIDE for node in nodes):
        return None
    entries = []
    for container in nodes:
        if tuple(node.get('class') for node in container) != (
                'android.widget.ImageView', 'android.widget.FrameLayout', 'android.widget.ImageView'):
            continue
        if not all(node.get('clickable') == 'true' and visible_in(node, container) and visible_in(node, window)
                   for node in container):
            continue
        left_icon, entry, right_icon = container
        if not bounds(left_icon)[2] <= bounds(entry)[0] < bounds(entry)[2] <= bounds(right_icon)[0]:
            continue
        labels = [node for node in entry.iter('node') if node.get('class') == 'android.widget.TextView'
                  and text(node) and visible_in(node, entry)]
        if len(labels) == 1:
            entries.append(entry)
    return entries[0] if len(entries) == 1 else None


def search_editor(root):
    window = root.find('node')
    if window is None:
        return None
    nodes = app_nodes(root)
    if not any(text(node) == '搜索' and node.get('clickable') == 'true' and visible_in(node, window)
               for node in nodes):
        return None
    editors = [node for node in nodes if node.get('class') == 'android.widget.EditText'
               and node.get('focused') == 'true' and visible_in(node, window)]
    return editors[0] if len(editors) == 1 else None


def compact_address(value):
    return ''.join(value.split())


def matching_sight_result(root, name, address):
    editor = search_editor(root)
    if editor is None or text(editor) != name:
        return None
    window = root.find('node')
    results = []
    for listing in app_nodes(root):
        if listing.get('class') != 'android.widget.ListView' or not visible_in(listing, window):
            continue
        for row in listing:
            if row.get('clickable') != 'true' or not visible_in(row, listing):
                continue
            labels = [n for n in row.iter('node') if n.get('class') == 'android.widget.TextView'
                      and visible_in(n, row)]
            titles = [n for n in labels if text(n) in (name, f'{name} 可订明日')]
            for label in labels:
                _, bar, location = text(label).partition('|')
                region, separator, suffix = location.rpartition(f'-{name}-')
                if len(titles) == 1 and bar and region.strip() and separator and compact_address(suffix) == address:
                    results.append(titles[0])
                    break
    if len(results) > 1:
        raise ValueError('搜索显示多个同名同地址结果，请手动打开目标景点点评。')
    return results[0] if results else None


def detail_review_entry(root, name, address):
    titles = [node for node in app_nodes(root) if node.get('resource-id') == DETAIL_TITLE]
    if not titles:
        return None
    if len(titles) != 1:
        raise ValueError('景点详情抬头不唯一，请手动打开目标景点点评。')
    title = titles[0]
    words = [text(node) for node in title if node.get('package') == APP and text(node)]
    if words != [name]:
        raise ValueError('景点详情名称与目录不符，请核对后重试。')
    try:
        detail = parse_sight_snapshot(ET.tostring(root, encoding='unicode'), name)
    except ValueError:
        return None
    location = detail.get('address_text')
    if not location:
        return None
    if compact_address(location) != address:
        raise ValueError('景点详情地址与目录不符，请核对后重试。')
    review = detail.get('review_display')
    if not review:
        return None
    header = next(node for node in root.iter('node') if title in node)
    window = root.find('node')
    buttons = [node for node in header.iter('node') if text(node) == review['count_text']
               and visible_in(node, window)]
    return buttons[0] if len(buttons) == 1 else None


def checked_reviews_title(root, name):
    title = sight_screen_title(root)
    window = root.find('node')
    if not title or window is None:
        return None
    labels = {text(node) for node in app_nodes(root) if visible_in(node, window)}
    if '全部' not in labels or not labels.intersection(('好评', '差评', '有图·视频')):
        return None
    if title != name:
        raise ValueError('打开的点评页与目录景点名称不符，请核对后重试。')
    return title


def open_sight_reviews(device, name, address, stop):
    if not isinstance(name, str) or not (name := name.strip()):
        raise ValueError('请填写景点名称。')
    start = read_screen(device, stop)
    if sight_screen_title(start) == name:
        if checked_reviews_title(start, name) is None:
            wait_for(device, stop, lambda root: checked_reviews_title(root, name), REVIEW_NOT_CONFIRMED)
        return 'already_open'
    entry = home_search(start)
    if entry is None:
        raise ValueError('请先在模拟器打开携程首页或目标景点的用户点评。')
    if not isinstance(address, str) or not (address := compact_address(address)):
        raise ValueError('目录缺少景点地址，请手动打开目标景点点评。')

    tap(device, entry, stop)
    wait_for(device, stop, search_editor, '未打开携程搜索框，请回到首页重试。')
    device.ensure_foreground()
    cancellation_check(stop)
    device.enter_text(name)
    candidate = wait_for(device, stop, lambda root: matching_sight_result(root, name, address),
                         '搜索未找到同时匹配名称和地址的景点，请手动打开目标点评。')
    tap(device, candidate, stop)
    reviews = wait_for(device, stop, lambda root: detail_review_entry(root, name, address),
                       '未找到地址和点评入口可核对的景点详情，请手动打开目标点评。')
    tap(device, reviews, stop)
    wait_for(device, stop, lambda root: checked_reviews_title(root, name), REVIEW_NOT_CONFIRMED)
    return 'opened'
