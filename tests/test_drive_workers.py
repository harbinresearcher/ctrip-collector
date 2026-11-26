"""--drive 对象级并发（--workers）的单测：全部用假采集入口，不触网。

时序控制只用 Event / Barrier，不靠 sleep 竞速：等不到线程就超时失败，
而不是把「碰巧没并发」当成通过。
"""
import io
import json
import logging
import re
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
from unittest.mock import patch

from gap_collect import collect_gaps
from drive import drive_rounds

logger = logging.getLogger(__name__)


def object_snapshot(root, identity, total):
    url = f'https://you.ctrip.com/sight/a1/{identity}.html'
    data = {'kind': 'sight', 'resource_id': identity, 'name': f'景点{identity}',
            'display_total': total, 'url': url}
    (Path(root) / f'sight-{identity}-object.json').write_text(json.dumps(data), encoding='utf-8')


def add_rows(root, key, amount):
    # 与 network.collect_network 一样给 busy 等待留时间：并发写不能变成随机丢条。
    with closing(sqlite3.connect(Path(root) / 'network.sqlite3', timeout=30)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
        existing = db.execute('SELECT COUNT(*) FROM network_reviews WHERE object_key=?', (key,)).fetchone()[0]
        db.executemany('INSERT INTO network_reviews VALUES(?,?,?,?)',
                       [(key, str(existing + index), '{}', 't') for index in range(amount)])
        db.commit()


def identity_of(url):
    return int(re.search(r'/(\d+)\.html$', url).group(1))


def collector_writing(amount, seen):
    # 模拟一次成功的网络采集：只写本地库和导出文件，不访问网络。
    def collector(url, output, limit, stop, emit):
        key = 'sight-' + re.search(r'/(\d+)\.html$', url).group(1)
        rows = min(amount, limit)
        add_rows(output, key, rows)
        paths = {}
        for suffix in ('csv', 'jsonl'):
            path = Path(output) / f'{key}.{suffix}'
            path.write_text('export', encoding='utf-8')
            paths[suffix] = str(path)
        seen.append(url)
        return dict(paths, object_key=key, reason='target_reached', count=rows, new_count=rows,
                    error='', export_error='', complete=False)
    return collector


def step_clock(*times):
    # 假时钟：按给定时间点依次返回，让两轮报告在单测里逐字节可比较。
    values = iter(times)
    return lambda: next(values)


def all_rows(root):
    with closing(sqlite3.connect(Path(root) / 'network.sqlite3', timeout=30)) as db:
        return dict(db.execute('SELECT object_key, COUNT(*) FROM network_reviews GROUP BY object_key').fetchall())


class DriveWorkersTests(unittest.TestCase):
    def test_default_workers_one_keeps_serial_behavior_and_rejects_out_of_range(self):
        # /* ===== 步骤1：核对默认串行；数据源：同一构造目录跑两次；操作：1) 默认与 workers=1 报告一致 2) 顺序不变 3) 越界参数被拒 ===== */
        logger.info('开始默认串行不变测试')
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            def run(folder, **options):
                object_snapshot(folder, 1, 10)
                object_snapshot(folder, 2, 100)
                return drive_rounds(folder, 2, 1, 1, 3, 5, collector=collector_writing(3, []),
                                    clock=step_clock(0, 30, 30, 60), emit=lambda _msg: None, **options)

            default = run(first)
            explicit = run(second, workers=1)
            # 1.1 除了输出目录，两次报告必须完全一致：默认 1 就是原来的串行行为。
            default['output'] = explicit['output'] = '<output>'
            self.assertEqual(default, explicit)
            self.assertEqual(default['workers'], 1)
            # 1.2 串行顺序仍是「缺口大的在前」，不是按对象键重排。
            self.assertEqual([row['object_key'] for row in default['rounds'][0]['processed']],
                             ['sight-2', 'sight-1'])
            self.assertEqual([row['reason'] for row in default['rounds'][0]['processed']],
                             ['target_reached', 'target_reached'])

        import app
        for value in ('0', '4'):
            with patch('sys.argv', ['app.py', '--drive', '--output', first, '--workers', value]), \
                    patch('sys.stderr', new_callable=io.StringIO) as stderr, \
                    self.assertRaises(SystemExit) as caught:
                app.main()
            # 1.3 越界不是悄悄夹到 1–3，而是 argparse 的用法错误（退出码 2）。
            self.assertEqual(caught.exception.code, 2)
            self.assertIn('--workers', stderr.getvalue())
        logger.info('默认串行不变测试完成')

    def test_two_workers_process_two_objects_in_parallel(self):
        # /* ===== 步骤2：核对两路都跑到；数据源：Barrier 卡住两个对象；操作：1) 两个都完成 2) 输出按对象键排序 ===== */
        logger.info('开始两路并发测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 1, 10)
            object_snapshot(folder, 2, 100)
            barrier = Barrier(2, timeout=10)
            write = collector_writing(3, [])

            def collector(url, output, limit, stop, emit):
                # 只有两路线程同时进入采集入口，barrier 才会放行；串行实现会超时后抛错。
                barrier.wait()
                return write(url, output, limit, stop, emit)

            report = drive_rounds(folder, 1, 1, 1, 3, 2, collector=collector, workers=2,
                                  emit=lambda _msg: None)
            self.assertEqual(report['workers'], 2)
            processed = report['rounds'][0]['processed']
            self.assertEqual([row['object_key'] for row in processed], ['sight-1', 'sight-2'])
            self.assertEqual([row['reason'] for row in processed], ['target_reached', 'target_reached'])
            self.assertFalse(barrier.broken)
            # 2.1 两个对象各自的数据都落库：并发没有把哪一路吃掉。
            self.assertEqual(all_rows(folder), {'sight-1': 3, 'sight-2': 3})
            self.assertLess(report['rounds'][0]['after']['missing'], report['rounds'][0]['before']['missing'])
        logger.info('两路并发测试完成')

    def test_quota_is_not_overspent_under_concurrency(self):
        # /* ===== 步骤3：核对并发配额；数据源：4 个对象、每天只准 5 条；操作：1) 总新增不超过 5 2) 其余对象不开始 ===== */
        logger.info('开始并发配额测试')
        with TemporaryDirectory() as folder:
            for identity in (1, 2, 3, 4):
                object_snapshot(folder, identity, 100)
            seen = []
            report = drive_rounds(folder, 1, 1, 1, 3, 4, collector=collector_writing(3, seen),
                                  daily_quota=5, workers=2, emit=lambda _msg: None)
            processed = report['rounds'][0]['processed']
            counts = all_rows(folder)
            # 3.1 额度按对象预占：第一条 3 条、第二条只剩 2 条，第三、四个对象根本不开始。
            self.assertEqual(sum(counts.values()), 5)
            self.assertLessEqual(sum(counts.values()), 5)
            self.assertEqual(sum(row['requested_add'] for row in processed), 5)
            self.assertEqual([row['object_key'] for row in processed], ['sight-1', 'sight-2'])
            self.assertNotIn('https://you.ctrip.com/sight/a1/3.html', seen)
            self.assertNotIn('https://you.ctrip.com/sight/a1/4.html', seen)
            self.assertEqual(report['quota'], {'daily': 5, 'used_today': 5, 'remaining': 0})
            self.assertEqual(report['reason'], 'daily_quota_reached')
            self.assertFalse(report['complete'])
        logger.info('并发配额测试完成')

    def test_breaker_stops_other_objects_from_starting(self):
        # /* ===== 步骤4：核对并发熔断；数据源：1 号对象失败、2 号对象在途；操作：1) 熔断后 3 号不开始 2) 原因与错误原样保留 ===== */
        logger.info('开始并发熔断测试')
        with TemporaryDirectory() as folder:
            for identity in (1, 2, 3):
                object_snapshot(folder, identity, 10)
            starts = []
            barrier = Barrier(2, timeout=10)
            write = collector_writing(2, [])

            def collector(url, output, limit, stop, emit):
                identity = identity_of(url)
                starts.append(identity)
                if identity in (1, 2):
                    # 两个在途对象都进到采集入口后才让 1 号失败：否则它可能在 2 号被提交前就熔断，
                    # 测试就变成在赌线程启动顺序。
                    barrier.wait()
                if identity == 1:
                    return {'reason': 'error', 'error': '接口拒绝：验证码',
                            'export_error': '', 'complete': False}
                # 2 号对象等到熔断信号再收尾：这是「当前对象跑完就停」，不是立即掐断。
                stop.wait(5)
                return write(url, output, limit, stop, emit)

            report = drive_rounds(folder, 1, 1, 1, 2, 3, collector=collector,
                                  breaker_failures=1, workers=2, emit=lambda _msg: None)
            self.assertEqual(sorted(starts), [1, 2])
            # 4.1 3 号对象没有开始：熔断后不再提交新对象。
            self.assertNotIn(3, starts)
            self.assertEqual(report['reason'], 'circuit_open')
            self.assertTrue(report['breaker']['tripped'])
            self.assertEqual(report['breaker']['object_key'], 'sight-1')
            self.assertIn('验证码', report['breaker']['error'])
            processed = report['rounds'][0]['processed']
            self.assertEqual([row['object_key'] for row in processed], ['sight-1', 'sight-2'])
            self.assertEqual(processed[0]['reason'], 'error')
            self.assertIn('验证码', processed[0]['error'])
            self.assertEqual(processed[1]['reason'], 'target_reached')
            self.assertFalse(report['complete'])
        logger.info('并发熔断测试完成')

    def test_cancellation_stops_remaining_objects(self):
        # /* ===== 步骤5：核对取消；数据源：预置 stop 与运行中 set；操作：1) 都不再开新对象 2) 在途对象跑完 ===== */
        logger.info('开始并发取消测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 1, 10)
            object_snapshot(folder, 2, 10)
            stop = Event()
            stop.set()
            seen = []
            # 5.1 stop 已经置位：一个对象都不该开始。
            gap = collect_gaps(folder, 2, 2, stop=stop, collector=collector_writing(2, seen),
                               emit=lambda _msg: None, workers=2)
            self.assertEqual(gap['reason'], 'cancelled')
            self.assertEqual(gap['processed'], [])
            self.assertEqual(seen, [])
            self.assertEqual(gap['workers'], 2)

        with TemporaryDirectory() as folder:
            for identity in (1, 2, 3, 4):
                object_snapshot(folder, identity, 10)
            starts = []
            barrier = Barrier(2, timeout=10)
            write = collector_writing(1, [])

            def collector(url, output, limit, stop, emit):
                identity = identity_of(url)
                starts.append(identity)
                if identity in (1, 2):
                    # 5.2 运行中取消：先等 2 号也进到采集入口，再自己置位，等价于人工按了停止。
                    barrier.wait()
                if identity == 1:
                    stop.set()
                else:
                    stop.wait(5)
                return write(url, output, limit, stop, emit)

            report = drive_rounds(folder, 3, 1, 1, 1, 4, collector=collector, workers=2,
                                  emit=lambda _msg: None)
            # 5.3 在途的 1、2 号跑完；取消后不再提交 3、4 号，也不再开下一轮。
            self.assertEqual(sorted(starts), [1, 2])
            self.assertNotIn(3, starts)
            self.assertNotIn(4, starts)
            self.assertEqual(report['reason'], 'cancelled')
            self.assertEqual(report['rounds_used'], 1)
            self.assertEqual([row['object_key'] for row in report['rounds'][0]['processed']],
                             ['sight-1', 'sight-2'])
        logger.info('并发取消测试完成')


if __name__ == '__main__':
    unittest.main()