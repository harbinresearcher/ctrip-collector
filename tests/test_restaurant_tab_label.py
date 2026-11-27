"""餐饮页签「视频/图片」也要被识别（真机样本，词序与景点页相反）。"""
import logging
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from mobile_review_context import read_review_context

logger = logging.getLogger(__name__)
FIXTURE = Path(__file__).parent / 'fixtures' / 'restaurant-review-live-lingshui.xml'


class RestaurantTabLabelTests(unittest.TestCase):
    def test_reversed_image_tab_label_is_recognised(self):
        # /* ===== 步骤1：真机餐饮页签；数据源：陵水「琼州糟粕醋」点评列表；
        #         操作：1) 读筛选 2) 视频/图片 的计数不能漏 ===== */
        logger.info('开始餐饮页签词序测试')
        root = ET.fromstring(FIXTURE.read_text(encoding='utf-8'))
        context = read_review_context(root, 'restaurant')
        labels = {item['label']: item['display_count'] for item in context['visible_filters']}
        # 1.1 真机页签是「视频/图片」，与景点页「图片/视频」词序相反；计数 74 必须读到。
        self.assertEqual(labels.get('视频/图片'), '74')
        self.assertEqual(labels.get('全部'), '99')
        logger.info('餐饮页签词序测试完成')

    def test_restaurant_page_has_no_baseline_and_does_not_alarm(self):
        # /* ===== 步骤2：没有基线就不报警；数据源：同一真机样本；操作：1) 指纹 2) expected 为 None ===== */
        logger.info('开始餐饮基线缺省测试')
        from layout_fingerprint import structure_fingerprint
        from layout_fingerprint import FAMILIES
        xml = FIXTURE.read_text(encoding='utf-8')
        self.assertNotIn('mobile_reviews_restaurant', FAMILIES)
        self.assertIsNotNone(structure_fingerprint(xml))
        logger.info('餐饮基线缺省测试完成')


if __name__ == '__main__':
    unittest.main()