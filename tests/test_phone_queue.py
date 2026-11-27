"""手机侧缺口队列（--phone-queue）：只读对账与手机库，不触网、不连设备。"""
import io
import json
import logging
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from phone_queue import write_phone_queue

logger = logging.getLogger(__name__)


def snapshot(root, kind='sight', identity=1, total=100, url=None):
    # 网页侧对象快照：只用来提供对象键、名称与链接，手机侧缺口不看这里的数字。
    data = {'kind': kind, 'resource_id': identity, 'name': f'景点{identity}', 'display_total': total}
    if url is not False:
        data['url'] = url or f'https://you.ctrip.com/sight/a1/{identity}.html'
    (Path(root) / f'{kind}-{identity}-object.json').write_text(
        json.dumps(data, ensure_ascii=False), encoding='utf-8')


def phone_report(root, run_id, kind, identity, place, *, reason='target_reached', counts=None):
    data = {'kind': kind, 'object_id': str(identity), 'place': place, 'reason': reason,
            'run_id': run_id, 'count': 0}
    if counts is not None:
        data['sampling_context'] = {'scope': 'current_run_ui_controls_only',
                                    'observations': [{'screen': 1, 'visible_filters': [
                                        {'label': label, 'display_count': value} for label, value in counts]}]}
    (Path(root) / f'report-{run_id}.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def phone_rows(root, kind='sight', identity=1, amount=3):
    with closing(sqlite3.connect(Path(root) / 'reviews.sqlite3')) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS reviews_scoped '
                   '(key TEXT PRIMARY KEY, kind TEXT, object_id TEXT, content TEXT)')
        db.executemany('INSERT INTO reviews_scoped VALUES(?,?,?,?)',
                       [(f'{kind}-{identity}-{index}', kind, str(identity), f'第{index}条')
                        for index in range(amount)])


def blocked_web_report(root, identity=5):
    # 今天的网页被拦报告：对账据此把对象标 blocked，只在当天挡跑。
    (Path(root) / f'sight-{identity}-report-20261127T120000000000Z.json').write_text(
        json.dumps({'object_key': f'sight-{identity}', 'kind': 'sight', 'reason': 'error',
                    'error': 'HTTP Error 432', 'blocked': True, 'blocked_status': '432'}), encoding='utf-8')


