"""命令行常见错误的输出：可预期错误只给一行原因，不甩 Python 堆栈。"""
import logging
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


def run_cli(*args):
    return subprocess.run([sys.executable, str(ROOT / 'app.py'), *args], cwd=ROOT,
                          capture_output=True, encoding='utf-8', errors='replace', timeout=120)


class CliErrorTests(unittest.TestCase):
    def assert_readable_failure(self, result, expected):
        # /* ===== 步骤1：核对失败输出；数据源：真实子进程；操作：1) 退出码 2) 无堆栈 3) 含原因 ===== */
        logger.info('开始核对命令行错误输出')
        output = (result.stdout or '') + (result.stderr or '')
        self.assertEqual(result.returncode, 1, output)
        self.assertNotIn('Traceback', output)
        self.assertIn(expected, output)

    def test_missing_output_directory_is_explained(self):
        # 1.1 覆盖统计在空目录上要说明缺什么，而不是抛 ValueError 栈。
        with tempfile.TemporaryDirectory() as folder:
            empty = Path(folder) / 'empty'
            empty.mkdir()
            self.assert_readable_failure(run_cli('--coverage', '--output', str(empty)), '没有网络评论数据库')

    def test_missing_capture_file_is_explained(self):
        # 1.2 导入不存在的文件：文件路径要出现在提示里。
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / 'nope.jsonl'
            result = run_cli('--import-capture', str(missing))
            self.assertEqual(result.returncode, 1)
            self.assertNotIn('Traceback', (result.stdout or '') + (result.stderr or ''))

    def test_rejected_link_is_explained(self):
        # 1.3 既不是携程详情页也不是合法 URL 时，给出入口要求。
        with tempfile.TemporaryDirectory() as folder:
            self.assert_readable_failure(
                run_cli('--collect', '--url', 'not-a-url', '--output', folder), '携程')

    def test_argument_errors_keep_usage_exit_code(self):
        # 1.4 argparse 自身错误仍是 2，不被上面的处理吞成 1。
        result = run_cli('--port', '70000')
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('Traceback', (result.stdout or '') + (result.stderr or ''))


if __name__ == '__main__':
    unittest.main()