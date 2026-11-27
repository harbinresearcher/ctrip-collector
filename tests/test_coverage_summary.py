"""summary.coverage：网页与手机两条通道分开算，跨通道只取较大值、不相加。

四类情形各一项：两通道都有数据 / 只有网页 / 只有手机 / 目标未知不计入分母。
"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from coverage_audit import COVERAGE_NOTE, write_coverage_audit


def save_json(root, filename, **data):
    (root / filename).write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def web_rows(root, key, amount):
    with closing(sqlite3.connect(root / 'network.sqlite3')) as db, db:
        db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
        db.executemany('INSERT INTO network_reviews VALUES(?,?,?,?)',
                       [(key, str(index), '{}', 't') for index in range(amount)])


def phone_rows(root, kind, identity, amount):
    with closing(sqlite3.connect(root / 'reviews.sqlite3')) as db, db:
        db.execute('CREATE TABLE reviews_scoped (key TEXT PRIMARY KEY, kind TEXT, object_id TEXT)')
        db.executemany('INSERT INTO reviews_scoped VALUES(?,?,?)',
                       [(f'row-{index}', kind, identity) for index in range(amount)])


def phone_report(root, kind, identity, all_count):
    # 手机目标只从屏幕上看到的「全部」计数来，库里没有已核对运行时不升级成 confirmed_run。
    save_json(root, 'report-20261127T090000-abcdef.json', run_id='20261127T090000-abcdef',
              kind=kind, object_id=identity, place=f'景点{identity}', reason='target_reached',
              sampling_context={'observations': [{'visible_filters': [
                  {'label': '全部', 'display_count': all_count},
                  {'label': '好评', 'display_count': all_count}]}]})


def object_snapshot(root, identity=1, total=10):
    save_json(root, f'sight-{identity}-object.json', kind='sight', resource_id=identity,
              name=f'景点{identity}', display_total=total)


class CoverageSummaryTests(unittest.TestCase):
    def test_both_channels_are_reported_separately(self):
        # /* ===== 步骤1：两通道都有数据；数据源：快照/报告/两个库；操作：1) 分别汇总 2) 不相加 ===== */
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            object_snapshot(root, total=10)
            web_rows(root, 'sight-1', 4)
            phone_report(root, 'sight', '1', 6)
            phone_rows(root, 'sight', '1', 3)
            coverage = write_coverage_audit(root)['summary']['coverage']
            self.assertEqual(coverage['web'], {'target': 10, 'collected': 4, 'missing': 6})
            # 1.1 手机已采 3 不能被加到网页已采里：两条通道没有共同主键。
            self.assertEqual(coverage['phone'], {'target': 6, 'collected': 3, 'missing': 3})
            self.assertEqual(coverage['objects'], {'total': 1, 'complete': 0, 'partial': 1,
                                                   'unknown_target': 0, 'no_phone_data': 0})
            # 1.2 best_effort 取已采更多的那条通道（网页 4 > 手机 3），目标也用这条通道的。
            self.assertEqual(coverage['best_effort'], {'covered': 4, 'target': 10, 'ratio': 0.4})
            self.assertEqual(coverage['note'], COVERAGE_NOTE)

    def test_web_only_keeps_phone_target_null(self):
        # /* ===== 步骤2：只有网页；数据源：快照+网页库；操作：1) 手机目标 null 2) 已采仍给 0 ===== */
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            object_snapshot(root, total=10)
            web_rows(root, 'sight-1', 4)
            coverage = write_coverage_audit(root)['summary']['coverage']
            self.assertEqual(coverage['web'], {'target': 10, 'collected': 4, 'missing': 6})
            self.assertEqual(coverage['phone'], {'target': None, 'collected': 0, 'missing': None})
            self.assertEqual(coverage['objects'], {'total': 1, 'complete': 0, 'partial': 1,
                                                   'unknown_target': 0, 'no_phone_data': 1})
            self.assertEqual(coverage['best_effort'], {'covered': 4, 'target': 10, 'ratio': 0.4})

    def test_phone_only_can_complete_an_object(self):
        # /* ===== 步骤3：只有手机；数据源：手机报告+手机库；操作：1) 网页目标为 0 2) 手机达到目标算完整 ===== */
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            phone_report(root, 'sight', '2', 6)
            phone_rows(root, 'sight', '2', 6)
            coverage = write_coverage_audit(root)['summary']['coverage']
            self.assertEqual(coverage['web'], {'target': 0, 'collected': 0, 'missing': 0})
            self.assertEqual(coverage['phone'], {'target': 6, 'collected': 6, 'missing': 0})
            # 3.1 任一条通道达到目标即算完整，这条通道是手机。
            self.assertEqual(coverage['objects'], {'total': 1, 'complete': 1, 'partial': 0,
                                                   'unknown_target': 0, 'no_phone_data': 0})
            self.assertEqual(coverage['best_effort'], {'covered': 6, 'target': 6, 'ratio': 1.0})

    def test_unknown_target_is_not_counted_in_denominator(self):
        # /* ===== 步骤4：目标未知；数据源：快照无 display_total；操作：1) 计入 unknown_target 2) 不进分母 ===== */
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_json(root, 'sight-1-object.json', kind='sight', resource_id=1, name='景点1')
            web_rows(root, 'sight-1', 1)
            coverage = write_coverage_audit(root)['summary']['coverage']
            self.assertEqual(coverage['web'], {'target': 0, 'collected': 1, 'missing': 0})
            self.assertEqual(coverage['phone'], {'target': None, 'collected': 0, 'missing': None})
            self.assertEqual(coverage['objects'], {'total': 1, 'complete': 0, 'partial': 0,
                                                   'unknown_target': 1, 'no_phone_data': 1})
            # 4.1 没有目标就写 null，不用已采数当分母。
            self.assertEqual(coverage['best_effort'], {'covered': 1, 'target': 0, 'ratio': None})


if __name__ == '__main__':
    unittest.main()