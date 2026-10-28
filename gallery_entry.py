"""从酒店住客图集网格启动AI优选图片采集。"""
import logging
import xml.etree.ElementTree as ET

from ai_capture import parse_gallery
from core import bounds
from gallery_batch import collect_gallery_pages
from gallery_cache import cache_snapshot
from gallery_capture import gallery_position

logger = logging.getLogger(__name__)


def capture_ai_gallery(adb, resource_id, output, stop, *, max_pages=30):
    # /* ===== 步骤1：验证网格入口；数据源：用户打开的住客图集；操作：1) 验证酒店ID 2) 点击前记录缓存 ===== */
    logger.info('开始准备AI图集采集')
    if not resource_id.isdigit() or int(resource_id) <= 0 or not 1 <= max_pages <= 500:
        raise ValueError('请填写正整数酒店ID和1至500页采集上限')
    if stop.is_set():
        raise InterruptedError('图集采集已取消')
    adb.ensure_foreground()
    xml = adb.snapshot()
    metadata = parse_gallery(xml)
    nodes = list(ET.fromstring(xml).iter('node'))
    labels = [n for n in nodes if n.get('text', '').strip() == f"AI优选 {metadata['selected_count']}"]
    if len(labels) != 1:
        raise ValueError('请打开住客晒图网格，并让AI优选分类可见')
    baseline = cache_snapshot(adb)
    # 1.1 按当前文字节点点击分类，不沿用历史坐标。
    def tap(box):
        if stop.is_set():
            raise InterruptedError('图集采集已取消')
        adb.ensure_foreground()
        left, top, right, bottom = box
        if not 0 <= left < right or not 0 <= top < bottom:
            raise ValueError('图集点击位置无效')
        adb.run('shell','input','tap',str((left+right)//2),str((top+bottom)//2))
        if stop.wait(1.5):
            raise InterruptedError('图集采集已取消')
    tap(bounds(labels[0]))
    logger.info('AI图集采集基线准备完成')

    # /* ===== 步骤2：打开首图；数据源：刷新后的网格；操作：1) 找首个可见大图 2) 验证首张再连续采集 ===== */
    logger.info('开始打开AI图集首张')
    adb.ensure_foreground()
    xml = adb.snapshot()
    metadata = parse_gallery(xml)
    nodes = list(ET.fromstring(xml).iter('node'))
    windows = [bounds(n) for n in nodes if n.get('class') == 'android.widget.FrameLayout' and bounds(n)[:2] == [0,0]]
    if not windows:
        raise ValueError('无法识别图集网格窗口')
    _, _, width, height = max(windows,key=lambda b:b[2]*b[3])
    labels = [bounds(n) for n in nodes if n.get('text','').strip() == f"AI优选 {metadata['selected_count']}"]
    if len(labels) != 1:
        raise ValueError('AI图集分类不可见')
    # 2.1 小缩略图和背景排除；若网格停在中间，打开后页码检查会拒绝继续。
    images = [bounds(n) for n in nodes if n.get('class') == 'android.widget.ImageView']
    images = [b for b in images if 0 <= b[0] < b[2] <= width and labels[0][3] < b[1] < b[3] <= height
              and b[2]-b[0] >= width*.45 and b[3]-b[1] >= height*.08]
    if not images:
        raise ValueError('请将AI优选网格滚动到顶部，确保第一张图片可见')
    tap(min(images,key=lambda b:(b[1],b[0])))
    adb.ensure_foreground()
    if gallery_position(adb.snapshot(),'AI优选') != (1,metadata['selected_count']):
        raise ValueError('打开的不是AI图集首张，请回到网格顶部后重试')
    logger.info('AI图集首张已打开')
    return collect_gallery_pages(adb,baseline,None,output,'hotel-'+resource_id,'AI优选',stop,max_pages=max_pages)
