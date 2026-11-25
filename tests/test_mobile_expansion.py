import csv
import logging
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

from core import parse_reviews
from runner import collect
from workbench_preview import read_scoped_mobile_preview

logger = logging.getLogger(__name__)
FIXTURE = Path(__file__).parent / 'fixtures' / 'hotel-comments2.xml'


def pages():
    collapsed = FIXTURE.read_text('utf-8')
    body = parse_reviews(collapsed, 'hotel')[0]['content']
    root = ET.fromstring(collapsed)
    parents = {child: parent for parent in root.iter() for child in parent}
    node = next(node for node in root.iter('node') if node.get('text') == body)
    control = next(node for node in parents[node].iter('node') if node.get('text') == '展开')
    control.set('text', '收起')
    fuller = body + '。这是展开后新增的内容。'
    node.set('text', fuller)
    return collapsed, ET.tostring(root, encoding='unicode'), fuller


class MobileExpansionTests(unittest.TestCase):
    def test_existing_target_can_refresh_and_expand_without_adding_duplicate(self):
        logger.info('开始验证已达标的手机点评重新读取')
        before, after, content = pages()
        with tempfile.TemporaryDirectory() as folder:
            adb = Mock()
            adb.snapshot.return_value = before
            first = collect(adb, 'hotel', '酒店', folder, limit=1, max_swipes=0, object_id='123')
            self.assertEqual(first['count'], 1)
            adb.reset_mock()
            adb.snapshot.side_effect = [before, before, after]
            result = collect(adb, 'hotel', '酒店', folder, limit=1, max_swipes=0,
                             object_id='123', expand_reviews=True, refresh=True,
                             stop=Mock(is_set=lambda: False, wait=lambda _: False))
            self.assertEqual((result['count'], result['new_count'], result['read_unique']), (1, 0, 1))
            self.assertEqual(result['target_scope'], 'current_run')
            self.assertEqual(result['reason'], 'target_reached')
            self.assertEqual(result['review_expansion']['accepted'], 1)
            self.assertEqual(read_scoped_mobile_preview(folder)['items'][0]['content'], content)
        logger.info('已达标的手机点评重新读取验证完成')

    def test_reply_control_and_cancellation_do_not_tap(self):
        logger.info('开始仅回复展开及取消验证')
        before, _, _ = pages()
        root = ET.fromstring(before)
        parents = {child: parent for parent in root.iter() for child in parent}
        body = parse_reviews(before, 'hotel')[0]['content']
        body_node = next(node for node in root.iter('node') if node.get('text') == body)
        controls = next(node for node in parents[body_node] if any(child.get('text') == '展开' for child in node))
        parents[body_node].remove(controls)
        reply = ET.SubElement(parents[parents[body_node]], 'node', {'package': 'ctrip.android.view'})
        ET.SubElement(reply, 'node', {'package': 'ctrip.android.view', 'text': '酒店回复：测试内容'})
        reply.append(controls)
        reply_only = ET.tostring(root, encoding='unicode')
        with tempfile.TemporaryDirectory() as folder:
            adb = Mock()
            adb.snapshot.return_value = reply_only
            report = collect(adb, 'hotel', '测试酒店', folder, max_swipes=0, object_id='123', expand_reviews=True)
            self.assertEqual(report['review_expansion']['attempted'], 0)
            adb.run.assert_not_called()
        with tempfile.TemporaryDirectory() as folder:
            adb = Mock()
            adb.snapshot.return_value = before
            is_set = iter((False, True, True))
            report = collect(adb, 'hotel', '测试酒店', folder, max_swipes=0, object_id='123', expand_reviews=True,
                             stop=Mock(is_set=lambda: next(is_set), wait=lambda _: False))
            self.assertEqual(report['reason'], 'stopped_by_user')
            self.assertEqual(report['count'], 1)
            adb.run.assert_not_called()
        logger.info('仅回复展开及取消验证完成')

    def test_expansion_keeps_identity_and_deduplicates_subsequent_screen(self):
        logger.info('开始长评补采与后续同卡去重测试')
        before, after, content = pages()
        with tempfile.TemporaryDirectory() as folder:
            adb = Mock()
            adb.snapshot.side_effect = [before, before, after, after]
            report = collect(adb, 'hotel', '样本酒店', folder, limit=2, max_swipes=1,
                             stop=Mock(is_set=lambda: False, wait=lambda _: False),
                             object_id='123', expand_reviews=True, refresh=True)
            self.assertEqual(report['count'], 1)
            self.assertEqual(report['read_unique'], 1)
            self.assertEqual(report['reason'], 'screen_budget_reached')
            self.assertEqual(report['review_expansion']['accepted'], 1)
            self.assertEqual([call.args[2] for call in adb.run.call_args_list], ['tap', 'swipe'])
            self.assertEqual(adb.run.call_args_list[0].args[3:], ('1246', '2567'))
            with Path(report['csv']).open(encoding='utf-8-sig', newline='') as stream:
                exported = next(csv.DictReader(stream))
            self.assertEqual(exported['expanded_content'], content)
            self.assertEqual(read_scoped_mobile_preview(folder)['items'][0]['content'], content)
        logger.info('长评补采与后续同卡去重测试完成')

    def test_changed_card_never_receives_expanded_text(self):
        logger.info('开始补采卡片变化测试')
        before, after, _ = pages()
        author = parse_reviews(before, 'hotel')[0]['author']
        changed = after.replace(author, '不同作者')
        with tempfile.TemporaryDirectory() as folder:
            adb = Mock()
            adb.snapshot.side_effect = [before, before, changed]
            report = collect(adb, 'hotel', '样本酒店', folder, max_swipes=0,
                             stop=Mock(is_set=lambda: False, wait=lambda _: False),
                             object_id='123', expand_reviews=True)
            self.assertEqual(report['count'], 1)
            self.assertEqual(report['review_expansion']['accepted'], 0)
            self.assertEqual(report['review_expansion']['unverified'], 1)
            self.assertNotIn('这是展开后新增的内容', read_scoped_mobile_preview(folder)['items'][0]['content'])
        logger.info('补采卡片变化测试完成')
