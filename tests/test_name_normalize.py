"""名称宽度归一：全角括号不该让导航永远对不上。"""
import logging
import unittest

from sight_review_navigation import normalize_name, same_name

logger = logging.getLogger(__name__)


class NameNormalizeTests(unittest.TestCase):
    def test_full_width_parentheses_match_half_width(self):
        # /* ===== 步骤1：目录半角、页面全角；数据源：真实对象名；操作：1) 归一后相等 ===== */
        logger.info('开始全角括号归一测试')
        catalog = '长白山华美胜地旅游度假区(原鲁能胜地)'
        screen = '长白山华美胜地旅游度假区（原鲁能胜地）'
        # 1.1 这条曾经让搜索结果永远匹配不上。
        self.assertTrue(same_name(screen, catalog))
        self.assertEqual(normalize_name(screen), normalize_name(catalog))
        logger.info('全角括号归一测试完成')

    def test_whitespace_is_ignored(self):
        # /* ===== 步骤2：名称里带空格；数据源：页面标题；操作：1) 去空白后相等 ===== */
        logger.info('开始空白归一测试')
        self.assertTrue(same_name('云冈 石窟', '云冈石窟'))
        logger.info('空白归一测试完成')

    def test_different_names_still_differ(self):
        # /* ===== 步骤3：名字确实不同；数据源：两个景点；操作：1) 仍判不同 ===== */
        logger.info('开始不同名判定测试')
        # 3.1 归一不能变成「什么都相等」：这是身份核验的底线。
        self.assertFalse(same_name('长白山西景区', '长白山万达国际度假区'))
        self.assertFalse(same_name('汉拿山温泉', '长白山万达瑞景汉拿山温泉酒店'))
        logger.info('不同名判定测试完成')


if __name__ == '__main__':
    unittest.main()