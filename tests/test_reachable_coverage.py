"""网页通道的「可达覆盖率」：到顶对象按已采量计入可得量。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path

from coverage_audit import write_coverage_audit

logger = logging.getLogger(__name__)


def snapshot(folder, identity, total):
    (Path(folder) / f'sight-{identity}-object.json').write_text(json.dumps(
        {'kind': 'sight', 'resource_id': identity, 'name': f'景点{identity}', 'display_total': total,
         'url': f'https://you.ctrip.com/sight/a1/{identity}.html'}), encoding='utf-8')


def capped_reports(folder, identity, pages=2):
    """连续两份空页报告 → 对账判通道到顶。"""
    for index in range(pages):
        # 报告名里的时间戳只能含字母数字：带连字符会匹配不上报告正则，对象就看不到这两份空页。
        name = f'sight-{identity}-report-20261128T12000{index}00Z.json'
        (Path(folder) / name).write_text(json.dumps(
            {'object_key': f'sight-{identity}', 'kind': 'sight', 'reason': 'empty_page',
             'error': '', 'pages': 3, 'count': 0}), encoding='utf-8')


def seed_rows(folder, identity, amount):
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(Path(folder) / 'network.sqlite3')) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS network_reviews '
                   '(object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
        db.executemany('INSERT INTO network_reviews VALUES(?,?,?,?)',
                       [(f'sight-{identity}', f'c{index}', '{}', 't') for index in range(amount)])


class ReachableCoverageTests(unittest.TestCase):
    def test_capped_object_counts_its_collected_as_reachable(self):
        # /* ===== 步骤1：到顶对象；数据源：目标 1000 但只采到 300；操作：1) 可达量=300 2) ratio=1.0 ===== */
        logger.info('开始可达覆盖测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, 1, 1000)
            seed_rows(root, 1, 300)
            capped_reports(root, 1)
            web = write_coverage_audit(root)['summary']['coverage']['web']
            # 1.1 相对平台总数只有 30%，但相对「网页通道能给的量」已经拿满。
            self.assertEqual(web['target'], 1000)
            self.assertEqual(web['collected'], 300)
            self.assertEqual(web['reachable']['target'], 300)
            self.assertEqual(web['reachable']['ratio'], 1.0)
            self.assertEqual(web['reachable']['capped_objects'], 1)
        logger.info('可达覆盖测试完成')

    def test_partial_object_keeps_platform_target_in_reachable(self):
        # /* ===== 步骤2：没到顶；数据源：目标 500 采到 100；操作：1) 可达量仍是 500 ===== */
        logger.info('开始未到顶可达量测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, 2, 500)
            seed_rows(root, 2, 100)
            web = write_coverage_audit(root)['summary']['coverage']['web']
            self.assertEqual(web['reachable']['target'], 500)
            self.assertEqual(web['reachable']['ratio'], 0.2)
            self.assertEqual(web['reachable']['capped_objects'], 0)
        logger.info('未到顶可达量测试完成')


if __name__ == '__main__':
    unittest.main()