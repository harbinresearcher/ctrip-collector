from contextlib import closing
import logging
import sqlite3
import unittest
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from batch import collect_batch

logger = logging.getLogger(__name__)
URL = 'https://you.ctrip.com/food/fusong2479/25045127.html'


class BatchTests(unittest.TestCase):
    def test_interrupted_and_changed_target_are_retried(self):
        # /* ===== 步骤0：核对中断任务；数据源：持久状态；操作：1) running重试 2) 新目标隔离 ===== */
        logger.info('开始中断任务恢复检查')
        with TemporaryDirectory() as folder:
            collector = Mock(side_effect=lambda *args: self.result(folder))
            collect_batch([URL], folder, 1, collector)
            with closing(sqlite3.connect(Path(folder) / 'batch.sqlite3')) as db:
                db.execute("UPDATE tasks SET status='running'")
                db.commit()
            self.assertEqual(collect_batch([URL], folder, 1, collector)['processed'], 1)
            self.assertEqual(collect_batch([URL], folder, 2, collector)['processed'], 1)
            stop = Event()
            stop.set()
            # 0.1 已取消时不得调用采集函数。
            collector.reset_mock()
            self.assertEqual(collect_batch([URL], folder, 1, collector, stop)['reason'], 'cancelled')
            collector.assert_not_called()
        logger.info('中断任务恢复检查完成')

    def test_report_write_failure_still_returns_processed_results(self):
        # /* ===== 步骤4：核对报告写入；数据源：模拟替换失败；操作：1) 保留已处理结果 2) 报告可返回 ===== */
        logger.info('开始批任务报告写入检查')
        with TemporaryDirectory() as folder:
            collector = Mock(side_effect=lambda *args: self.result(folder))
            original = Path.replace
            def replace(path, destination):
                if destination.name == 'batch-report.json':
                    raise OSError('模拟报告写入失败')
                return original(path, destination)
            with patch.object(Path, 'replace', replace):
                report = collect_batch([URL], folder, 1, collector)
            self.assertEqual(report['processed'], 1)
            self.assertEqual(report['skipped'], 0)
            self.assertTrue(report.get('export_error'))
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])
        logger.info('批任务报告写入检查完成')

    def result(self, folder):
        # /* ===== 步骤1：准备采集证据；数据源：模拟结果；操作：1) 数据库记录 2) 导出文件 ===== */
        logger.info('开始准备批处理证据')
        with closing(sqlite3.connect(Path(folder) / 'network.sqlite3')) as db:
            db.execute('CREATE TABLE IF NOT EXISTS network_reviews (object_key TEXT, comment_id TEXT)')
            db.execute('DELETE FROM network_reviews')
            db.execute("INSERT INTO network_reviews VALUES ('restaurant-25045127','1')")
            db.commit()
        paths = {}
        for suffix in ('csv', 'jsonl'):
            path = Path(folder) / ('restaurant-25045127.' + suffix)
            path.write_text('test export', encoding='utf-8')
            paths[suffix] = str(path)
        logger.info('批处理证据准备完成')
        return dict(paths, object_key='restaurant-25045127', reason='target_reached', count=1,
                    error='', export_error='', complete=False)

    def test_restart_skips_only_verified_target(self):
        # /* ===== 步骤2：核对重启；数据源：持久任务；操作：1) 首次成功 2) 跳过已验证任务 ===== */
        logger.info('开始批处理重启检查')
        with TemporaryDirectory() as folder:
            collector = Mock(side_effect=lambda *args: self.result(folder))
            first = collect_batch([URL, URL], folder, 1, collector)
            second = collect_batch([URL], folder, 1, collector)
            self.assertEqual(first['processed'], 1)
            self.assertEqual(second['skipped'], 1)
            self.assertFalse(second['complete'])
            self.assertEqual(collector.call_count, 1)
            # 2.1 导出文件被改动后不能继续跳过。
            (Path(folder) / 'restaurant-25045127.csv').write_text('changed', encoding='utf-8')
            self.assertEqual(collect_batch([URL], folder, 1, collector)['processed'], 1)
        logger.info('批处理重启检查完成')

    def test_deleted_database_forces_retry(self):
        # /* ===== 步骤3：核对数据丢失；数据源：已完成任务；操作：1) 删除行 2) 强制重采 ===== */
        logger.info('开始数据丢失检查')
        with TemporaryDirectory() as folder:
            collector = Mock(side_effect=lambda *args: self.result(folder))
            collect_batch([URL], folder, 1, collector)
            with closing(sqlite3.connect(Path(folder) / 'network.sqlite3')) as db:
                db.execute('DELETE FROM network_reviews')
                db.commit()
            self.assertEqual(collect_batch([URL], folder, 1, collector)['processed'], 1)
        logger.info('数据丢失检查完成')

    def test_single_object_error_does_not_stop_the_batch(self):
        # /* ===== 步骤4：核对失败恢复；数据源：接口异常；操作：1) 记失败但继续下一个 2) 重启重试 ===== */
        logger.info('开始失败恢复检查')
        with TemporaryDirectory() as folder:
            collector = Mock(side_effect=RuntimeError('verification required'))
            first = collect_batch([URL, URL.replace('25045127', '25044336')], folder, 1, collector)
            # 4.1 单对象失败只记一笔（failed），后面的对象照跑：真机实测一次
            #     IncompleteRead 曾让 62 个对象白白没跑。
            self.assertEqual(first['processed'], 2)
            self.assertEqual(first['failed'], 2)
            self.assertEqual(first['reason'], 'partial')
            self.assertEqual(collector.call_count, 2)
            collector.side_effect = lambda *args: self.result(folder)
            self.assertEqual(collect_batch([URL], folder, 1, collector)['processed'], 1)
        logger.info('失败恢复检查完成')

    def test_blocked_object_still_stops_the_batch(self):
        # /* ===== 步骤5：被平台拦住；数据源：带验证码特征的异常；操作：1) 立即停 2) reason=blocked ===== */
        logger.info('开始被拦停批检查')
        with TemporaryDirectory() as folder:
            collector = Mock(side_effect=RuntimeError('页面返回验证码'))
            report = collect_batch([URL, URL.replace('25045127', '25044336')], folder, 1, collector)
            # 5.1 被拦是「不要再打」的信号，必须停整轮，而不是继续下一个对象。
            self.assertEqual(report['reason'], 'blocked')
            self.assertEqual(collector.call_count, 1)
        logger.info('被拦停批检查完成')

