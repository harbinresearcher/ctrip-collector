"""从已识别的携程图集XML提取主图区，未知布局拒绝猜测。"""
import logging
import re
import xml.etree.ElementTree as ET
from collections import defaultdict

from core import bounds
from gallery_capture import gallery_position

logger = logging.getLogger(__name__)


def viewer_region(xml, category):
    # /* ===== 步骤1：识别主图区边界；数据源：当前XML；操作：1) 定位分类头栏 2) 定位缩略图行 ===== */
    logger.info('开始识别图集主图区')
    gallery_position(xml, category)
    nodes = list(ET.fromstring(xml).iter('node'))
    boxes = [(node, bounds(node)) for node in nodes]
    # 1.1 屏幕范围取当前窗口，要求从左上角开始且尺寸有效。
    windows = [box for node, box in boxes if node.get('class') == 'android.widget.FrameLayout' and tuple(box[:2]) == (0, 0)]
    if not windows:
        raise ValueError('缺少图集窗口边界')
    _, _, width, height = max(windows, key=lambda box: box[2] * box[3])
    if width <= 0 or height <= 0:
        raise ValueError('图集窗口尺寸无效')
    labels = [box for node, box in boxes if re.fullmatch(re.escape(category) + r'\s+\d+', node.get('text', '').strip())]
    if len(labels) != 1 or labels[0][3] > height * .3:
        raise ValueError('图集分类头栏位置未知')
    header = labels[0]
    top = header[3]
    rows = defaultdict(set)
    for node, box in boxes:
        if node.get('class') != 'android.widget.ImageView':
            continue
        left, upper, right, lower = box
        # 1.2 同一头栏中的关闭按钮也排除；仅接受界内的小图标。
        if 0 <= left < right <= width and 0 < right-left <= width*.15 and 0 < lower-upper <= height*.06 and upper <= header[3] and lower >= header[1] and lower <= header[3] + height*.03:
            top = max(top, lower)
        # 1.3 底部至少三个同高缩略图才可作为边界，避免误用单个图片按钮。
        if 0 <= left < right <= width and height*.5 < upper < lower <= height and right-left <= width*.25 and lower-upper <= height*.12:
            rows[(upper, lower)].add((left, right))
    thumbnail_rows = [row for row, cells in rows.items() if len(cells) >= 3]
    if len(thumbnail_rows) != 1 or thumbnail_rows[0][0] <= top:
        raise ValueError('图集缩略图栏位置未知')
    region = (0, top, width, thumbnail_rows[0][0])
    logger.info('图集主图区识别完成：%s', region)
    return region
