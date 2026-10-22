import json
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock

from ai_capture import capture_ai, parse_gallery, capture_gallery_metadata

logger = logging.getLogger(__name__)
FIXTURES = Path(__file__).parent / 'fixtures'


class GalleryTests(unittest.TestCase):
    def test_cancel_during_foreground_check_prevents_gallery_tap(self):
        # /* ===== 步骤0：核对检查中取消；数据源：停止事件；操作：1) 前台检查时取消 2) 禁止点击 ===== */
        logger.info('开始前台检查取消回归')
        adb = Mock()
        stop = Event()
        adb.ensure_foreground.side_effect = stop.set
        with TemporaryDirectory() as folder:
            # 0.1 检查返回后必须重新核对取消。
            result = capture_gallery_metadata(
                adb, (FIXTURES / 'hotel-ai-expanded.xml').read_text('utf-8'),
                Path(folder) / 'sample', stop)
            self.assertEqual(result['metadata_status'], 'cancelled')
            adb.run.assert_not_called()
        logger.info('前台检查取消回归完成')

    def test_unrecognized_gallery_stops_without_back(self):
        # /* ===== 步骤0：核对等待边界；数据源：未跳转页面；操作：1) 有界重试 2) 禁止误返回 ===== */
        logger.info('开始图集等待边界检查')
        adb = Mock()
        adb.snapshot.return_value = (FIXTURES / 'hotel-ai-expanded.xml').read_text('utf-8')
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        with TemporaryDirectory() as folder:
            # 0.1 只有入口点击；六次无图集即停止并保留总结。
            result = capture_ai(adb, 'hotel', '酒店', '1', folder, stop)
            self.assertEqual(result['ai_selected_images']['metadata_status'], 'timeout')
            self.assertEqual(len(result['sections']), 3)
            self.assertEqual(adb.snapshot.call_count, 7)
            adb.run.assert_called_once()
        logger.info('图集等待边界检查完成')

    def test_real_gallery_counts_are_not_image_items(self):
        # /* ===== 步骤1：核对真实图集；数据源：真机XML；操作：1) 提取分类 2) 保留原图缺口 ===== */
        logger.info('开始图集分类检查')
        result = parse_gallery((FIXTURES / 'hotel-ai-gallery.xml').read_text('utf-8'))
        # 1.1 AI优选数量必须来自分类标签。
        self.assertEqual(result['selected_count'], 30)
        self.assertEqual(result['visible_category_counts']['房间'], 41)
        self.assertEqual(result['items'], [])
        self.assertEqual(result['status'], 'not_captured')
        with self.assertRaises(ValueError):
            parse_gallery('<hierarchy><node text="AI优选 30"/></hierarchy>')
        logger.info('图集分类检查完成')

    def test_capture_saves_gallery_and_returns_to_summary(self):
        # /* ===== 步骤2：核对完整链路；数据源：模拟设备与真实XML；操作：1) 进入图集 2) 保存并返回 ===== */
        logger.info('开始图集链路检查')
        summary = (FIXTURES / 'hotel-ai-expanded.xml').read_text('utf-8')
        gallery = (FIXTURES / 'hotel-ai-gallery.xml').read_text('utf-8')
        adb = Mock()
        adb.snapshot.side_effect = [summary, gallery]
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        with TemporaryDirectory() as folder:
            # 2.1 入口总量和AI分类数量保持独立。
            result = capture_ai(adb, 'hotel', '酒店', '1', folder, stop)
            self.assertEqual(result['ai_selected_images']['display_count'], 301)
            self.assertEqual(result['ai_selected_images']['selected_count'], 30)
            self.assertTrue(Path(result['ai_selected_images']['evidence']).exists())
            adb.run.assert_any_call('shell', 'input', 'keyevent', 'KEYCODE_BACK')
            saved = json.loads(next((Path(folder) / 'ai').glob('*.json')).read_text('utf-8'))
            self.assertEqual(saved['ai_selected_images']['metadata_status'], 'captured')
        logger.info('图集链路检查完成')

    def test_gallery_failure_keeps_summary(self):
        # /* ===== 步骤3：核对失败保存；数据源：读取异常；操作：1) 图集失败 2) 总结仍保存 ===== */
        logger.info('开始图集失败检查')
        adb = Mock()
        adb.snapshot.side_effect = [(FIXTURES / 'hotel-ai-expanded.xml').read_text('utf-8'),
                                    RuntimeError('UI unavailable')]
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        with TemporaryDirectory() as folder:
            # 3.1 图集错误不能抹掉已取得的总结。
            result = capture_ai(adb, 'hotel', '酒店', '1', folder, stop)
            self.assertEqual(len(result['sections']), 3)
            self.assertEqual(result['ai_selected_images']['metadata_status'], 'error')
            self.assertTrue(list((Path(folder) / 'ai').glob('*.json')))
        logger.info('图集失败检查完成')

    def test_cancelled_capture_does_not_enter_gallery(self):
        # /* ===== 步骤4：核对取消；数据源：已停止事件；操作：1) 取消 2) 禁止设备操作 ===== */
        logger.info('开始图集取消检查')
        stop = Event()
        stop.set()
        adb = Mock()
        with TemporaryDirectory() as folder:
            # 4.1 取消前不读取、不点击。
            self.assertEqual(capture_ai(adb, 'hotel', '酒店', '1', folder, stop)['status'], 'cancelled')
            adb.snapshot.assert_not_called()
            adb.run.assert_not_called()
        logger.info('图集取消检查完成')
