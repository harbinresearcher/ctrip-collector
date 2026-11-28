"""wait_for 的等待窗口可以按场景单独指定。"""
import logging
import unittest

from sight_review_navigation import STANDARD_READS, wait_for

logger = logging.getLogger(__name__)


class FakeStop:
    def is_set(self):
        return False

    def wait(self, seconds):
        return False


class FakeDevice:
    def __init__(self, frames):
        self.frames = frames
        self.reads = 0

    def ensure_foreground(self):
        return None

    def snapshot(self):
        index = min(self.reads, len(self.frames) - 1)
        self.reads += 1
        return self.frames[index]

    def run(self, *args):
        return ''


def frame(text):
    return ('<hierarchy><node package="ctrip.android.view" bounds="[0,0][1080,2400]">'
            f'<node text="{text}" bounds="[0,0][200,80]"/></node></hierarchy>')


def find_target(root):
    texts = ''.join(node.get('text', '') for node in root.iter('node'))
    return root if '目标' in texts else None


class WaitForReadsTests(unittest.TestCase):
    def test_late_result_is_found_with_longer_window(self):
        # /* ===== 步骤1：结果第 20 次才出现；数据源：假设备；操作：1) 默认窗口找不到 2) 长窗口找到 ===== */
        logger.info('开始长窗口等待测试')
        frames = [frame('加载中')] * 19 + [frame('目标')]
        # 1.1 默认窗口 12 次看不到第 20 帧。
        with self.assertRaises(RuntimeError):
            wait_for(FakeDevice(frames), FakeStop(), find_target, '默认窗口没等到')
        # 1.2 同样的设备状态，给更长窗口就能等到。
        device = FakeDevice(frames)
        self.assertIsNotNone(wait_for(device, FakeStop(), find_target, '长窗口也没等到', reads=40))
        self.assertGreater(device.reads, STANDARD_READS)
        logger.info('长窗口等待测试完成')

    def test_default_window_is_unchanged(self):
        # /* ===== 步骤2：默认窗口仍是 12 次；数据源：常量；操作：1) 不改既有行为 ===== */
        self.assertEqual(STANDARD_READS, 12)
        logger.info('默认窗口检查完成')


if __name__ == '__main__':
    unittest.main()