class PhoneQueueTests(unittest.TestCase):
    def test_never_collected_entry_points_at_object_ref(self):
        # /* ===== 步骤1：从未采过；数据源：只有网页证据、没有手机库；操作：1) 走命令行 2) 命令带对象引用 ===== */
        logger.info('开始从未采过队列测试')
        import app
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, identity=7, total=50)
            with patch('sys.argv', ['app.py', '--phone-queue', '--output', folder]), \
                    patch('sys.stdout', new_callable=io.StringIO) as stdout:
                app.main()
                result = json.loads(stdout.getvalue())
            self.assertEqual(len(result['queue']), 1)
            entry = result['queue'][0]
            self.assertEqual((entry['object_key'], entry['reason'], entry['phone_collected']),
                             ('sight-7', 'never_collected', 0))
            # 1.1 没有手机报告就没有目标，不能拿网页目标或已采数冒充。
            self.assertIsNone(entry['target'])
            self.assertTrue(entry['target_unknown'])
            self.assertIsNone(entry['missing'])
            self.assertEqual(entry['limit_suggestion'], 200)
            self.assertIn('200', entry['limit_note'])
            self.assertIn('--collect --object-ref sight-7 --limit 200 --output', entry['command'])
            self.assertEqual(entry['url'], 'https://you.ctrip.com/sight/a1/7.html')
            self.assertEqual(result['phone_database'], 'missing')
            self.assertEqual(result['summary'], {'objects': 1, 'with_phone_gap': 0, 'never_collected': 1,
                                                 'target_unknown': 1, 'skipped': 0})
        logger.info('从未采过队列测试完成')

    def test_partially_collected_reports_missing_and_sorts_after_never_collected(self):
        # /* ===== 步骤2：部分已采；数据源：手机报告「全部」计数 + 手机库 4 条；操作：1) 算缺口 2) 排在从未采之后 ===== */
        logger.info('开始部分已采队列测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, identity=1, total=50)
            snapshot(root, identity=2, total=50)
            phone_report(root, '20261127T000000-abcdef', 'sight', 2, '景点2',
                         counts=[('全部', 10), ('好评', 8)])
            phone_rows(root, 'sight', 2, 4)
            result = write_phone_queue(root, 10)
            self.assertEqual([entry['object_key'] for entry in result['queue']], ['sight-1', 'sight-2'])
            partial = result['queue'][1]
            self.assertEqual((partial['reason'], partial['target'], partial['phone_collected'],
                              partial['missing'], partial['limit_suggestion']),
                             ('partial', 10, 4, 6, 6))
            # 2.1 --collect 的 --limit 是累计目标：已有 4 条、再补 6 条，命令里是 10。
            self.assertIn('--limit 10', partial['command'])
            self.assertIsNone(partial['limit_note'])
            self.assertEqual(result['summary'], {'objects': 2, 'with_phone_gap': 1, 'never_collected': 1,
                                                 'target_unknown': 1, 'skipped': 0})
        logger.info('部分已采队列测试完成')

    def test_unknown_target_is_null_with_200_suggestion(self):
        # /* ===== 步骤3：目标未知；数据源：手机报告没有「全部」精确计数；操作：1) target=null 2) 按 200 试采 ===== */
        logger.info('开始目标未知队列测试')
        cases = ({'counts': [('好评', 5)]}, {'counts': [('全部', '4.3w')]})
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
                root = Path(folder)
                snapshot(root, identity=3, total=50)
                phone_rows(root, 'sight', 3, 2)
                phone_report(root, '20261127T000000-abcdef', 'sight', 3, '景点3', **case)
                result = write_phone_queue(root, 10)
                entry = result['queue'][0]
                self.assertEqual((entry['reason'], entry['target'], entry['target_unknown'],
                                  entry['phone_collected'], entry['missing']),
                                 ('target_unknown', None, True, 2, None))
                self.assertEqual(entry['limit_suggestion'], 200)
                self.assertIn('目标未知', entry['limit_note'])
                # 3.1 已有 2 条时命令换算成累计目标 202，仍然只追加 200 条。
                self.assertIn('--limit 202', entry['command'])
                self.assertEqual(result['summary']['target_unknown'], 1)
                self.assertEqual(result['summary']['with_phone_gap'], 0)
        logger.info('目标未知队列测试完成')

    def test_exhausted_and_blocked_are_skipped_not_queued(self):
        # /* ===== 步骤4：采尽与被拦；数据源：手机报告 stalled_unverified + 今天的网页 blocked；操作：1) 进 skipped 2) 不入队 ===== */
        logger.info('开始采尽与被拦跳过测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, identity=4, total=20)
            phone_rows(root, 'sight', 4, 5)
            phone_report(root, '20261127T000000-abcdef', 'sight', 4, '景点4',
                         reason='stalled_unverified', counts=[('全部', 20)])
            snapshot(root, identity=5, total=100)
            blocked_web_report(root, identity=5)
            result = write_phone_queue(root, 10)
            self.assertEqual(result['queue'], [])
            self.assertEqual({item['object_key']: item['reason'] for item in result['skipped']},
                             {'sight-4': 'source_exhausted', 'sight-5': 'blocked'})
            self.assertTrue(all(item['detail'] for item in result['skipped']))
            self.assertEqual(result['summary'], {'objects': 2, 'with_phone_gap': 1, 'never_collected': 1,
                                                 'target_unknown': 1, 'skipped': 2})
        logger.info('采尽与被拦跳过测试完成')

    def test_order_and_max_objects_limit_are_reproducible(self):
        # /* ===== 步骤5：排序与截断；数据源：四个对象；操作：1) never 在前 2) missing 降序 3) 两次结果一致 ===== */
        logger.info('开始排序与截断测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            # 四个对象：缺口 40 / 28 / 9，外加一个从未采过（缺口 5）。
            for identity, total, collected in ((11, 10, 1), (12, 30, 2), (13, 5, 0), (14, 50, 10)):
                snapshot(root, identity=identity, total=total)
                if collected:
                    phone_rows(root, 'sight', identity, collected)
                phone_report(root, f'20261127T00000{identity}-abcdef', 'sight', identity,
                             f'景点{identity}', counts=[('全部', total)])
            first = write_phone_queue(root, 10)
            second = write_phone_queue(root, 10)
            order = [entry['object_key'] for entry in first['queue']]
            self.assertEqual(order, ['sight-13', 'sight-14', 'sight-12', 'sight-11'])
            self.assertEqual([entry['missing'] for entry in first['queue']], [5, 40, 28, 9])
            self.assertEqual(first['queue'], second['queue'])
            self.assertEqual(first['summary']['with_phone_gap'], 4)
            truncated = write_phone_queue(root, 2)
            self.assertEqual([entry['object_key'] for entry in truncated['queue']], ['sight-13', 'sight-14'])
            self.assertEqual(truncated['summary'], first['summary'])
        # 5.1 命令行不给 --max-objects 时默认只列 10 个，且不改其他模式的 50。
        import app
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            for identity in range(21, 33):
                snapshot(root, identity=identity, total=100)
                phone_rows(root, 'sight', identity, identity - 20)
                phone_report(root, f'20261127T01{identity:04d}-abcdef', 'sight', identity,
                             f'景点{identity}', counts=[('全部', 100)])
            with patch('sys.argv', ['app.py', '--phone-queue', '--output', folder]), \
                    patch('sys.stdout', new_callable=io.StringIO) as stdout:
                app.main()
                result = json.loads(stdout.getvalue())
            self.assertEqual(len(result['queue']), 10)
            self.assertEqual([entry['object_key'] for entry in result['queue']],
                             [f'sight-{identity}' for identity in range(21, 31)])
            self.assertEqual(result['summary']['objects'], 12)
        logger.info('排序与截断测试完成')


if __name__ == '__main__':
    unittest.main()