"""真机景点点评页：筛选计数归一化与相邻标签解析（不猜哪个筛选生效）。"""
import logging
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from mobile_review_context import adjacent_filter_entries, normalize_count, read_review_context

logger = logging.getLogger(__name__)
FIXTURE = Path(__file__).parent / 'fixtures' / 'sight-review-live-yungang.xml'


class SightTabCountTests(unittest.TestCase):
    def test_count_text_is_normalized_to_integer(self):
        # /* ===== 步骤1：归一化；数据源：页面计数文本；操作：1) 万千 2) 千分位 3) 加号 ===== */
        logger.info('开始计数归一化测试')
        cases = {'4.3w': 43000, '3.4万': 34000, '588': 588, '551': 551, '1,234': 1234,
                 '2.5w+': 25000, '1.2千': 1200}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(normalize_count(text), expected)
        # 1.1 不是计数就返回 None，不能把别的文字当数字。
        for text in ('全部', '4.3x', '', 'w'):
            with self.subTest(text=text):
                self.assertIsNone(normalize_count(text))
        logger.info('计数归一化测试完成')

    def test_live_sight_page_exposes_tab_counts(self):
        # /* ===== 步骤2：真机样本；数据源：景点点评列表；操作：1) 相邻标签计数 2) 全部/好评/差评 ===== */
        logger.info('开始真机计数解析测试')
        root = ET.fromstring(FIXTURE.read_text(encoding='utf-8'))
        nodes = [node for node in root.iter('node') if node.get('package') == 'ctrip.android.view']
        entries = {label: (text, value) for label, text, value, _ in adjacent_filter_entries(nodes, {'全部', '好评', '差评'})}
        self.assertEqual(entries['全部'], ('4.3w', 43000))
        self.assertEqual(entries['好评'], ('3.4w', 34000))
        self.assertEqual(entries['差评'], ('588', 588))
        logger.info('真机计数解析测试完成')

    def test_context_keeps_counts_but_does_not_guess_the_active_filter(self):
        # /* ===== 步骤3：不猜测；数据源：同一真机页面；操作：1) 计数可见 2) 选中筛选保持未知 ===== */
        logger.info('开始筛选不猜测测试')
        root = ET.fromstring(FIXTURE.read_text(encoding='utf-8'))
        context = read_review_context(root, 'sight')
        counts = {item['label']: item['display_count'] for item in context['visible_filters']}
        self.assertEqual(counts.get('全部'), 43000)
        # 3.1 页面没有显式选中标记，所以 selected_filter 仍必须是 None。
        self.assertIsNone(context['selected_filter'])
        logger.info('筛选不猜测测试完成')


if __name__ == '__main__':
    unittest.main()