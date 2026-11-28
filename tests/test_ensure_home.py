"""导航前要回到能开始搜索的页面。"""
import logging
import threading
import unittest
from pathlib import Path

from sight_review_navigation import ensure_home

logger = logging.getLogger(__name__)

FIXTURES = Path(__file__).parent / 'fixtures'


class FakeDevice:
    def __init__(self, frames):
        self.frames = frames
        self.back_calls = 0

    def ensure_foreground(self):
        return None

    def snapshot(self):
        return self.frames[min(self.back_calls, len(self.frames) - 1)]

    def run(self, *args):
        if args == ('shell', 'input', 'keyevent', 'KEYCODE_BACK'):
            self.back_calls += 1


class EnsureHomeTests(unittest.TestCase):
    def test_already_home_needs_no_back(self):
        # /* ===== 步骤1：已在首页；数据源：首页样本；操作：1) 不按返回键 ===== */
        logger.info('开始已在首页测试')
        home = (FIXTURES / 'sight-review-nav-home.xml').read_text('utf-8')
        stop = threading.Event()
        device = FakeDevice([home])
        self.assertTrue(ensure_home(device, stop))
        # 1.1 已经是首页就一次返回键都不该按。
        self.assertEqual(device.back_calls, 0)
        logger.info('已在首页测试完成')

    def test_not_home_returns_false_after_bounded_backs(self):
        # /* ===== 步骤2：一直不是首页；数据源：点评页样本；操作：1) 返回键最多 4 次 2) 返回 False ===== */
        logger.info('开始回不去首页测试')
        review = (FIXTURES / 'sight-review-live-yungang.xml').read_text('utf-8')
        stop = threading.Event()
        device = FakeDevice([review])
        self.assertFalse(ensure_home(device, stop))
        # 2.1 只按返回键，不盲点屏幕；次数有上限。
        self.assertEqual(device.back_calls, 4)
        logger.info('回不去首页测试完成')


if __name__ == '__main__':
    unittest.main()