"""对象名判定：只认完全相等，包含匹配不能算到达。"""
import logging
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from review_screen import review_screen_state

logger = logging.getLogger(__name__)
FIXTURES = Path(__file__).parent / 'fixtures'


class NameMatchTests(unittest.TestCase):
    def test_partial_name_match_is_not_arrived(self):
        # /* ===== 步骤1：真机踩过的坑；数据源：景点点评页改成酒店名；
        #         操作：1) 标题只是包含目标名 2) 不能算到达 ===== */
        logger.info('开始包含匹配测试')
        xml = (FIXTURES / 'sight-review-live-yungang.xml').read_text(encoding='utf-8')
        root = ET.fromstring(xml)
        for node in root.iter('node'):
            if (node.get('text') or '').strip() == '云冈石窟':
                node.set('text', '云冈石窟大酒店')
        state = review_screen_state(ET.tostring(root, encoding='unicode'), '云冈石窟')
        # 1.1 页面写的是「云冈石窟大酒店」，目标「云冈石窟」只是它的前缀——
        #     这种情况真机上出现过（汉拿山温泉 vs 长白山万达瑞景汉拿山温泉酒店）。
        self.assertFalse(state['arrived'])
        self.assertEqual(state['reason'], 'name_partial_match')
        self.assertEqual(state['name_match'], 'contains')
        self.assertEqual(state['matched_text'], '云冈石窟大酒店')
        logger.info('包含匹配测试完成')

    def test_exact_name_still_arrives(self):
        # /* ===== 步骤2：名字完全相等；数据源：同一真机样本；操作：1) 仍判 arrived ===== */
        logger.info('开始完全相等测试')
        xml = (FIXTURES / 'sight-review-live-yungang.xml').read_text(encoding='utf-8')
        state = review_screen_state(xml, '云冈石窟')
        self.assertTrue(state['arrived'])
        self.assertEqual(state['name_match'], 'exact')
        logger.info('完全相等测试完成')


if __name__ == '__main__':
    unittest.main()