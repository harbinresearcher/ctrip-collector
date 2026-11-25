import logging
import stat
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from workbench_result_files import open_hotel_report, open_task_csv

logger = logging.getLogger(__name__)


class TaskResultFileTests(unittest.TestCase):
    def test_hotel_recovery_opens_only_named_json_from_output_root(self):
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            name = 'app-hotel-' + 'a' * 32 + '.json'
            payload = b'{"status":"running","complete":false}'
            (root / name).write_bytes(payload)
            (root / 'private.json').write_bytes(b'private')
            with open_hotel_report(root, name) as stream:
                self.assertEqual(stream.read(), payload)
            for invalid in ('../' + name, str(root / name), 'private.json', name + '.part'):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    open_hotel_report(root, invalid)

    def test_only_current_terminal_mobile_report_can_download(self):
        # /* ===== 步骤1：核验任务文件；数据源：当前任务报告；操作：1) 允许有效CSV 2) 拒绝旧状态和路径逃逸 ===== */
        logger.info('开始任务文件权限测试')
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            valid = root / 'hotel-a23456789abc.csv'
            valid.write_bytes(b'actual csv')
            report = {'kind': 'hotel', 'csv': str(valid)}
            # 1.1 结果仅从当前任务状态读取，正在运行的新任务不能引用旧报告。
            with open_task_csv(root, {'status': 'finished', 'result': report}) as stream:
                self.assertEqual(stream.read(), b'actual csv')
            # 1.1.1 ID作用域导出保留独立文件名，不覆盖旧版CSV。
            scoped = root / ('hotel-v2-' + 'a' * 24 + '.csv')
            scoped.write_bytes(b'scoped csv')
            with open_task_csv(root, {'status':'finished','result':{'kind':'hotel','csv':str(scoped)}}) as stream:
                self.assertEqual(stream.read(), b'scoped csv')
            for snapshot in ({'status': 'idle', 'result': None},
                             {'status': 'running', 'result': report},
                             {'status': 'stopping', 'result': report},
                             {'status': 'finished', 'result': None},
                             {'status': 'finished', 'result': {'kind': 'hotel', 'csv': 'hotel-a23456789abc.csv'}},
                             {'status': 'finished', 'result': {'kind': 'sight', 'csv': str(valid)}},
                             {'status': 'finished', 'result': {'kind': 'hotel', 'csv': str(root / 'hotel-123.csv')}},
                             {'status': 'finished', 'result': {'kind': 'hotel', 'csv': str(root.parent / valid.name)}}):
                with self.subTest(snapshot=snapshot), self.assertRaises((ValueError, FileNotFoundError)):
                    open_task_csv(root, snapshot)
            # 1.2 缺失文件和空导出均不能返回旧文件。
            with self.assertRaises(FileNotFoundError):
                open_task_csv(root, {'status': 'finished', 'result': {'kind': 'hotel', 'csv': str(root / 'hotel-000000000000.csv')}})
            with self.assertRaises(ValueError):
                open_task_csv(root, {'status': 'error', 'result': {'kind': 'hotel', 'csv': None}})
        logger.info('任务文件权限测试完成')

    def test_symbolic_link_is_rejected(self):
        # /* ===== 步骤1：拒绝符号链接；数据源：临时目录；操作：1) 创建链接 2) 拒绝下载 ===== */
        logger.info('开始符号链接测试')
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            target = root / 'actual.csv'
            target.write_bytes(b'data')
            link = root / 'sight-0123456789ab.csv'
            try:
                # 1.1 Windows不允许创建链接时明确跳过，不把未执行写成通过。
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f'无法创建符号链接: {exc}')
            with self.assertRaises(ValueError):
                open_task_csv(root, {'status': 'finished', 'result': {'kind': 'sight', 'csv': str(link)}})
        logger.info('符号链接测试完成')

    def test_open_handle_reparse_point_is_rejected(self):
        # /* ===== 步骤1：核验已打开句柄；数据源：模拟重解析属性；操作：1) 发现链接 2) 关闭句柄并拒绝 ===== */
        logger.info('开始重解析点句柄测试')
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            path = root / 'hotel-a23456789abc.csv'
            path.write_bytes(b'data')
            # 1.1 即使路径检查后发生变化，句柄标记为重解析点仍须拒绝。
            fake = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
            with patch('workbench_result_files.os.fstat', return_value=fake), self.assertRaises(ValueError):
                open_task_csv(root, {'status': 'finished', 'result': {'kind': 'hotel', 'csv': str(path)}})
        logger.info('重解析点句柄测试完成')
