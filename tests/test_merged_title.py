"""合并标题布局：标题+评分+地址在同一节点时也能按地址认出来，且不误认。"""
import logging
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from sight_review_navigation import matching_sight_result

logger = logging.getLogger(__name__)
FIXTURE = Path(__file__).parent / 'fixtures' / 'sight-search-merged-title.xml'


class MergedTitleTests(unittest.TestCase):
    def test_merged_title_row_is_matched_by_address(self):
        # /* ===== 步骤1：真实搜索结果页；数据源：真机样本；操作：1) 名称+地址能认出 ===== */
        logger.info('开始合并标题匹配测试')
        root = ET.fromstring(FIXTURE.read_text('utf-8'))
        self.assertIsNotNone(matching_sight_result(root, '吉林省长白山如来寺', '302省道'))
        logger.info('合并标题匹配测试完成')

    def test_wrong_address_or_other_object_is_not_matched(self):
        # /* ===== 步骤2：地址不符 / 别的景点；数据源：同一份样本；操作：1) 不误认 ===== */
        logger.info('开始合并标题防误认测试')
        root = ET.fromstring(FIXTURE.read_text('utf-8'))
        # 2.1 只有名称对上不够：地址不符就不认，别的景点更不能借。
        self.assertIsNone(matching_sight_result(root, '吉林省长白山如来寺', '某某路'))
        self.assertIsNone(matching_sight_result(root, '长白山西景区', '302省道'))
        logger.info('合并标题防误认测试完成')


if __name__ == '__main__':
    unittest.main()