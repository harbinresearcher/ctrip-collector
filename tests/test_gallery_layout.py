import logging
import unittest

from gallery_layout import viewer_region

logger = logging.getLogger(__name__)


def layout(scale=1, thumbnails=True):
    def node(kind, box, text=''):
        a,b,c,d=[round(v*scale) for v in box]
        return f'<node package="ctrip.android.view" class="android.widget.{kind}" text="{text}" bounds="[{a},{b}][{c},{d}]"/>'
    body=node('FrameLayout',(0,0,1440,3200))+node('TextView',(216,213,484,304),'AI优选 30')+node('TextView',(1279,2833,1395,2903),'9/30')+node('ImageView',(31,199,151,319))
    if thumbnails:
        for x in (207,424,641):
            body+=node('ImageView',(x,2443,x+188,2631))
    return '<hierarchy>'+body+'</hierarchy>'


class GalleryLayoutTests(unittest.TestCase):
    def test_region_follows_actual_device_bounds(self):
        # /* ===== 步骤1：验证布局适配；数据源：两种分辨率XML；操作：1) 排除头栏 2) 排除缩略图栏 ===== */
        logger.info('开始图集布局适配测试')
        self.assertEqual(viewer_region(layout(), 'AI优选'), (0,319,1440,2443))
        # 1.1 半分辨率设备使用自己的边界，不套用1440像素坐标。
        self.assertEqual(viewer_region(layout(.5), 'AI优选'), (0,160,720,1222))
        logger.info('图集布局适配测试完成')

    def test_missing_thumbnail_row_is_rejected(self):
        # /* ===== 步骤2：验证未知布局；数据源：缺少底部缩略图；操作：1) 不猜主图区 2) 明确报错 ===== */
        logger.info('开始未知图集布局测试')
        with self.assertRaises(ValueError):
            viewer_region(layout(thumbnails=False), 'AI优选')
        logger.info('未知图集布局测试完成')
