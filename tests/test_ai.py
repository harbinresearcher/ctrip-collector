import logging
import unittest
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from ai_capture import parse_ai, capture_ai

logger = logging.getLogger(__name__)


class AiTests(unittest.TestCase):
    def test_expand_waits_for_updated_page(self):
        # /* ===== 步骤1：验证异步展开；数据源：模拟页面序列；操作：1) 返回旧页 2) 返回展开页 ===== */
        logger.info('开始异步展开检查')
        expanded = (Path(__file__).parent / 'fixtures/hotel-ai-expanded.xml').read_text('utf-8')
        collapsed = expanded.replace('text="收起 ', 'text="查看全部总结 ')
        adb = Mock()
        adb.snapshot.side_effect = [collapsed, collapsed, expanded]
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        with TemporaryDirectory() as folder:
            # 1.1 点击后旧页面不能成为最终结果。
            result = capture_ai(adb, 'hotel', '酒店', '1', folder, stop, include_gallery=False)
            self.assertEqual(result['status'], 'expanded_visible')
            self.assertEqual(adb.snapshot.call_count, 3)
            adb.run.assert_called_once()
        logger.info('异步展开检查完成')

    def test_expand_wait_has_a_limit(self):
        # /* ===== 步骤2：验证等待上限；数据源：不更新的页面；操作：1) 保持旧页 2) 返回部分结果 ===== */
        logger.info('开始等待上限检查')
        xml = (Path(__file__).parent / 'fixtures/hotel-ai-expanded.xml').read_text('utf-8').replace('text="收起 ', 'text="查看全部总结 ')
        adb = Mock()
        adb.snapshot.return_value = xml
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        with TemporaryDirectory() as folder:
            # 2.1 有限重读，不重复点击，不误报完整。
            result = capture_ai(adb, 'hotel', '酒店', '1', folder, stop, include_gallery=False)
            self.assertEqual(result['status'], 'partial')
            self.assertEqual(adb.snapshot.call_count, 7)
            adb.run.assert_called_once()
        logger.info('等待上限检查完成')

    def test_cancel_during_snapshot_prevents_tap(self):
        # /* ===== 步骤0：核对取消；数据源：停止事件；操作：1) 读取中停止 2) 禁止后续点击 ===== */
        logger.info('开始取消检查')
        stop = Event()
        xml = (Path(__file__).parent / 'fixtures/hotel-ai-expanded.xml').read_text('utf-8').replace('text="收起 ', 'text="查看全部总结 ')
        adb = Mock()
        def snapshot():
            stop.set()
            return xml
        adb.snapshot.side_effect = snapshot
        with TemporaryDirectory() as folder:
            result = capture_ai(adb, 'hotel', '酒店', '1', folder, stop, include_gallery=False)
            self.assertEqual(result['status'], 'cancelled')
            adb.run.assert_not_called()
        logger.info('取消检查完成')

    def test_expanded_summary_preserves_sections_and_excludes_reviews(self):
        # /* ===== 步骤1：核对 AI 原文；数据源：真机展开 XML；操作：1) 提取分段 2) 核对边界 ===== */
        logger.info('开始 AI 原文检查')
        xml = (Path(__file__).parent / 'fixtures/hotel-ai-expanded.xml').read_text('utf-8')
        # 1.1 三段全部保留，反馈按钮不混入正文。
        result = parse_ai(xml)
        self.assertTrue(result['expanded_boundary_visible'])
        self.assertEqual(len(result['sections']), 3)
        self.assertIn('早餐的质量和种类', result['sections'][2]['text'])
        self.assertNotIn('有帮助', str(result['sections']))
        self.assertEqual(result['sections'][0]['linked_count'], '3372')
        self.assertEqual(result['ai_selected_images']['display_count'], 301)
        self.assertFalse(parse_ai(xml.replace('text="收起 ', 'text="继续 '))['expanded_boundary_visible'])
        with self.assertRaises(ValueError):
            parse_ai('<hierarchy><node text="普通评论"/></hierarchy>')
        logger.info('AI 原文检查完成')

