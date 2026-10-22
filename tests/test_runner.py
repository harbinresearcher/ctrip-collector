import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from runner import Adb, collect

logger = logging.getLogger(__name__)


class RunnerTests(unittest.TestCase):
    def test_fast_snapshot_excludes_other_windows_and_releases_service(self):
        # /* ===== 步骤0：验证快速页面读取；数据源：多窗口 XML；操作：1) 只保留携程窗口 2) 复用连接 3) 释放自动化服务 ===== */
        logger.info('开始快速读取检查')
        device = Mock()
        device.dump_hierarchy.return_value = '<hierarchy><node package="com.android.systemui" text="private" bounds="[0,0][100,10]"/><node package="ctrip.android.view" text="全部" bounds="[0,0][100,200]"/></hierarchy>'
        adb = Adb('adb', 'test', ui_backend='uiautomator2')
        with patch('uiautomator2.connect', return_value=device) as connect:
            first = adb.snapshot()
            adb.snapshot()
            self.assertNotIn('private', first)
            self.assertIn('[0,0][100,200]', first)
            connect.assert_called_once_with('test')
            adb.close()
            device.stop_uiautomator.assert_called_once()
        logger.info('快速读取检查完成')

    def test_export_lock_does_not_discard_report(self):
        logger.info('开始检查 CSV 文件占用')
        # 1.0 导出失败仍记录停止原因，已保存数据库不能被当成丢失。
        stop = threading.Event()
        stop.set()
        with tempfile.TemporaryDirectory() as folder:
            with patch('runner.Store.export', side_effect=PermissionError('CSV is open in Excel')):
                report = collect(Adb('adb', 'test'), 'hotel', 'test', Path(folder), stop=stop)
            self.assertEqual(report['reason'], 'stopped_by_user')
            self.assertIn('Excel', report['export_error'])
            self.assertIsNone(report['csv'])
            self.assertEqual(len(list(Path(folder).glob('report-*.json'))), 1)
        logger.info('CSV 文件占用检查完成')

    def test_hotel_detail_rejected_before_swiping(self):
        logger.info('开始检查详情页误识别')
        # 1.0 酒店详情页也有“点评”，但不是评论列表。
        xml = (Path(__file__).parent / 'fixtures' / 'hotel-detail.xml').read_text('utf-8')
        with tempfile.TemporaryDirectory() as folder:
            adb = Adb('adb', 'test')
            with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', return_value=xml), patch.object(adb, 'run') as run:
                report = collect(adb, 'hotel', 'test', Path(folder), max_swipes=1)
                run.assert_not_called()
                self.assertEqual(report['reason'], 'error')
        logger.info('详情页误识别检查完成')

    def test_android16_focus_uses_full_window_dump(self):
        logger.info('开始检查 Android 16 前台检测')
        # 1.0 Android 16 的 windows 子命令可能不含焦点信息。
        adb = Adb('adb', 'test')
        def response(*args):
            return 'mCurrentFocus=Window{123 u0 ctrip.android.view/Test}' if args == ('shell', 'dumpsys', 'window') else ''
        with patch.object(adb, 'run', side_effect=response):
            adb.ensure_foreground()
        logger.info('Android 16 前台检测完成')

    # /* ================================================================
    # 步骤1：检查设备异常与停止
    # 数据源：受控设备响应；操作：1) 注入 dump 失败 2) 验证不读旧文件
    # ================================================================ */
    def test_dump_failure_never_reads_old_file(self):
        logger.info('开始检查旧文件隔离')
        # 1.1 Android dump 可能返回零退出码但文字报错。
        adb = Adb('adb', 'test')
        with patch.object(adb, 'run', return_value='ERROR: could not get idle state.') as run:
            with self.assertRaises(RuntimeError):
                adb.snapshot()
            self.assertFalse(any('cat' in call.args for call in run.call_args_list))
        logger.info('旧文件隔离检查完成')

    def test_cancel_before_start(self):
        logger.info('开始检查停止状态')
        # 1.2 预先停止时不得向设备发命令，仍写出停止报告。
        stop = threading.Event()
        stop.set()
        with tempfile.TemporaryDirectory() as folder:
            adb = Adb('adb', 'test')
            with patch.object(adb, 'run') as run:
                result = collect(adb, 'hotel', 'test', Path(folder), 500, 10, stop)
                run.assert_not_called()
            self.assertEqual(result['reason'], 'stopped_by_user')
            self.assertEqual(result['count'], 0)
        logger.info('停止状态检查完成')


if __name__ == '__main__':
    unittest.main()
