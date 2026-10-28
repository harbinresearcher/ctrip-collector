import logging
import unittest
from unittest.mock import Mock, patch

from gallery_entry import capture_ai_gallery
from test_gallery_layout import layout

logger = logging.getLogger(__name__)
GRID = '<hierarchy><node package="ctrip.android.view" class="android.widget.FrameLayout" bounds="[0,0][1440,3200]"/><node text="住客晒图"/><node text="官方相册"/><node text="AI优选 30" bounds="[84,443][291,524]"/><node class="android.widget.ImageView" bounds="[0,573][1440,1533]"/></hierarchy>'


class GalleryEntryTests(unittest.TestCase):
    def test_baseline_precedes_navigation_and_starts_first_page(self):
        # /* ===== 步骤1：验证启动顺序；数据源：网格与首张XML；操作：1) 先记录基线 2) 再打开首图 ===== */
        logger.info('开始图集入口测试')
        adb = Mock()
        adb.snapshot.side_effect = [GRID, GRID, layout().replace('9/30', '1/30')]
        stop = Mock(is_set=lambda: False, wait=lambda _: False)
        events=[]
        adb.run.side_effect=lambda *args: events.append('tap')
        # 1.1 缓存基线必须早于点击，不能漏掉点击触发的预加载。
        with patch('gallery_entry.cache_snapshot', side_effect=lambda _: events.append('baseline') or {'old': (1,1)}), patch('gallery_entry.collect_gallery_pages', return_value={'status':'page_limit'}) as collect:
            result=capture_ai_gallery(adb,'427100','out',stop,max_pages=2)
            self.assertEqual(events,['baseline','tap','tap'])
            self.assertEqual(result['status'],'page_limit')
            self.assertIsNone(collect.call_args.args[2])
            self.assertEqual(collect.call_args.kwargs['max_pages'],2)
        logger.info('图集入口测试完成')

    def test_wrong_first_page_and_initial_cancel_do_not_collect(self):
        # /* ===== 步骤2：验证启动边界；数据源：非首张与取消；操作：1) 不从未知位置开始 2) 取消不点击 ===== */
        logger.info('开始图集入口边界测试')
        adb=Mock()
        adb.snapshot.side_effect=[GRID,GRID,layout()]
        stop=Mock(is_set=lambda:False,wait=lambda _:False)
        with patch('gallery_entry.cache_snapshot', return_value={}), patch('gallery_entry.collect_gallery_pages') as collect:
            with self.assertRaises(ValueError):
                capture_ai_gallery(adb,'427100','out',stop)
            collect.assert_not_called()
            # 2.1 初始取消不得操作设备。
            adb.reset_mock()
            stop.is_set=lambda:True
            with self.assertRaises(InterruptedError):
                capture_ai_gallery(adb,'427100','out',stop)
            adb.run.assert_not_called()
        logger.info('图集入口边界测试完成')
