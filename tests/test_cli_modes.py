"""运行模式互斥：同时给出多个入口时应明确拒绝，而不是只跑其中一个。"""
import io
import logging
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import app

logger = logging.getLogger(__name__)


def run(argv):
    # 与 __main__ 一致：参数错误是 SystemExit(2)，运行期可预期错误转成一行提示。
    with patch('sys.argv', ['app.py', *argv]), redirect_stderr(io.StringIO()) as err:
        try:
            app.main()
        except SystemExit as exc:
            return exc.code, err.getvalue()
        except (ValueError, OSError) as exc:
            return 1, f'{err.getvalue()}{exc}'
    return 0, err.getvalue()


class CliModeTests(unittest.TestCase):
    def assert_rejected(self, argv):
        # /* ===== 步骤1：核对互斥；数据源：命令行参数；操作：1) 退出码 2 2) 提示含模式名 ===== */
        code, message = run(argv)
        self.assertEqual(code, 2, ' '.join(argv))
        self.assertIn('一次只能运行一种模式', message, ' '.join(argv))

    def test_combined_actions_are_rejected(self):
        logger.info('开始模式互斥检查')
        for argv in (
            ['--collect', '--capture-ai'],
            ['--collect', '--check'],
            ['--check', '--url', 'https://you.ctrip.com/sight/a1/1.html'],
            ['--url', 'https://you.ctrip.com/sight/a1/1.html', '--collect'],
            ['--capture-ai', '--url', 'https://you.ctrip.com/sight/a1/1.html'],
            ['--check', '--city-list', 'https://you.ctrip.com/sight/a1/s0-p1.html'],
            ['--collect', '--region-seeds', 'sight'],
            ['--hotel-public', '--collect'],
            ['--url-file', 'links.txt', '--collect'],
        ):
            self.assert_rejected(argv)
        logger.info('模式互斥检查完成')

    def test_single_action_still_passes_this_check(self):
        # /* ===== 步骤2：单一模式不被误拦；数据源：单独参数；操作：1) 不触发互斥错误 ===== */
        logger.info('开始单一模式检查')
        # 2.1 用只会走到参数校验、不发请求的组合验证没有误拦；
        #     这些组合仍可能因别的校验失败（例如空目录没有数据库），只看互斥提示有没有出现。
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            # 2.2 只用不会触网的组合：目录发现用非法链接，采集缺 --place，统计用空目录。
            for argv in (['--coverage', '--output', folder], ['--collect'], ['--city-list', 'x'],
                         ['--hotel-public', '--url', 'x']):
                _code, message = run(argv)
                self.assertNotIn('一次只能运行一种模式', message, ' '.join(argv))
        logger.info('单一模式检查完成')


if __name__ == '__main__':
    unittest.main()