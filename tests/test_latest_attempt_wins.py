"""手机状态只看最新一次尝试：后来的成功报告要能清掉旧的未知布局。"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from coverage_audit import write_coverage_audit


def snapshot(folder, identity, name='景点'):
    (Path(folder) / f'sight-{identity}-object.json').write_text(json.dumps(
        {'kind': 'sight', 'resource_id': identity, 'name': name, 'display_total': 10,
         'url': f'https://you.ctrip.com/sight/a1/{identity}.html'}, ensure_ascii=False), encoding='utf-8')


def report(folder, identity, run_id, payload, mtime):
    path = Path(folder) / f'report-{run_id}.json'
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    os.utime(path, (mtime, mtime))
    return path


class LatestAttemptWinsTests(unittest.TestCase):
    def test_success_after_failure_clears_layout_unknown(self):
        # /* ===== 步骤1：先认不出、后成功；数据源：两份手机报告；操作：1) 不再算未知布局 ===== */
        report_old = {'kind': 'sight', 'object_id': '11', 'place': '景点',
                      'reason': 'arrival_failed', 'arrival_reason': 'no_review_anchor'}
        report_new = {'kind': 'sight', 'object_id': '11', 'place': '景点', 'reason': 'target_scanned',
                      'sampling_context': {'observations': [
                          {'visible_filters': [{'label': '全部', 'display_count': 42}]}]}}
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, 11)
            report(root, 11, '20261128T100000-aaa111', report_old, 1_700_000_000)
            report(root, 11, '20261128T110000-bbb222', report_new, 1_700_000_900)
            result = write_coverage_audit(root)
            row = [item for item in result['objects'] if item['object_key'] == 'sight-11'][0]
            # 1.1 已经采到「全部 42」，旧的失败状态必须被清掉。
            self.assertFalse(row['phone_layout_unknown'])
            self.assertEqual(row['latest_phone_reason'], 'target_scanned')
            self.assertEqual(row['phone_target'], 42)
            self.assertEqual(result['summary']['phone_layout_unknown'], 0)

    def test_failure_after_success_is_reported(self):
        # /* ===== 步骤2：先成功、后认不出；数据源：反序两份报告；操作：1) 算未知布局 ===== */
        scan = {'kind': 'sight', 'object_id': '12', 'place': '景点', 'reason': 'target_scanned',
                'sampling_context': {'observations': [
                    {'visible_filters': [{'label': '全部', 'display_count': 7}]}]}}
        failure = {'kind': 'sight', 'object_id': '12', 'place': '景点',
                   'reason': 'arrival_failed', 'arrival_reason': 'no_review_anchor'}
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            snapshot(root, 12)
            report(root, 12, '20261128T100000-ccc333', scan, 1_700_000_000)
            report(root, 12, '20261128T110000-ddd444', failure, 1_700_000_900)
            row = [item for item in write_coverage_audit(root)['objects']
                   if item['object_key'] == 'sight-12'][0]
            # 2.1 目标还在，但最新一次尝试确实没认出页面，两者都要如实保留。
            self.assertTrue(row['phone_layout_unknown'])
            self.assertEqual(row['phone_target'], 7)
            self.assertEqual(row['phone_arrival_reason'], 'no_review_anchor')


if __name__ == '__main__':
    unittest.main()