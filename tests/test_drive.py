import io
import json
import logging
import re
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from drive import drive_rounds

logger = logging.getLogger(__name__)


def object_snapshot(root, identity, total):
    url = f'https://you.ctrip.com/sight/a1/{identity}.html'
    data = {'kind': 'sight', 'resource_id': identity, 'name': f'景点{identity}',
            'display_total': total, 'url': url}
    (Path(root) / f'sight-{identity}-object.json').write_text(json.dumps(data), encoding='utf-8')


def add_rows(root, key, amount):
    with closing(sqlite3.connect(Path(root) / 'network.sqlite3')) as db:
        db.execute('CREATE TABLE IF NOT EXISTS network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
        existing = db.execute('SELECT COUNT(*) FROM network_reviews WHERE object_key=?', (key,)).fetchone()[0]
        db.executemany('INSERT INTO network_reviews VALUES(?,?,?,?)',
                       [(key, str(existing + index), '{}', 't') for index in range(amount)])
        db.commit()


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


def fake_discovery(calls):
    # 模拟目录发现：只往同一输出目录写一份目录报告，不访问网络。
    def discover(output, pages, objects):
        calls.append((Path(output), pages, objects))
        report = {'objects': [], 'pages': 1, 'listing_total': 100, 'complete': False,
                  'reason': 'page_budget_reached', 'error': '', 'max_pages': pages, 'max_objects': objects}
        name = 'discovery-sight-2-20261126T000000000000Z.json'
        (Path(output) / name).write_text(json.dumps(report), encoding='utf-8')
        return dict(report, links_file=str(Path(output) / name))
    return discover


def step_clock(*times):
    # 假时钟：按给定时间点依次返回，让每轮耗时在单测里可复现，不引入真实等待。
    values = iter(times)
    return lambda: next(values)


class DriveTests(unittest.TestCase):
    def test_progress_continues_to_next_round(self):
        # /* ===== 步骤1：核对有进展；数据源：模拟成功采集；操作：1) 缺口下降就继续 2) 目录只发现一次 ===== */
        logger.info('开始有进展继续测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 1, 100)
            seen, calls = [], []
            report = drive_rounds(folder, 3, 2, 3, 3, 5, discover=fake_discovery(calls),
                                  collector=collector_writing(3, seen), emit=lambda _msg: None)
            self.assertEqual((report['reason'], report['rounds_used'], len(report['rounds'])), ('round_budget_reached', 3, 3))
            self.assertEqual([row['before']['missing'] for row in report['rounds']], [100, 97, 94])
            self.assertEqual([row['after']['missing'] for row in report['rounds']], [97, 94, 91])
            self.assertEqual([row['collected_delta'] for row in report['rounds']], [3, 3, 3])
            self.assertEqual([row['reason'] for row in report['rounds']], ['continued', 'continued', 'round_budget_reached'])
            # 1.1 目录接口只在第一轮打一次，且报告落在同一输出目录。
            self.assertEqual(calls, [(Path(folder).resolve(), 2, 3)])
            self.assertEqual(report['discovery']['object_count'], 0)
            self.assertTrue((Path(folder) / 'discovery-sight-2-20261126T000000000000Z.json').is_file())
            self.assertFalse(report['complete'])
            self.assertTrue(report['limitations'])
        logger.info('有进展继续测试完成')

    def test_no_progress_stops_after_one_round(self):
        # /* ===== 步骤2：核对无进展；数据源：模拟采集失败；操作：1) 只跑一轮 2) 报 no_progress ===== */
        logger.info('开始无进展停止测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 2, 10)
            report = drive_rounds(folder, 5, 1, 1, 2, 5,
                                  collector=lambda url, output, limit, stop, emit: {
                                      'reason': 'error', 'error': 'verification required',
                                      'export_error': '', 'complete': False},
                                  emit=lambda _msg: None)
            self.assertEqual((report['reason'], report['rounds_used']), ('no_progress', 1))
            self.assertEqual(len(report['rounds']), 1)
            self.assertEqual(report['rounds'][0]['before']['missing'], 10)
            self.assertEqual(report['rounds'][0]['after']['missing'], 10)
            self.assertEqual(report['rounds'][0]['processed'][0]['reason'], 'error')
        logger.info('无进展停止测试完成')

    def test_all_targets_met_stops(self):
        # /* ===== 步骤3：核对达标停止；数据源：缺口清零；操作：1) 报 all_targets_met 2) 不再跑下一轮 ===== */
        logger.info('开始目标全达成测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 3, 2)
            seen = []
            report = drive_rounds(folder, 5, 1, 1, 5, 5, collector=collector_writing(2, seen),
                                  emit=lambda _msg: None)
            self.assertEqual((report['reason'], report['rounds_used']), ('all_targets_met', 1))
            self.assertEqual(report['rounds'][0]['after']['missing'], 0)
            self.assertEqual(report['rounds'][0]['collected_delta'], 2)
            self.assertEqual(len(seen), 1)
        logger.info('目标全达成测试完成')

    def test_round_budget_stops_even_with_progress(self):
        # /* ===== 步骤4：核对轮数预算；数据源：仍有缺口但预算用完；操作：1) 报 round_budget_reached 2) 只跑一轮 ===== */
        logger.info('开始轮数预算测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 4, 100)
            report = drive_rounds(folder, 1, 1, 1, 2, 1, collector=collector_writing(2, []),
                                  emit=lambda _msg: None)
            self.assertEqual((report['reason'], report['rounds_used']), ('round_budget_reached', 1))
            self.assertEqual(len(report['rounds']), 1)
            self.assertLess(report['rounds'][0]['after']['missing'], report['rounds'][0]['before']['missing'])
            self.assertFalse(report['complete'])
        logger.info('轮数预算测试完成')

    def test_single_object_failure_keeps_reason_and_round_continues(self):
        # /* ===== 步骤5：核对单对象失败；数据源：一个对象抛错；操作：1) 原样保留 reason/error 2) 另一个对象照常采 ===== */
        logger.info('开始单对象失败测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 1, 10)
            object_snapshot(folder, 2, 10)
            good = collector_writing(2, [])

            def collector(url, output, limit, stop, emit):
                if url.endswith('/2.html'):
                    raise RuntimeError('接口拒绝')
                return good(url, output, limit, stop, emit)

            report = drive_rounds(folder, 1, 1, 1, 2, 5, collector=collector, emit=lambda _msg: None)
            processed = report['rounds'][0]['processed']
            self.assertEqual([item['object_key'] for item in processed], ['sight-1', 'sight-2'])
            self.assertEqual(processed[0]['reason'], 'target_reached')
            # 5.1 失败对象不许写成成功：原因、错误和导出错误都原样保留。
            self.assertEqual(processed[1]['reason'], 'error')
            self.assertIn('接口拒绝', processed[1]['error'])
            self.assertEqual(processed[1]['export_error'], '')
            self.assertEqual((processed[1]['before_missing'], processed[1]['after_missing']), (10, 10))
            # 5.2 整轮没有被这个失败中断：成功对象的数据照样进账，缺口按真实数字下降。
            self.assertEqual(report['rounds'][0]['after']['collected'], 2)
            self.assertEqual(report['rounds'][0]['after']['missing'], 18)
        logger.info('单对象失败测试完成')


class DriveThroughputTests(unittest.TestCase):
    def test_round_rate_and_throughput_come_from_measured_elapsed(self):
        # /* ===== 步骤1：核对吞吐；数据源：注入假时钟（两轮分别 30 秒 / 60 秒）；操作：1) 每轮算速率 2) 整体按全部轮次算 ===== */
        logger.info('开始吞吐率测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 5, 100)
            report = drive_rounds(folder, 2, 1, 1, 3, 5, collector=collector_writing(3, []),
                                  clock=step_clock(0, 30, 30, 90), emit=lambda _msg: None)
            self.assertEqual([row['elapsed_seconds'] for row in report['rounds']], [30, 60])
            # 1.1 每轮速率 = 本轮 delta / 本轮耗时 × 60；两轮各采 3 条。
            self.assertEqual([row['reviews_per_minute'] for row in report['rounds']], [6.0, 3.0])
            # 1.2 整体吞吐：本次共采 6 条 / 90 秒 × 60 = 4 条/分钟。
            self.assertEqual(report['throughput'], {'reviews_per_minute': 4.0, 'collected_total': 6,
                                                    'elapsed_seconds': 90})
            # 1.3 ETA 用整体速率外推剩余缺口：94 条 / 4 条每分钟 = 1410 秒。
            self.assertEqual(report['eta_seconds'], 1410.0)
            self.assertTrue(any('ETA' in line for line in report['limitations']))
        logger.info('吞吐率测试完成')

    def test_zero_delta_gives_null_rate_and_null_eta(self):
        # /* ===== 步骤2：核对零增量；数据源：采集报错、时钟正常走；操作：1) 速率写 null 2) ETA 也写 null ===== */
        logger.info('开始零增量速率测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 6, 10)
            report = drive_rounds(folder, 2, 1, 1, 2, 5,
                                  collector=lambda url, output, limit, stop, emit: {
                                      'reason': 'error', 'error': 'verification required',
                                      'export_error': '', 'complete': False},
                                  clock=step_clock(0, 5), emit=lambda _msg: None)
            self.assertEqual(report['reason'], 'no_progress')
            self.assertIsNone(report['rounds'][0]['reviews_per_minute'])
            self.assertIsNone(report['throughput']['reviews_per_minute'])
            self.assertEqual(report['throughput']['collected_total'], 0)
            self.assertIsNone(report['eta_seconds'])
        logger.info('零增量速率测试完成')

    def test_eta_matches_missing_and_rate(self):
        # /* ===== 步骤3：核对 ETA；数据源：missing 240、速率 120 条/分钟；操作：1) eta 必须等于 120 秒 ===== */
        logger.info('开始 ETA 一致性测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 7, 360)
            report = drive_rounds(folder, 1, 1, 1, 120, 5, collector=collector_writing(120, []),
                                  clock=step_clock(0, 60), emit=lambda _msg: None)
            self.assertEqual(report['throughput']['reviews_per_minute'], 120.0)
            self.assertEqual(report['rounds'][0]['after']['missing'], 240)
            self.assertEqual(report['eta_seconds'], 120.0)
        logger.info('ETA 一致性测试完成')

    def test_cli_drive_defaults_to_two_hundred_and_keeps_collect_gaps_default(self):
        # /* ===== 步骤4：核对默认条数；数据源：真实 app.main 参数解析；操作：1) drive 默认 200 2) collect-gaps 仍 500 ===== */
        logger.info('开始默认条数测试')
        import app
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 8, 1000)
            with patch('sys.argv', ['app.py', '--drive', '--output', folder, '--rounds', '1',
                                    '--max-objects', '1', '--gap-limit', '1']), \
                    patch('app.collect_url', side_effect=collector_writing(3, [])), \
                    patch('sys.stdout', new_callable=io.StringIO) as stdout:
                app.main()
                result = json.loads(stdout.getvalue())
            self.assertEqual(result['limit'], 200)
            with patch('sys.argv', ['app.py', '--collect-gaps', '--output', folder, '--gap-limit', '1']), \
                    patch('app.collect_url', side_effect=collector_writing(3, [])), \
                    patch('sys.stdout', new_callable=io.StringIO) as stdout:
                app.main()
                result = json.loads(stdout.getvalue())
            self.assertEqual(result['limit'], 500)
        logger.info('默认条数测试完成')

    def test_help_states_drive_limit_default_and_measured_rate(self):
        # /* ===== 步骤5：核对帮助文本；数据源：argparse --help；操作：1) 写出 drive 默认 200 2) 写出实测约 230 条/分钟 ===== */
        logger.info('开始帮助文本测试')
        import app
        with patch('sys.argv', ['app.py', '--help']), \
                patch('sys.stdout', new_callable=io.StringIO) as stdout, self.assertRaises(SystemExit):
            app.main()
        text = stdout.getvalue()
        self.assertIn('--drive 默认 200', text)
        self.assertIn('230', text)
        logger.info('帮助文本测试完成')


if __name__ == '__main__':
    unittest.main()