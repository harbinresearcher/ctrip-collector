"""u2 快速读取：读不到携程窗口时重试，别把「正在切页」当成「App 没了」。"""
import logging
import unittest
from unittest.mock import patch

from runner import Adb

logger = logging.getLogger(__name__)

OK = ('<hierarchy><node package="ctrip.android.view" bounds="[0,0][1080,2400]">'
      '<node text="全部" bounds="[0,0][100,50]"/></node></hierarchy>')
EMPTY = '<hierarchy><node package="com.other.app" bounds="[0,0][10,10]"/></hierarchy>'


class FakeUi:
    def __init__(self, outputs):
        self.outputs = list(outputs)

    def dump_hierarchy(self, compressed=False):
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FastSnapshotRetryTests(unittest.TestCase):
    def test_empty_hierarchy_is_retried(self):
        # /* ===== 步骤1：第一次只有别的窗口；数据源：假 u2；操作：1) 重试 2) 第二次拿到携程 ===== */
        logger.info('开始快速读取重试测试')
        adb = Adb('adb', 'test', ui_backend='uiautomator2')
        adb.serial = 'test'
        adb._ui = FakeUi([EMPTY, OK])
        with patch('time.sleep'):
            xml = adb.snapshot()
        # 1.1 只有携程窗口能被留下。
        self.assertIn('ctrip.android.view', xml)
        self.assertNotIn('com.other.app', xml)
        logger.info('快速读取重试测试完成')

    def test_persistent_failure_reports_reason(self):
        # /* ===== 步骤2：一直读不到；数据源：假 u2；操作：1) 三次后报错 ===== */
        logger.info('开始快速读取持续失败测试')
        adb = Adb('adb', 'test', ui_backend='uiautomator2')
        adb.serial = 'test'
        adb._ui = FakeUi([EMPTY, EMPTY, EMPTY])
        with patch('time.sleep'), self.assertRaises(RuntimeError) as caught:
            adb.snapshot()
        self.assertIn('没有携程窗口', str(caught.exception))
        logger.info('快速读取持续失败测试完成')


if __name__ == '__main__':
    unittest.main()