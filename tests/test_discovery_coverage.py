"""对象发现覆盖：同一城市多份目录报告只取最新一份，不重复累加。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path

from coverage_audit import write_coverage_audit

logger = logging.getLogger(__name__)


def object_snapshot(folder, identity, total):
    (folder / f'sight-{identity}-object.json').write_text(json.dumps(
        {'kind': 'sight', 'resource_id': identity, 'name': f'景点{identity}', 'display_total': total,
         'url': f'https://you.ctrip.com/sight/a1/{identity}.html'}), encoding='utf-8')


def directory_report(folder, stamp, listing_total, objects):
    rows = [{'kind': 'sight', 'resource_id': identity, 'name': f'景点{identity}'} for identity in objects]
    (folder / f'discovery-sight-2479-{stamp}.json').write_text(json.dumps(
        {'kind': 'sight', 'scope_id': 2479, 'listing_total': listing_total, 'objects': rows,
         'reason': 'page_budget_reached'}, ensure_ascii=False), encoding='utf-8')


class DiscoveryCoverageTests(unittest.TestCase):
    def test_repeated_discovery_is_not_double_counted(self):
        # /* ===== 步骤1：同一城市两份目录；数据源：两次发现报告；操作：1) 只取最新 2) 不累加 ===== */
        logger.info('开始目录去重测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            object_snapshot(root, 1, 10)
            object_snapshot(root, 2, 20)
            directory_report(root, '20261128T020645993604Z', 3000, [1, 2])
            directory_report(root, '20261128T021645993604Z', 3000, [1, 2])
            discovery = write_coverage_audit(root)['summary']['coverage']['discovery']
            # 3000 是整千值 → 按平台截断值处理：不给分母也不给比值，但对象数不重复累加。
            self.assertTrue(discovery['listing_total_suspect'])
            self.assertIsNone(discovery['listing_total'])
            self.assertEqual(discovery['discovered'], 2)
            self.assertIsNone(discovery['ratio'])
        logger.info('目录去重测试完成')

    def test_no_directory_report_gives_null_target(self):
        # /* ===== 步骤2：没有目录报告；数据源：只有对象快照；操作：1) listing_total 为 None ===== */
        logger.info('开始缺目录报告测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            object_snapshot(root, 3, 5)
            discovery = write_coverage_audit(root)['summary']['coverage']['discovery']
            self.assertIsNone(discovery['listing_total'])
            self.assertIsNone(discovery['ratio'])
            self.assertEqual(discovery['discovered'], 0)
        logger.info('缺目录报告测试完成')


if __name__ == '__main__':
    unittest.main()