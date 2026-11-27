"""页面读取重试：真实设备会偶发 null root 的瞬时失败，重试一次应能成功。"""
import logging
import unittest
from unittest.mock import patch

from runner import Adb

logger = logging.getLogger(__name__)
GOOD = '<hierarchy><node package="ctrip.android.view" text="全部"/></hierarchy>'


class SnapshotRetryTests(unittest.TestCase):
    def test_transient_dump_failure_is_retried_once(self):
        # /* ===== 步骤1：第一次 dump 失败；数据源：假的 adb 输出；操作：1) 重试 2) 第二次成功 ===== */
        logger.info('开始页面读取重试测试')
        adb = Adb('adb', serial='fake-serial')
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            if args[:3] == ('shell', 'uiautomator', 'dump'):
                if sum(1 for c in calls if c[:3] == ('shell', 'uiautomator', 'dump')) == 1:
                    return 'ERROR: null root node returned by UiTestAutomationBridge.'
                return 'UI hierchary dumped to: /sdcard/x.xml'
            if args[:2] == ('shell', 'cat'):
                return GOOD
            return ''

        with patch.object(adb, 'run', side_effect=fake_run):
            xml = adb.snapshot()
        # 1.1 第二次 dump 成功后返回内容，且确实发生过两次 dump。
        self.assertIn('ctrip.android.view', xml)
        dumps = [c for c in calls if c[:3] == ('shell', 'uiautomator', 'dump')]
        self.assertEqual(len(dumps), 2)
        logger.info('页面读取重试测试完成')

    def test_persistent_failure_reports_actionable_message(self):
        # /* ===== 步骤2：两次都失败；数据源：同一假 adb；操作：1) 抛错 2) 提示唤醒屏幕 ===== */
        logger.info('持续失败测试')
        adb = Adb('adb', serial='fake-serial')

        def fake_run(*args, **kwargs):
            if args[:3] == ('shell', 'uiautomator', 'dump'):
                return 'ERROR: null root node returned by UiTestAutomationBridge.'
            return ''

        with patch.object(adb, 'run', side_effect=fake_run):
            with self.assertRaises(RuntimeError) as caught:
                adb.snapshot()
        message = str(caught.exception)
        self.assertIn('页面读取失败', message)
        self.assertIn('唤醒屏幕', message)
        logger.info('持续失败测试完成')


if __name__ == '__main__':
    unittest.main()