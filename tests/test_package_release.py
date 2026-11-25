"""交付目录选择：按版本号数字比较，避免把 0.9 当成比 0.10 新。"""
import logging
import tempfile
import unittest
from pathlib import Path

from package_release import latest_release

logger = logging.getLogger(__name__)


class PackageReleaseTests(unittest.TestCase):
    def test_picks_numeric_max_not_lexicographic(self):
        # /* ===== 步骤1：核对编号顺序；数据源：多个交付目录；操作：1) 建 0.9 与 0.10 2) 取 0.10 ===== */
        logger.info('开始交付目录顺序测试')
        with tempfile.TemporaryDirectory() as folder:
            for name in ('携程采集测试版-0.9', '携程采集测试版-0.10', '携程采集测试版-0.20'):
                (Path(folder) / name).mkdir()
            self.assertEqual(latest_release(folder), '携程采集测试版-0.20')
            # 1.1 去掉编号目录后才能看出 0.10 大于 0.9，不按字符串比较。
            (Path(folder) / '携程采集测试版-0.20').rmdir()
            self.assertEqual(latest_release(folder), '携程采集测试版-0.10')
        logger.info('交付目录顺序测试完成')

    def test_ignores_unversioned_and_files(self):
        # /* ===== 步骤2：排除干扰项；数据源：无编号目录与同名文件；操作：1) 只认编号目录 ===== */
        logger.info('开始交付目录过滤测试')
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / '携程采集测试版').mkdir()
            (Path(folder) / '携程采集测试版-0.3.zip').write_bytes(b'zip')
            (Path(folder) / '携程采集测试版-0.4').mkdir()
            self.assertEqual(latest_release(folder), '携程采集测试版-0.4')
        logger.info('交付目录过滤测试完成')

    def test_missing_outputs_is_a_clear_error(self):
        # /* ===== 步骤3：核对空目录；数据源：不存在或没有编号目录；操作：1) 抛出可读错误 ===== */
        logger.info('开始交付目录缺失测试')
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError) as caught:
                latest_release(Path(folder) / 'missing')
            self.assertIn('没有', str(caught.exception))
            with self.assertRaises(ValueError):
                latest_release(folder)
        logger.info('交付目录缺失测试完成')


if __name__ == '__main__':
    unittest.main()