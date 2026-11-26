import io
import json
import logging
import re
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from gap_collect import collect_gaps, idle_reason

logger = logging.getLogger(__name__)


def object_snapshot(root, identity, total, url=None):
    # 快照默认带对象链接；传空字符串表示快照里没有链接。
    if url is None:
        url = f'https://you.ctrip.com/sight/a1/{identity}.html'
    data = {'kind': 'sight', 'resource_id': identity, 'name': f'景点{identity}', 'display_total': total}
    if url:
        data['url'] = url
    (Path(root) / f'sight-{identity}-object.json').write_text(json.dumps(data), encoding='utf-8')


def discovery_report(root, name, **fields):
    report = {'objects': [], 'pages': 1, 'listing_total': None, 'complete': False,
              'reason': 'page_budget_reached', 'error': '', 'links_file': ''}
    report.update(fields)
    (Path(root) / name).write_text(json.dumps(report), encoding='utf-8')


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


class GapCollectTests(unittest.TestCase):
    def test_no_gap_does_not_collect(self):
        # /* ===== 步骤1：核对无缺口；数据源：完整对象；操作：1) 不调用采集 2) 前后 summary 一致 ===== */
        logger.info('开始无缺口不动手测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 1, 2)
            add_rows(folder, 'sight-1', 2)
            collector = Mock(side_effect=AssertionError('无缺口时不该采集'))
            report = collect_gaps(folder, 5, collector=collector)
            collector.assert_not_called()
            self.assertEqual(report['reason'], 'no_gap')
            self.assertEqual((report['selected'], report['processed']), ([], []))
            self.assertEqual(report['before'], report['after'])
            self.assertEqual(report['before']['missing'], 0)
        logger.info('无缺口不动手测试完成')

    def test_candidates_are_sorted_by_missing_and_limited(self):
        # /* ===== 步骤2：核对排序限量；数据源：三个缺口对象；操作：1) 按缺口降序 2) 只处理 gap-limit 个 ===== */
        logger.info('开始缺口排序与限量测试')
        with TemporaryDirectory() as folder:
            for identity, total in ((1, 100), (2, 50), (3, 20)):
                object_snapshot(folder, identity, total)
            seen = []
            report = collect_gaps(folder, 3, gap_limit=2, collector=collector_writing(3, seen))
            self.assertEqual(report['reason'], 'gap_processed')
            # 2.1 缺口 100 和 50 优先，20 那个不处理；顺序稳定。
            self.assertEqual(report['selected'], ['sight-1', 'sight-2'])
            self.assertEqual([item['object_key'] for item in report['processed']], ['sight-1', 'sight-2'])
            self.assertEqual(seen, ['https://you.ctrip.com/sight/a1/1.html',
                                    'https://you.ctrip.com/sight/a1/2.html'])
            self.assertEqual([item['before_missing'] for item in report['processed']], [100, 50])
            self.assertEqual([item['after_collected'] for item in report['processed']], [3, 3])
            self.assertEqual(report['after']['missing'], 170 - 6)
        logger.info('缺口排序与限量测试完成')

    def test_objects_without_url_or_known_target_are_skipped_with_reason(self):
        # /* ===== 步骤3：核对跳过原因；数据源：缺链接与未知目标；操作：1) 不采集 2) 报 gap_url_missing / target_unknown_only ===== */
        logger.info('开始缺链接与未知目标跳过测试')
        with TemporaryDirectory() as folder:
            root = Path(folder)
            discovery_report(root, 'discovery-sight-2-20261126T000000000000Z.json', listing_total=50,
                             objects=[{'kind': 'sight', 'resource_id': 7, 'name': '缺链接', 'comment_count': 10},
                                      {'kind': 'sight', 'resource_id': 8, 'name': '未知目标',
                                       'url': 'https://you.ctrip.com/sight/a1/8.html'}])
            collector = Mock(side_effect=AssertionError('没有可采对象时不该采集'))
            report = collect_gaps(folder, 5, collector=collector)
            collector.assert_not_called()
            self.assertEqual(report['reason'], 'gap_url_missing')
            self.assertEqual(report['processed'], [])
            # 3.1 目标未知的对象仍不计入目标，也不需要续采。
            self.assertEqual(report['before']['unknown_target'], 1)
        with TemporaryDirectory() as folder:
            discovery_report(Path(folder), 'discovery-sight-2-20261126T000000000000Z.json', listing_total=50,
                             objects=[{'kind': 'sight', 'resource_id': 8, 'name': '未知目标',
                                       'url': 'https://you.ctrip.com/sight/a1/8.html'}])
            report = collect_gaps(folder, 5, collector=collector)
            self.assertEqual(report['reason'], 'target_unknown_only')
            self.assertEqual(report['processed'], [])
        with TemporaryDirectory() as folder:
            # 3.2 酒店走专用浏览器并等人工登录，自动续采不拉起它。
            (Path(folder) / 'hotel-5-object.json').write_text(json.dumps(
                {'kind': 'hotel', 'resource_id': 5, 'name': '测试酒店', 'display_total': 8,
                 'url': 'https://hotels.ctrip.com/hotels/5.html'}), encoding='utf-8')
            report = collect_gaps(folder, 5, collector=collector)
            self.assertEqual(report['reason'], 'hotel_object_only')
            self.assertEqual(report['processed'], [])
        # 3.3 没有对象与只有未知目标要分开说。
        self.assertEqual(idle_reason([]), 'no_objects')
        logger.info('缺链接与未知目标跳过测试完成')

    def test_collection_failure_keeps_reason_and_is_not_success(self):
        # /* ===== 步骤4：核对采集失败；数据源：模拟接口错误；操作：1) 保留 reason/error 2) 缺口不变 ===== */
        logger.info('开始采集失败保留原因测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 5, 10)
            collector = Mock(side_effect=RuntimeError('verification required'))
            report = collect_gaps(folder, 5, collector=collector)
            item = report['processed'][0]
            self.assertEqual(item['object_key'], 'sight-5')
            self.assertEqual(item['reason'], 'error')
            self.assertIn('verification required', item['error'])
            self.assertEqual(item['export_error'], '')
            self.assertEqual(item['after_collected'], 0)
            self.assertEqual((item['before_missing'], item['after_missing']), (10, 10))
            self.assertEqual(report['after']['missing'], report['before']['missing'])
            self.assertFalse(report['after']['complete'])
        logger.info('采集失败保留原因测试完成')

    def test_collection_shrinks_missing(self):
        # /* ===== 步骤5：核对缺口下降；数据源：模拟成功采集；操作：1) 重新对账 2) after.missing 小于 before.missing ===== */
        logger.info('开始缺口下降测试')
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 6, 10)
            report = collect_gaps(folder, 5, collector=collector_writing(4, []))
            self.assertLess(report['after']['missing'], report['before']['missing'])
            self.assertEqual((report['before']['missing'], report['after']['missing']), (10, 6))
            self.assertEqual((report['before']['collected'], report['after']['collected']), (0, 4))
            item = report['processed'][0]
            self.assertEqual((item['before_missing'], item['after_collected'], item['reason']),
                             (10, 4, 'target_reached'))
            self.assertFalse(report['after']['complete'])
        logger.info('缺口下降测试完成')

    def test_cli_collect_gaps_uses_existing_collection_entry(self):
        # /* ===== 步骤6：核对命令入口；数据源：本地目录；操作：1) 传入 limit/gap-limit 2) 走既有采集入口 ===== */
        logger.info('开始缺口续采命令测试')
        import app
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 9, 3)
            seen = []
            with patch('sys.argv', ['app.py', '--collect-gaps', '--output', folder,
                                    '--limit', '2', '--gap-limit', '1']), \
                    patch('app.collect_url', side_effect=collector_writing(2, seen)), \
                    patch('sys.stdout', new_callable=io.StringIO) as stdout:
                app.main()
                result = json.loads(stdout.getvalue())
            self.assertEqual(result['reason'], 'gap_processed')
            self.assertEqual(result['selected'], ['sight-9'])
            self.assertEqual((result['before']['missing'], result['after']['missing']), (3, 1))
            self.assertEqual(result['processed'][0]['reason'], 'target_reached')
            self.assertEqual(len(seen), 1)
            self.assertFalse(result['after']['complete'])
        logger.info('缺口续采命令测试完成')

    def test_cli_without_collectable_gap_returns_zero(self):
        # /* ===== 步骤7：核对空跑；数据源：已完整对象；操作：1) 输出 no_gap 2) 不抛错、不采集 ===== */
        logger.info('开始无缺口命令测试')
        import app
        with TemporaryDirectory() as folder:
            object_snapshot(folder, 2, 1)
            add_rows(folder, 'sight-2', 1)
            with patch('sys.argv', ['app.py', '--collect-gaps', '--output', folder, '--limit', '5']), \
                    patch('app.collect_url', side_effect=AssertionError('无缺口时不该采集')), \
                    patch('sys.stdout', new_callable=io.StringIO) as stdout:
                app.main()
                result = json.loads(stdout.getvalue())
            self.assertEqual(result['reason'], 'no_gap')
            self.assertEqual(result['processed'], [])
            self.assertEqual(result['before'], result['after'])
        logger.info('无缺口命令测试完成')