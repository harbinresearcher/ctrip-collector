"""以最新一次手机尝试为准：后来查明「没有点评」就不该再算未知布局。"""
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path

from coverage_audit import write_coverage_audit

logger = logging.getLogger(__name__)


def snapshot(folder, identity, name='景点'):
    (Path(folder) / f'sight-{identity}-object.json').write_text(json.dumps(
        {'kind': 'sight', 'resource_id': identity, 'name': name, 'display_total': 10,
         'url': f'https://you.ctrip.com/sight/a1/{identity}.html'}, ensure_ascii=False), encoding='utf-8')


def write_report(folder, identity, run_id, payload):
    path = Path(folder) / f'report-{run_id}.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return path


class LatestPhoneAttemptTests(unittest.TestCase):
    def test_newer_no_review_report_clears_layout_unknown(self):
        # /* ===== 步骤1：先未知布局、后查明没有点评；数据源：两份报告；操作：1) 不算未知布局 ===== */
        logger.info('开始最新尝试优先测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, 11)
            first = write_report(root, 11, '20261128T100000-aaa111',
                                 {'kind': 'sight', 'object_id': '11', 'place': '景点',
                                  'reason': 'arrival_failed', 'arrival_reason': 'no_review_anchor'})
            second = write_report(root, 11, '20261128T110000-bbb222',
                                  {'kind': 'sight', 'object_id': '11', 'place': '景点',
                                   'reason': 'no_review_entry', 'review_entry_missing': True})
            os.utime(first, (1_700_000_000, 1_700_000_000))
            os.utime(second, (1_700_000_100, 1_700_000_100))
            report = write_coverage_audit(root)
            row = [item for item in report['objects'] if item['object_key'] == 'sight-11'][0]
            # 1.1 已经查明平台没有点评，就不该继续挂在「未知布局」里。
            self.assertFalse(row['phone_layout_unknown'])
            self.assertTrue(row['phone_no_review_entry'])
            self.assertEqual(report['summary']['phone_layout_unknown'], 0)
        logger.info('最新尝试优先测试完成')

    def test_older_no_review_does_not_hide_newer_unknown_layout(self):
        # /* ===== 步骤2：先没有点评、后又认不出；数据源：反序两份报告；操作：1) 仍算未知布局 ===== */
        logger.info('开始反序报告测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, 12)
            old = write_report(root, 12, '20261128T090000-ccc333',
                               {'kind': 'sight', 'object_id': '12', 'place': '景点',
                                'reason': 'no_review_entry', 'review_entry_missing': True})
            new = write_report(root, 12, '20261128T120000-ddd444',
                               {'kind': 'sight', 'object_id': '12', 'place': '景点',
                                'reason': 'arrival_failed', 'arrival_reason': 'no_review_anchor'})
            os.utime(old, (1_700_000_000, 1_700_000_000))
            os.utime(new, (1_700_000_900, 1_700_000_900))
            row = [item for item in write_coverage_audit(root)['objects']
                   if item['object_key'] == 'sight-12'][0]
            self.assertTrue(row['phone_layout_unknown'])
        logger.info('反序报告测试完成')


if __name__ == '__main__':
    unittest.main()