"""--adb-serial 在命令行采集里可用：同时连着模拟器和真机时能指定其中一台。"""
import io
import logging
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import app

logger = logging.getLogger(__name__)


def run(argv):
    with patch('sys.argv', ['app.py', *argv]), redirect_stderr(io.StringIO()) as err:
        try:
            app.main()
        except SystemExit as exc:
            return exc.code, err.getvalue()
        except (ValueError, OSError) as exc:
            return 1, f'{err.getvalue()}{exc}'
    return 0, err.getvalue()


class AdbSerialCliTests(unittest.TestCase):
    def test_check_accepts_serial(self):
        # /* ===== 步骤1：自检带 serial；数据源：命令行；操作：1) 不报参数错 2) 把 serial 传下去 ===== */
        logger.info('开始 serial 自检测试')
        seen = {}

        class FakeAdb:
            def __init__(self, executable, serial=None, ui_backend='adb', **kwargs):
                seen['serial'] = serial

            def connect(self):
                return 'ok'

            def device_info(self):
                return {'device': 'authorized', 'ctrip': 'installed'}

        with patch.object(app, 'Adb', FakeAdb):
            code, _ = run(['--check', '--adb-serial', '95e007f0'])
        # 1.1 参数被接受（不是 exit 2），且 serial 真的传到了 Adb。
        self.assertEqual(code, 0)
        self.assertEqual(seen.get('serial'), '95e007f0')
        logger.info('serial 自检测试完成')

    def test_workbench_only_flags_are_still_rejected(self):
        # /* ===== 步骤2：工作台专属参数仍拒绝；数据源：命令行；操作：1) 退出码 2 ===== */
        logger.info('开始工作台参数拒绝测试')
        for flag in (['--port', '8765'], ['--no-browser']):
            with self.subTest(flag=flag):
                code, message = run(['--check', *flag])
                self.assertEqual(code, 2)
                self.assertIn('--workbench', message)
        logger.info('工作台参数拒绝测试完成')

    def test_two_actions_with_serial_still_rejected(self):
        # /* ===== 步骤3：带 serial 也不放宽模式互斥；数据源：命令行；操作：1) 退出码 2 ===== */
        logger.info('开始互斥仍生效测试')
        code, message = run(['--check', '--collect', '--adb-serial', '95e007f0'])
        self.assertEqual(code, 2)
        self.assertIn('一次只能运行一种模式', message)
        logger.info('互斥仍生效测试完成')


if __name__ == '__main__':
    unittest.main()