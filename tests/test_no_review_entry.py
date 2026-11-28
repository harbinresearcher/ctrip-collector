"""没有点评入口的对象要单独成类，不能算「还没采」。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path

from coverage_audit import write_coverage_audit

logger = logging.getLogger(__name__)


def snapshot(folder, identity, name='佛库伦湖'):
    (Path(folder) / f'sight-{identity}-object.json').write_text(json.dumps(
        {'kind': 'sight', 'resource_id': identity, 'name': name, 'display_total': 0,
         'url': f'https://you.ctrip.com/sight/a1/{identity}.html'}, ensure_ascii=False), encoding='utf-8')


def report(folder, identity, reason, quiet=True):
    data = {'kind': 'sight', 'object_id': str(identity), 'place': '佛库伦湖', 'reason': reason,
            'count': 0, 'detail_seen': True, 'review_entry_missing': quiet}
    (Path(folder) / f'report-20261128T1300{identity:02d}-abcde1.json').write_text(
        json.dumps(data, ensure_ascii=False), encoding='utf-8')


class NoReviewEntryTests(unittest.TestCase):
    def test_object_without_review_entry_is_flagged(self):
        # /* ===== 步骤1：页面没有点评入口；数据源：no_review_entry 报告；操作：1) 单独计数 ===== */
        logger.info('开始无点评入口测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, 11)
            report(root, 11, 'no_review_entry', quiet=True)
            result = write_coverage_audit(root)
            row = [item for item in result['objects'] if item['object_key'] == 'sight-11'][0]
            # 1.1 既不是未知布局，也不该被当成「还没扫」。
            self.assertTrue(row['phone_no_review_entry'])
            self.assertFalse(row['phone_layout_unknown'])
            self.assertEqual(result['summary']['phone_no_review_entry'], 1)
        logger.info('无点评入口测试完成')

    def test_normal_scan_is_not_flagged(self):
        # /* ===== 步骤2：正常扫描；数据源：target_scanned；操作：1) 不误标 ===== */
        logger.info('开始正常扫描不误标测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, 12)
            report(root, 12, 'target_scanned', quiet=False)
            result = write_coverage_audit(root)
            row = [item for item in result['objects'] if item['object_key'] == 'sight-12'][0]
            self.assertFalse(row['phone_no_review_entry'])
            self.assertEqual(result['summary']['phone_no_review_entry'], 0)
        logger.info('正常扫描不误标测试完成')


if __name__ == '__main__':
    unittest.main()