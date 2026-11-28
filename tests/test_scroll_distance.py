"""滑动距离随连续相同屏递增：卡片太高的对象不该被误判成采完。"""
import logging
import unittest

from runner import scroll_distance

logger = logging.getLogger(__name__)


class ScrollDistanceTests(unittest.TestCase):
    def test_distance_grows_then_caps(self):
        # /* ===== 步骤1：连续相同屏；数据源：停滞计数；操作：1) 逐步加大 2) 上限 65% ===== */
        logger.info('开始滑动距离测试')
        # 1.1 正常推进时仍是小幅滑动，不打扰列表。
        self.assertAlmostEqual(scroll_distance(0), 0.20)
        # 1.2 停滞越久滑得越多，但不超过 65%，避免一把滑过头。
        self.assertAlmostEqual(scroll_distance(1), 0.35)
        self.assertAlmostEqual(scroll_distance(2), 0.50)
        self.assertAlmostEqual(scroll_distance(9), 0.65)
        logger.info('滑动距离测试完成')

    def test_negative_is_treated_as_fresh(self):
        # /* ===== 步骤2：异常计数；数据源：负数；操作：1) 当作刚推进处理 ===== */
        self.assertAlmostEqual(scroll_distance(-3), 0.20)
        logger.info('负计数测试完成')


if __name__ == '__main__':
    unittest.main()