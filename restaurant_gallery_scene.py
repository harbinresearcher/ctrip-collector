"""Verify the restaurant review carousel and its native photo viewer."""
import re
from xml.etree import ElementTree as ET

from core import APP, bounds


_VIEW_GROUP = 'android.view.ViewGroup'
_RELATIVE_LAYOUT = 'android.widget.RelativeLayout'
_TEXT = 'android.widget.TextView'
_IMAGE = 'android.widget.ImageView'
_PAGE_FRACTION = re.compile(r'([1-9][0-9]{0,8})/([1-9][0-9]{0,8})')


def _is_app_node(node, cls=None):
    return (node is not None and node.tag == 'node' and node.get('package') == APP
            and node.get('visible-to-user') != 'false' and (cls is None or node.get('class') == cls))


def _window(xml):
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, TypeError) as exc:
        raise ValueError('图集页面 XML 无效') from exc
    if root.tag != 'hierarchy' or len(root) != 1 or not _is_app_node(root[0]):
        raise ValueError('未识别到唯一的携程图集窗口')
    return root[0]


def _inside(box, outer):
    left, top, right, bottom = box
    return outer[0] <= left < right <= outer[2] and outer[1] <= top < bottom <= outer[3]


def _label(node):
    return node.get('text', '').strip()


def _fraction(node, expected_total):
    if _is_app_node(node, _TEXT) and (match := _PAGE_FRACTION.fullmatch(_label(node))):
        index, total = (int(value) for value in match.groups())
        if index <= total and (expected_total is None or total == expected_total):
            return index, total
    return None


def _photo_tap_box(carousel):
    if len(carousel) != 1 or not _is_app_node(carousel[0], _VIEW_GROUP):
        return None
    area = bounds(carousel)
    photos = [bounds(photo) for photo in carousel[0]
              if _is_app_node(photo, _IMAGE) and _inside(bounds(photo), area)]
    if len(photos) != len(carousel[0]):
        return None
    # During the carousel transition, a neighboring photo can overlap the active one.
    # Tap the portion of the centered photo that no other image occupies.
    width = area[2] - area[0]
    center_twice = area[0] + area[2]
    centered = [i for i, (left, top, right, bottom) in enumerate(photos)
                if abs(left + right - center_twice) <= width // 5
                and right - left >= width * .7 and (top, bottom) == (area[1], area[3])]
    if len(centered) != 1:
        return None
    index = centered[0]
    left, top, right, bottom = photos[index]
    for i, (other_left, other_top, other_right, other_bottom) in enumerate(photos):
        if i == index or not (other_top < bottom and top < other_bottom):
            continue
        if other_left < left:
            left = max(left, other_right)
        else:
            right = min(right, other_left)
    return (left, top, right, bottom) if right - left >= width * .15 else None


def _review_note(carousel, parents, viewport, name, author, body, expected_total):
    cover = parents.get(carousel)
    column = parents.get(cover)
    scroll = parents.get(column)
    panel = parents.get(scroll)
    scene = parents.get(panel)
    if not (all(_is_app_node(node, _VIEW_GROUP) for node in (cover, column, panel, scene))
            and _is_app_node(scroll, 'android.widget.ScrollView') and list(scroll) == [column]
            and list(panel) == [scroll] and list(cover) == [carousel] and len(column) == 5
            and column[0] is cover):
        return None

    area = bounds(carousel)
    if not (_inside(bounds(column), viewport) and _inside(area, bounds(column)) and bounds(cover) == area):
        return None
    author_nodes = [node for node in scene if _is_app_node(node, _TEXT) and _label(node) == author
                    and _inside(bounds(node), viewport) and bounds(node)[3] <= area[1]]
    back_nodes = [node for node in scene if _is_app_node(node, _TEXT) and _label(node) == '\ue943'
                  and _inside(bounds(node), viewport) and bounds(node)[3] <= area[1]]
    if len(author_nodes) != 1 or len(back_nodes) != 1:
        return None
    if bounds(back_nodes[0])[2] > bounds(author_nodes[0])[0]:
        return None

    badge, _, business, review = column[1:]
    if not (_is_app_node(badge, _VIEW_GROUP) and len(badge) == 1
            and _inside(bounds(badge[0]), bounds(badge)) and _inside(bounds(badge), area)):
        return None
    position = _fraction(badge[0], expected_total)
    if not position or bounds(badge)[0] < (area[0] + area[2]) // 2:
        return None
    if not (_is_app_node(business, _VIEW_GROUP) and len(business) == 3
            and _is_app_node(business[0], _IMAGE) and _is_app_node(business[2], _IMAGE)
            and _is_app_node(business[1], _TEXT) and _label(business[1]) == name
            and _inside(bounds(business[1]), bounds(business))
            and _inside(bounds(business), bounds(column)) and area[3] <= bounds(business)[1]):
        return None
    if not (_is_app_node(review, _TEXT) and ''.join(_label(review).split()) == body
            and _inside(bounds(review), bounds(column)) and bounds(business)[3] <= bounds(review)[1]):
        return None
    box = _photo_tap_box(carousel)
    return (*position, box) if box is not None else None


