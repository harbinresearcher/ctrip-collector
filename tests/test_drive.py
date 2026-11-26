import io
import json
import logging
import re
import sqlite3
import unittest
from contextlib import closing
from datetime import date, timedelta
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


class DriveLongRunProtectionTests(unittest.TestCase):
    """长跑保护：每日配额 / 熔断 / 降速。全部用假采集入口与假时钟，不触网。"""

    def test_daily_quota_stops_when_reached_and_survives_restart(self):
        # /* ===== 步骤1：核对每日配额；数据源：假采集 + 输出目录状态；操作：1) 达到 N 就停 2) 同一天重启后仍生效 ===== */
        logger.info('开始每日配额测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 1, 100)
            seen = []
            report = drive_rounds(folder, 5, 1, 1, 3, 5, collector=collector_writing(3, seen),
                                  daily_quota=5, clock=step_clock(0, 30, 30, 60), emit=lambda _msg: None)
            # 1.1 第 1 轮采 3 条（3 < 5）继续；第 2 轮累计 6 条达到 5，停。
            self.assertEqual((report['reason'], report['rounds_used']), ('daily_quota_reached', 2))
            self.assertEqual(report['rounds'][0]['reason'], 'continued')
            self.assertEqual(report['rounds'][-1]['reason'], 'daily_quota_reached')
            self.assertEqual(report['quota'], {'daily': 5, 'used_today': 6, 'remaining': 0})
            self.assertEqual(len(seen), 2)
            state_path = Path(folder) / 'drive-state.json'
            self.assertEqual(json.loads(state_path.read_text(encoding='utf-8')),
                             {'date': date.today().isoformat(), 'collected_today': 6})
            # 1.2 模拟同一天的重启：状态说已经采满，就不该再开新一轮。
            restarted = []
            again = drive_rounds(folder, 5, 1, 1, 3, 5, collector=collector_writing(3, restarted),
                                 daily_quota=5, emit=lambda _msg: None)
            self.assertEqual((again['reason'], again['rounds_used']), ('daily_quota_reached', 0))
            self.assertEqual((restarted, again['rounds']), ([], []))
            self.assertFalse(again['complete'])
        logger.info('每日配额测试完成')

    def test_daily_quota_resets_on_a_new_day(self):
        # /* ===== 步骤2：核对隔天归零；数据源：伪造的旧日期状态；操作：1) 旧日期不生效 2) 状态改写为今天 ===== */
        logger.info('开始隔天归零测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 2, 100)
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            (Path(folder) / 'drive-state.json').write_text(
                json.dumps({'date': yesterday, 'collected_today': 999}), encoding='utf-8')
            report = drive_rounds(folder, 1, 1, 1, 2, 1, collector=collector_writing(2, []),
                                  daily_quota=5, clock=step_clock(0, 30), emit=lambda _msg: None)
            # 2.1 昨天采到 999 条不影响今天：今天从 0 起算，这一轮照跑。
            self.assertEqual((report['reason'], report['rounds_used']), ('round_budget_reached', 1))
            self.assertEqual(report['quota'], {'daily': 5, 'used_today': 2, 'remaining': 3})
            self.assertEqual(json.loads((Path(folder) / 'drive-state.json').read_text(encoding='utf-8')),
                             {'date': date.today().isoformat(), 'collected_today': 2})
        logger.info('隔天归零测试完成')

    def test_breaker_stops_after_consecutive_failures(self):
        # /* ===== 步骤3：核对熔断；数据源：假采集连续返回 error；操作：1) 到阈值立即停 2) 记下触发对象与错误原文 ===== */
        logger.info('开始熔断测试')
        with TemporaryDirectory() as folder:
            for identity in (1, 2, 3):
                object_snapshot(folder, identity, 10)
            seen = []

            def failing(url, output, limit, stop, emit):
                seen.append(url)
                return {'reason': 'error', 'error': '接口拒绝：' + url,
                        'export_error': '', 'complete': False}

            report = drive_rounds(folder, 5, 1, 1, 2, 5, collector=failing,
                                  breaker_failures=2, emit=lambda _msg: None)
            self.assertEqual((report['reason'], report['rounds_used']), ('circuit_open', 1))
            self.assertEqual(report['rounds'][0]['reason'], 'circuit_open')
            self.assertEqual(report['breaker']['threshold'], 2)
            self.assertTrue(report['breaker']['tripped'])
            # 3.1 触发对象是第 2 个失败：缺口相同时按对象键排序，sight-1 → sight-2。
            self.assertEqual(report['breaker']['object_key'], 'sight-2')
            self.assertIn('接口拒绝', report['breaker']['error'])
            self.assertIn('/2.html', report['breaker']['error'])
            # 3.2 阈值到了就收手：第 3 个对象根本没有发请求。
            self.assertEqual(len(seen), 2)
            self.assertEqual([row['object_key'] for row in report['rounds'][0]['processed']],
                             ['sight-1', 'sight-2'])
            self.assertFalse(report['complete'])
        logger.info('熔断测试完成')

    def test_low_rate_degrades_and_halves_next_round_gap_limit(self):
        # /* ===== 步骤4：核对降速；数据源：假时钟把速率压到阈值以下；操作：1) 标 degraded 2) 下一轮每轮对象数减半 ===== */
        logger.info('开始降速测试')
        with TemporaryDirectory() as folder:
            for identity in (1, 2, 3, 4):
                object_snapshot(folder, identity, 100)
            report = drive_rounds(folder, 2, 1, 1, 1, 4, collector=collector_writing(1, []),
                                  min_per_minute=60, clock=step_clock(0, 30, 30, 90),
                                  emit=lambda _msg: None)
            # 4.1 第 1 轮 4 条 / 30 秒 = 8 条/分钟 < 60：本轮降级，下一轮每轮对象数 4 → 2。
            self.assertEqual(report['rounds'][0]['degraded'], True)
            self.assertEqual(report['rounds'][0]['reviews_per_minute'], 8.0)
            self.assertEqual(report['rounds'][1]['gap_limit'], 2)
            self.assertEqual(len(report['rounds'][1]['selected']), 2)
            self.assertEqual(report['pacing']['degraded'], True)
            self.assertEqual(report['pacing']['min_per_minute'], 60)
            # 4.2 第 2 轮仍未到阈值，再减半到 1（至少 1，不清零、不放弃）。
            self.assertEqual(report['pacing']['gap_limit_next'], 1)
            self.assertFalse(report['complete'])
        logger.info('降速测试完成')

    def test_protection_fields_are_reported_and_complete_stays_false(self):
        # /* ===== 步骤5：核对报告字段；数据源：三种保护同时配置的任务；操作：1) 三块字段齐全 2) complete 仍为 false ===== */
        logger.info('开始保护字段测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 1, 100)
            report = drive_rounds(folder, 3, 1, 1, 3, 2, collector=collector_writing(3, []),
                                  daily_quota=2, breaker_failures=2, min_per_minute=60,
                                  clock=step_clock(0, 30), emit=lambda _msg: None)
            self.assertEqual(set(report['quota']), {'daily', 'used_today', 'remaining'})
            self.assertEqual(set(report['breaker']), {'threshold', 'tripped', 'object_key', 'error'})
            self.assertEqual(set(report['pacing']), {'min_per_minute', 'degraded', 'gap_limit_next'})
            self.assertEqual(report['quota'], {'daily': 2, 'used_today': 3, 'remaining': 0})
            self.assertEqual(report['breaker'], {'threshold': 2, 'tripped': False,
                                                 'object_key': None, 'error': ''})
            # 5.1 被配额停下的运行不是成功：complete 仍为 false，reason 是保护值之一。
            self.assertEqual(report['reason'], 'daily_quota_reached')
            self.assertEqual(report['rounds'][0]['reason'], 'daily_quota_reached')
            self.assertFalse(report['complete'])
            # 5.2 这一轮缺口确实变小了，但停因仍然是配额保护而不是进度。
            self.assertLess(report['rounds'][0]['after']['missing'],
                            report['rounds'][0]['before']['missing'])
        logger.info('保护字段测试完成')


if __name__ == '__main__':
    unittest.main()
