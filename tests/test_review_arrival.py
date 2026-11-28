"""到达判据：三条齐全才算停在目标对象的点评页。"""
import json
import logging
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

from review_screen import count_int, review_screen_state

logger = logging.getLogger(__name__)
FIXTURES = Path(__file__).parent / 'fixtures'


class ArrivalTests(unittest.TestCase):
    def test_sight_and_restaurant_and_hotel_pages_count_as_arrived(self):
        # /* ===== 步骤1：三类真实点评页；数据源：fixtures；操作：1) 锚点 2) 名字 3) 全部计数 ===== */
        logger.info('开始三类点评页到达测试')
        cases = [('sight-review-live-yungang.xml', '云冈石窟', 43000, False),
                 ('restaurant-review-live-lingshui.xml', '琼州糟粕醋', 99, False),
                 ('hotel-comments.xml', '长白山万达锦华度假酒店', 5722, True)]
        for name, expected, total, allow in cases:
            with self.subTest(fixture=name):
                state = review_screen_state((FIXTURES / name).read_text(encoding='utf-8'), expected,
                                            allow_missing_name=allow)
                self.assertTrue(state['arrived'], state)
                self.assertEqual(state['reason'], 'arrived')
                self.assertEqual(state['all_count'], total)
        logger.info('三类点评页到达测试完成')

    def test_non_review_screen_reports_missing_anchor(self):
        # /* ===== 步骤2：不是点评页；数据源：榜单页快照；操作：1) arrived=False 2) 原因=no_review_anchor ===== */
        logger.info('开始非点评页测试')
        snapshot = Path('.test-tmp/live/ui-top.xml')
        if not snapshot.is_file():
            self.skipTest('缺少榜单页快照')
        state = review_screen_state(snapshot.read_text(encoding='utf-8'), None)
        self.assertFalse(state['arrived'])
        self.assertEqual(state['reason'], 'no_review_anchor')
        logger.info('非点评页测试完成')

    def test_name_mismatch_is_reported(self):
        # /* ===== 步骤3：名字不符；数据源：景点点评页 + 别的名字；操作：1) 原因=name_mismatch ===== */
        logger.info('开始对象名不符测试')
        xml = (FIXTURES / 'sight-review-live-yungang.xml').read_text(encoding='utf-8')
        state = review_screen_state(xml, '完全不相干的另一个景点名字')
        self.assertFalse(state['arrived'])
        self.assertEqual(state['reason'], 'name_mismatch')
        logger.info('对象名不符测试完成')

    def test_count_text_conversion(self):
        # /* ===== 步骤4：计数归一化；数据源：页面文本；操作：1) 4.3w 2) 千分位 3) 非计数 ===== */
        logger.info('开始计数归一化测试')
        self.assertEqual(count_int('4.3w'), 43000)
        self.assertEqual(count_int('1,234'), 1234)
        self.assertEqual(count_int('2.5万+'), 25000)
        self.assertIsNone(count_int('全部'))
        self.assertIsNone(count_int(''))
        logger.info('计数归一化测试完成')

    def test_missing_all_count_is_reported(self):
        # /* ===== 步骤5：有锚点有名字但没有「全部」计数；数据源：景点样本改字；
        #         操作：1) arrived=False 2) 原因=no_all_count ===== */
        logger.info('开始缺全部计数测试')
        xml = (FIXTURES / 'sight-review-live-yungang.xml').read_text(encoding='utf-8')
        root = ET.fromstring(xml)
        for node in root.iter('node'):
            text = (node.get('text') or '').strip()
            if text in ('全部', '4.3w', '好评', '3.4w', '差评', '588', '消费后评价'):
                node.set('text', '')
        state = review_screen_state(ET.tostring(root, encoding='unicode'), '云冈石窟')
        self.assertTrue(state['anchor'])
        self.assertTrue(state['name_seen'])
        self.assertFalse(state['arrived'])
        self.assertEqual(state['reason'], 'no_all_count')
        logger.info('缺全部计数测试完成')


if __name__ == '__main__':
    unittest.main()