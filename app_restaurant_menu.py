"""Read the dedicated restaurant recommendation page in Ctrip for Android."""
import re
from xml.etree import ElementTree as ET

from core import bounds
from sight_guide import text
from sight_snapshot import APP


WEB_VIEW = 'android.webkit.WebView'
VIEW = 'android.view.View'
TEXT_VIEW = 'android.widget.TextView'
MENU_TITLE = '推荐菜'
DISPLAY_COUNT = re.compile(r'\(([1-9][0-9]{0,4})\)')


def app_view(node, class_name):
    return node.tag == 'node' and node.get('package') == APP and node.get('class') == class_name


def visible_bounds(node):
    left, top, right, bottom = bounds(node)
    if node.get('visible-to-user') != 'false' and left < right and top < bottom:
        return left, top, right, bottom
    return None


def overlaps(node, area):
    box = visible_bounds(node)
    if box is None:
        return False
    left, top, right, bottom = area
    return left < box[2] and box[0] < right and top < box[3] and box[1] < bottom


def dish_name(card, area):
    if not app_view(card, VIEW) or len(card) < 2 or not overlaps(card, area):
        return None
    # The first column is the dish photo. The second starts with the name and may include a description.
    photo, details = card[:2]
    if not app_view(photo, VIEW) or not app_view(details, VIEW) or not len(details):
        return None
    photo_box, info_box = visible_bounds(photo), visible_bounds(details)
    title = details[0]
    if (photo_box and info_box and photo_box[2] <= info_box[0]
            and app_view(title, TEXT_VIEW) and overlaps(title, area)
            and (name := text(title)) and any(char.isalnum() for char in name)):
        return name
    return None


def parse_restaurant_menu(xml) -> tuple[int, list[str], tuple[int, int, int, int] | None]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError('推荐菜页面读取失败') from exc

    pages = [node for node in root.iter('node') if app_view(node, WEB_VIEW)
             and text(node) == MENU_TITLE and visible_bounds(node)]
    if len(pages) != 1 or len(pages[0]) != 1:
        raise ValueError('未核验到推荐菜专页')
    layout = pages[0][0]
    if not app_view(layout, VIEW) or len(layout) < 2:
        raise ValueError('推荐菜专页缺少列表')
    header, listing = layout[:2]
    header_box, list_box = visible_bounds(header), visible_bounds(listing)
    if not app_view(header, VIEW) or not app_view(listing, VIEW) or not header_box or not list_box:
        raise ValueError('推荐菜专页缺少表头或列表')
    labels = [text(node) for node in header if app_view(node, TEXT_VIEW) and overlaps(node, header_box)]
    if len(labels) != 2 or labels[0] != MENU_TITLE or not (match := DISPLAY_COUNT.fullmatch(labels[1])):
        raise ValueError('未核验到推荐菜展示总数')
    total = int(match[1])

    # The fixed header and floating add button overlap the list's reported bounds.
    left, top, right, bottom = list_box
    top = max(top, header_box[3])
    button_tops = [box[1] for section in layout[2:] if app_view(section, VIEW)
                   for button in section if app_view(button, VIEW) and (box := visible_bounds(button))
                   if any(app_view(label, TEXT_VIEW) and text(label) == '新增推荐菜'
                          and overlaps(label, box) for label in button)]
    bottom = min([bottom, *button_tops])
    if top >= bottom:
        raise ValueError('推荐菜列表不可见')
    area = (left, top, right, bottom)
    names = [name for card in listing if (name := dish_name(card, area))]
    if not names or len(names) > total:
        raise ValueError('未核验到可见推荐菜卡片')
    at_end = any(app_view(node, TEXT_VIEW) and text(node) == '没有更多了' and overlaps(node, area)
                 for node in listing)
    return total, names, area if listing.get('scrollable') == 'true' and not at_end else None

