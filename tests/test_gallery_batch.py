import json
import logging
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from gallery_batch import collect_gallery_pages
from test_gallery_capture import viewer
from test_gallery_layout import layout

logger = logging.getLogger(__name__)


class GalleryBatchTests(unittest.TestCase):
    def test_automatic_region_is_derived_from_current_page(self):
        # /* ===== 步骤1：验证自动主图区；数据源：当前布局；操作：1) 不传坐标 2) 使用识别结果采集 ===== */
        logger.info('开始分页自动布局测试')
        adb = Mock()
        adb.snapshot.return_value = layout(.5)
        # 1.1 每页按实际XML识别，不沿用其他分辨率的坐标。
        with tempfile.TemporaryDirectory() as folder, patch('gallery_batch.GalleryCandidateSession', return_value=nullcontext(Mock())), patch('gallery_batch.capture_current_gallery_frame', return_value={'status': 'no_match'}) as capture:
            result = collect_gallery_pages(adb, {}, None, folder, 'hotel-1', 'AI优选', Event(), max_pages=1)
            self.assertEqual(result['status'], 'page_limit')
            self.assertEqual(capture.call_args.args[2], (0,160,720,1222))
        logger.info('分页自动布局测试完成')

    def test_last_page_does_not_swipe_and_wait_cancel_keeps_page(self):
        # /* ===== 步骤1：验证停止边界；数据源：末页与翻页等待取消；操作：1) 末页不滑动 2) 取消保留记录 ===== */
        logger.info('开始末页与取消验证')
        for index, cancelled, expected in [(30, False, 'end_reached'), (4, True, 'cancelled')]:
            adb = Mock()
            adb.snapshot.return_value = viewer(index)
            stop = Mock(is_set=lambda: False, wait=lambda seconds: cancelled)
            # 1.1 到达末页不等于完整采集；中途取消也保留刚采的页。
            with tempfile.TemporaryDirectory() as folder, patch('gallery_batch.GalleryCandidateSession', return_value=nullcontext(Mock())), patch('gallery_batch.capture_current_gallery_frame', return_value={'status': 'no_match'}):
                result = collect_gallery_pages(adb, {}, (0, 40, 240, 400), folder, 'hotel-1', 'AI优选', stop)
                self.assertEqual(result['status'], expected)
                self.assertEqual(len(result['pages']), 1)
                self.assertFalse(result['complete'])
                self.assertEqual(adb.run.call_count, int(cancelled))
        logger.info('末页与取消验证完成')

    def test_missing_page_is_recorded_and_later_page_is_collected(self):
        # /* ===== 步骤1：验证连续采集；数据源：模拟两页；操作：1) 保留缺失页 2) 保存后续进度 ===== */
        logger.info('开始图集分页测试')
        adb = Mock()
        adb.snapshot.side_effect = [viewer(1), viewer(1), viewer(2), viewer(2)]
        stop = Mock(is_set=lambda: False, wait=lambda seconds: False)
        with tempfile.TemporaryDirectory() as folder, patch('gallery_batch.GalleryCandidateSession', return_value=nullcontext(Mock())), patch('gallery_batch.capture_current_gallery_frame', side_effect=[{'status': 'no_match'}, {'status': 'saved', 'gallery_index': 2}]):
            # 1.1 单页没有匹配时继续，页数预算用完后停止，不标记图集完整。
            result = collect_gallery_pages(adb, {}, (0, 40, 240, 400), folder, 'hotel-1', 'AI优选', stop, max_pages=2)
            self.assertEqual(result['status'], 'page_limit')
            self.assertEqual([p['index'] for p in result['pages']], [1, 2])
            self.assertFalse(result['complete'])
            self.assertEqual(adb.run.call_count, 1)
            self.assertEqual(json.loads(next(Path(folder).glob('gallery-run-*.json')).read_text(encoding='utf-8'))['pages'], result['pages'])
        logger.info('图集分页测试完成')

    def test_stalled_page_stops_without_repeated_swiping(self):
        # /* ===== 步骤2：验证翻页停滞；数据源：固定页码；操作：1) 发现页码未变 2) 停止重试 ===== */
        logger.info('开始图集停滞测试')
        adb = Mock()
        adb.snapshot.return_value = viewer(4)
        stop = Mock(is_set=lambda: False, wait=lambda seconds: False)
        with tempfile.TemporaryDirectory() as folder, patch('gallery_batch.GalleryCandidateSession', return_value=nullcontext(Mock())), patch('gallery_batch.capture_current_gallery_frame', return_value={'status': 'no_match'}) as capture:
            # 2.1 不允许重复采同一页直到次数耗尽。
            result = collect_gallery_pages(adb, {}, (0, 40, 240, 400), folder, 'hotel-1', 'AI优选', stop, max_pages=3)
            self.assertEqual(result['status'], 'page_not_advanced')
            self.assertEqual(capture.call_count, 1)
            self.assertEqual(adb.run.call_count, 1)
        logger.info('图集停滞测试完成')

    def test_error_preserves_progress_and_initial_cancel_does_not_touch_device(self):
        # /* ===== 步骤3：验证中断；数据源：设备异常和取消；操作：1) 保留成功页 2) 取消不操作手机 ===== */
        logger.info('开始图集中断测试')
        adb = Mock()
        adb.snapshot.side_effect = [viewer(1), viewer(1), viewer(2)]
        stop = Mock(is_set=lambda: False, wait=lambda seconds: False)
        with tempfile.TemporaryDirectory() as folder, patch('gallery_batch.GalleryCandidateSession', return_value=nullcontext(Mock())), patch('gallery_batch.capture_current_gallery_frame', side_effect=[{'status': 'no_match'}, RuntimeError('disconnected')]):
            result = collect_gallery_pages(adb, {}, (0, 40, 240, 400), folder, 'hotel-1', 'AI优选', stop, max_pages=3)
            self.assertEqual(result['status'], 'error')
            self.assertEqual(len(result['pages']), 1)
            # 3.1 已取消的任务只写取消状态，不接触设备。
            adb.reset_mock()
            cancelled = Event()
            cancelled.set()
            result = collect_gallery_pages(adb, {}, (0, 40, 240, 400), folder, 'hotel-1', 'AI优选', cancelled)
            self.assertEqual(result['status'], 'cancelled')
            adb.ensure_foreground.assert_not_called()
        logger.info('图集中断测试完成')
