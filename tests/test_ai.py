import json
import logging
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from ai_capture import parse_ai, capture_ai

logger = logging.getLogger(__name__)


class AiTests(unittest.TestCase):
    def test_rating_dimensions_follow_screen_columns_and_exclude_external_nodes(self):
        xml = (Path(__file__).parent / 'fixtures/hotel-ai-expanded.xml').read_text('utf-8')
        root = ET.fromstring(xml)
        parents = {child: parent for parent in root.iter() for child in parent}
        score = next(node for node in root.iter('node') if node.get('text') == '4.6'
                     and node.get('bounds') == '[1271,463][1365,527]')
        parent = parents[score]
        # The DOM order in this real screen already differs from the display order.
        parent.insert(list(parent).index(score), ET.Element('node', {
            'package': 'other.app', 'class': 'android.widget.TextView', 'text': '1.0',
            'bounds': '[1271,463][1365,527]'}))
        parsed = parse_ai(ET.tostring(root, encoding='unicode'))
        self.assertEqual(parsed['rating_dimensions'], [
            {'label': '卫生', 'score_text': '4.7'}, {'label': '环境', 'score_text': '4.7'},
            {'label': '服务', 'score_text': '4.7'}, {'label': '设施', 'score_text': '4.6'},
        ])
        duplicate = ET.Element('node', score.attrib | {'text': '2.0'})
        parent.insert(list(parent).index(score), duplicate)
        parsed = parse_ai(ET.tostring(root, encoding='unicode'))
        self.assertEqual([row['label'] for row in parsed['rating_dimensions']], ['卫生', '环境', '服务'])

    def test_topic_counts_belong_to_their_paragraph_and_cannot_be_replaced_by_unrelated_number(self):
        xml = (Path(__file__).parent / 'fixtures/hotel-ai-expanded.xml').read_text('utf-8')
        root = ET.fromstring(xml)
        parents = {child: parent for parent in root.iter() for child in parent}
        first_count = next(node for node in root.iter('node') if node.get('text', '').strip() == '3372')
        group = parents[first_count]
        parent = parents[group]
        parent.insert(list(parent).index(group) + 1,
                      ET.Element('node', {'text': '9999', 'package': 'ctrip.android.view'}))
        result = parse_ai(ET.tostring(root, encoding='unicode'))
        self.assertEqual([part['topic'] for part in result['sections']], ['设施与活动', '卫生与环境', '客人建议'])
        self.assertEqual([part['linked_count'] for part in result['sections']], ['3372', '3175', '63'])
        self.assertEqual(result['linked_review_scope'], 'display_counts_only')
        first_count.set('text', '3,372')
        result = parse_ai(ET.tostring(root, encoding='unicode'))
        self.assertEqual(result['sections'][0]['linked_count'], '3,372')
        first_count.set('package', 'other.app')
        result = parse_ai(ET.tostring(root, encoding='unicode'))
        self.assertIsNone(result['sections'][0]['linked_count'])

    def test_summary_excludes_other_apps(self):
        # /* ===== 步骤1：核验设备页面；数据源：受控 XML 变体；操作：1) 排除其他应用 2) 点击前复核前台 ===== */
        logger.info('开始检查 AI 总结页面来源')
        expanded = (Path(__file__).parent / 'fixtures/hotel-ai-expanded.xml').read_text('utf-8')
        # 1.1 即使有少量携程节点，其他应用提供的总结文本也不能被采集。
        alien = expanded.replace('package="ctrip.android.view"', 'package="other.app"')
        alien = alien.replace('</hierarchy>', '<node package="ctrip.android.view" text="其他"/></hierarchy>')
        with self.assertRaises(ValueError):
            parse_ai(alien)
        logger.info('AI 总结页面来源检查完成')

    def test_summary_checks_foreground_before_tap(self):
        # /* ===== 步骤1：核验点击前台；数据源：切换后的设备；操作：1) 确认前台 2) 禁止误点击 ===== */
        logger.info('开始检查 AI 展开点击前台')
        expanded = (Path(__file__).parent / 'fixtures/hotel-ai-expanded.xml').read_text('utf-8')
        # 1.2 手机在读屏后切出携程时，不得向其他应用发送点击。
        collapsed = expanded.replace('text="收起 ', 'text="查看全部总结 ')
        adb = Mock()
        adb.snapshot.return_value = collapsed
        adb.ensure_foreground.side_effect = [None, RuntimeError('携程不在前台')]
        with TemporaryDirectory() as folder, self.assertRaises(RuntimeError):
            capture_ai(adb, 'hotel', '酒店', '1', folder, include_gallery=False)
        adb.run.assert_not_called()
        logger.info('AI 展开点击前台检查完成')

    def test_foreign_overlay_does_not_end_valid_summary(self):
        # /* ===== 步骤1：核验混合窗口；数据源：受控页面；操作：1) 加入其他应用边界 2) 保留携程总结 ===== */
        logger.info('开始混合窗口总结检查')
        xml = ('<hierarchy><node package="ctrip.android.view" text="点评总结"/>'
               '<node package="ctrip.android.view" text="由AI生成"/>'
               '<node package="other.app" text="收起"/>'
               '<node package="ctrip.android.view" text="设施:真实内容"/>'
               '<node package="ctrip.android.view" text="收起"/></hierarchy>')
        result = parse_ai(xml)
        self.assertEqual([part['text'] for part in result['sections']], ['设施:真实内容'])
        self.assertEqual(result['status'], 'expanded_visible')
        logger.info('混合窗口总结检查完成')

    def test_interrupted_second_write_preserves_first_summary(self):
        # /* ===== 步骤1：验证中断保护；数据源：已有 AI 摘要；操作：1) 注入二次写失败 2) 保留首份 ===== */
        logger.info('开始 AI 摘要二次写入保护测试')
        xml = (Path(__file__).parent / 'fixtures/hotel-ai-expanded.xml').read_text('utf-8')
        adb = Mock()
        adb.snapshot.return_value = xml
        original = Path.write_text
        attempts = 0

        def fail_second(path, data, *args, **kwargs):
            nonlocal attempts
            # 1.1 写入中断发生在文件已打开并截断之后。
            if path.name.endswith('.json') or '.json.' in path.name:
                attempts += 1
                if attempts == 2:
                    original(path, '{', encoding='utf-8')
                    raise OSError('模拟磁盘故障')
            return original(path, data, *args, **kwargs)

        with TemporaryDirectory() as folder, patch.object(Path, 'write_text', new=fail_second), \
             patch('ai_capture.capture_gallery_metadata', return_value={'metadata_status':'captured'}):
            with self.assertRaises(OSError):
                capture_ai(adb, 'hotel', '酒店', '1', folder)
            files = list((Path(folder) / 'ai').glob('*.json'))
            self.assertEqual(len(files), 1)
            saved = json.loads(files[0].read_text('utf-8'))
            self.assertEqual(saved['status'], 'expanded_visible')
            self.assertEqual(len(saved['sections']), 3)
            self.assertEqual(saved['ai_selected_images']['status'], 'not_captured')
        logger.info('AI 摘要二次写入保护测试完成')

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