def read_review_note(xml, name, author, body, expected_total=None):
    """Return the verified note page and an unobstructed tap rectangle."""
    if not all(isinstance(value, str) and value.strip() for value in (name, author, body)):
        raise ValueError('缺少点评作者、正文或餐厅名称')
    window = _window(xml)
    parents = {child: parent for parent in window.iter('node') for child in parent}
    viewport = bounds(window)
    body = ''.join(body.split())
    matches = []
    for node in window.iter('node'):
        if _is_app_node(node, 'android.widget.HorizontalScrollView'):
            match = _review_note(node, parents, viewport, name.strip(), author.strip(), body, expected_total)
            if match is not None:
                matches.append(match)
    if len(matches) != 1:
        raise ValueError('未核验到同一条餐厅点评的大图详情或页码')
    return matches[0]


def _viewer_button(toolbar, label, area):
    candidates = [node for node in toolbar.iter('node') if _is_app_node(node, 'android.widget.Button')
                  and node.get('content-desc') == label and node.get('clickable') == 'true'
                  and node.get('enabled') == 'true' and _inside(bounds(node), area)]
    return bounds(candidates[0]) if len(candidates) == 1 else None


def _read_native_view(pager, container, viewport, expected_total):
    area = bounds(pager)
    if not (_is_app_node(container, _RELATIVE_LAYOUT) and len(container) == 3 and pager in container
            and _inside(area, viewport) and area[:3] == viewport[:3] and bounds(container) == area
            and len(pager) == 1 and _is_app_node(pager[0], _RELATIVE_LAYOUT) and bounds(pager[0]) == area):
        return None
    if expected_total > 1 and pager.get('scrollable') != 'true':
        return None
    if len(pager[0]) != 2 or not _is_app_node(pager[0][0], 'android.widget.FrameLayout'):
        return None
    photo = pager[0][1]
    if not _is_app_node(photo, _IMAGE) or bounds(photo) != area:
        return None

    overlays = [node for node in container if node is not pager]
    if not all(_is_app_node(node, _RELATIVE_LAYOUT) and bounds(node) == area for node in overlays):
        return None
    page_labels = []
    toolbars = []
    for branch in overlays:
        close = _viewer_button(branch, '关闭', area)
        download = _viewer_button(branch, '下载', area)
        if close and download:
            toolbars.append((branch, close, download))
        for node in branch.iter('node'):
            position = _fraction(node, expected_total)
            if position and _inside(bounds(node), area) and bounds(node)[1] >= (area[1] + 3 * area[3]) // 4:
                index, total = position
                if node.get('content-desc') == f'当前在第{index}张，总共{total}张':
                    page_labels.append((branch, position))
    if len(page_labels) != 1 or len(toolbars) != 1:
        return None
    toolbar, close, download = toolbars[0]
    counter, position = page_labels[0]
    middle = (area[0] + area[2]) // 2
    if (toolbar is counter or not close[2] <= middle <= download[0]
            or not max(close[1], download[1]) < min(close[3], download[3]) <= (3 * area[1] + area[3]) // 4):
        return None
    return (*position, tuple(area), tuple(download))


def read_photo_viewer(xml, expected_total):
    """Return the native viewer's page, photo area, and enabled download button."""
    if type(expected_total) is not int or expected_total < 1:
        raise ValueError('图集照片总数无效')
    window = _window(xml)
    viewport = bounds(window)
    matches = []
    for parent in window.iter('node'):
        for node in parent:
            if _is_app_node(node, 'androidx.viewpager.widget.ViewPager'):
                match = _read_native_view(node, parent, viewport, expected_total)
                if match is not None:
                    matches.append(match)
    if len(matches) != 1:
        raise ValueError('未核验到全屏图集页码或下载按钮')
    return matches[0]
