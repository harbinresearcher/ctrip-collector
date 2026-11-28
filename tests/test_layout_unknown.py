"""手机侧未知布局（没认出点评页）要成为可计数的缺口。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path

from coverage_audit import write_coverage_audit

logger = logging.getLogger(__name__)


def snapshot(folder, identity, total=10):
    (Path(folder) / f'sight-{identity}-object.json').write_text(json.dumps(
        {'kind': 'sight', 'resource_id': identity, 'name': f'景点{identity}', 'display_total': total,
         'url': f'https://you.ctrip.com/sight/a1/{identity}.html'}), encoding='utf-8')


def phone_report(folder, identity, reason, arrival_reason=None):
    data = {'kind': 'sight', 'object_id': str(identity), 'place': f'景点{identity}',
            'reason': reason, 'count': 0, 'identity_source': 'evidence_manifest'}
    if arrival_reason is not None:
        data['arrival_reason'] = arrival_reason
    (Path(folder) / f'report-20261128T1200{identity:02d}-abcde1.json').write_text(
        json.dumps(data, ensure_ascii=False), encoding='utf-8')


def row_for(root, key):
    return [row for row in write_coverage_audit(root)['objects'] if row['object_key'] == key][0]


class PhoneLayoutUnknownTests(unittest.TestCase):
    def test_phone_arrival_failure_is_flagged_and_counted(self):
        # /* ===== 步骤1：手机没认出点评页；数据源：arrival_failed 报告；操作：1) 标出 2) 计数 ===== */
        logger.info('开始手机未知布局测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, 11, 100)
            phone_report(root, 11, 'arrival_failed', 'no_review_anchor')
            report = write_coverage_audit(root)
            row = row_for(root, 'sight-11')
            # 1.1 网页缺口原因不受影响，但手机侧要留下「没认出页面」的记录。
            self.assertTrue(row['phone_layout_unknown'])
            self.assertEqual(row['phone_arrival_reason'], 'no_review_anchor')
            self.assertEqual(report['summary']['phone_layout_unknown'], 1)
        logger.info('手机未知布局测试完成')

    def test_normal_phone_report_is_not_flagged(self):
        # /* ===== 步骤2：正常手机报告；数据源：target_reached；操作：1) 不误标 ===== */
        logger.info('开始正常手机报告测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, 12, 5)
            phone_report(root, 12, 'target_reached')
            report = write_coverage_audit(root)
            row = row_for(root, 'sight-12')
            self.assertFalse(row['phone_layout_unknown'])
            self.assertIsNone(row['phone_arrival_reason'])
            self.assertEqual(report['summary']['phone_layout_unknown'], 0)
        logger.info('正常手机报告测试完成')


if __name__ == '__main__':
    unittest.main()