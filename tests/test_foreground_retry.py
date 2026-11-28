"""前台检测要能容忍页面切换瞬间的「读不到焦点窗口」。"""
import logging
import unittest
from unittest.mock import patch

from runner import Adb

logger = logging.getLogger(__name__)

EMPTY = 'WINDOW MANAGER WINDOWS\n'
FOCUSED = '  mCurrentFocus=Window{abc u0 ctrip.android.view/ctrip.android.publicproduct.home.CtripHomeActivity}\n'
OTHER = '  mCurrentFocus=Window{abc u0 com.google.android.apps.nexuslauncher/.Launcher}\n'


class ForegroundRetryTests(unittest.TestCase):
    def test_empty_focus_is_retried_then_succeeds(self):
        # /* ===== 步骤1：前两次读不到焦点；数据源：假 dumpsys；操作：1) 重试 2) 第三次通过 ===== */
        logger.info('开始前台重试测试')
        adb = Adb('adb', 'test')
        outputs = [EMPTY, EMPTY, FOCUSED]
        with patch.object(adb, 'run', side_effect=lambda *args, **kwargs: outputs.pop(0)), \
                patch('time.sleep'):
            adb.ensure_foreground()
        # 1.1 切页瞬间不该判成「用户切走了」，重试后通过即可。
        self.assertEqual(outputs, [])
        logger.info('前台重试测试完成')

    def test_other_app_in_foreground_still_fails_fast(self):
        # /* ===== 步骤2：确实停在别的应用；数据源：桌面焦点；操作：1) 立刻判失败 ===== */
        logger.info('开始别的应用在前台测试')
        adb = Adb('adb', 'test')
        calls = []
        with patch.object(adb, 'run', side_effect=lambda *args, **kwargs: (calls.append(args), OTHER)[1]):
            with self.assertRaises(Exception) as caught:
                adb.ensure_foreground()
        # 2.1 焦点读到了、但不是携程：不重试，直接报后台。
        self.assertIn('不在前台', str(caught.exception))
        self.assertEqual(len(calls), 1)
        logger.info('别的应用在前台测试完成')


if __name__ == '__main__':
    unittest.main()