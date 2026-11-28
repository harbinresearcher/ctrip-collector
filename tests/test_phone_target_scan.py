"""手机侧「全部」计数只读扫描：报告要能被对账读成 phone_target。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coverage_audit import write_coverage_audit
from runner import Adb, scan_phone_target

logger = logging.getLogger(__name__)

FIXTURE = Path(__file__).parent / 'fixtures' / 'sight-review-live-yungang.xml'


class ScanPhoneTargetTests(unittest.TestCase):
    def test_scan_writes_report_with_all_count(self):
        # /* ===== 步骤1：只读全部计数；数据源：真实点评页样本；操作：1) 落报告 2) 不采点评 ===== */
        logger.info('开始全部计数扫描测试')
        xml = FIXTURE.read_text('utf-8')
        with tempfile.TemporaryDirectory() as folder:
            adb = Adb('adb', 'test')
            with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', return_value=xml):
                report = scan_phone_target(adb, 'sight', '云冈石窟', Path(folder), object_id='143364',
                                           identity_source='evidence_manifest')
            # 1.1 只读不写点评：count 必须是 0，但报告要带上屏幕上的「全部」数字。
            self.assertEqual(report['reason'], 'target_scanned')
            self.assertEqual(report['count'], 0)
            self.assertIsNone(report['arrival_reason'])
            self.assertEqual(report['object_id'], '143364')
            filters = report['sampling_context']['observations'][0]['visible_filters']
            self.assertTrue(any(entry['label'] == '全部' for entry in filters))
            self.assertTrue(list(Path(folder).glob('report-*.json')))
        logger.info('全部计数扫描测试完成')

    def test_audit_reads_scanned_target(self):
        exact_fixture = FIXTURE.with_name('ctrip-comments2.xml')
        cases = ((exact_fixture, '长白山西景区', 136039, 1095), (FIXTURE, '云冈石窟', 11311, None))
        for path, name, identity, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                key = f'sight-{identity}'
                (root / f'{key}-object.json').write_text(json.dumps(
                    {'kind': 'sight', 'resource_id': identity, 'name': name, 'display_total': 100,
                     'url': f'https://you.ctrip.com/sight/a1/{identity}.html'}), encoding='utf-8')
                adb = Adb('adb', 'test')
                with patch.object(adb, 'ensure_foreground'), \
                        patch.object(adb, 'snapshot', return_value=path.read_text('utf-8')):
                    report = scan_phone_target(adb, 'sight', name, root, object_id=str(identity),
                                               identity_source='evidence_manifest')
                self.assertEqual(report['reason'], 'target_scanned')
                row = next(item for item in write_coverage_audit(root)['objects'] if item['object_key'] == key)
                self.assertEqual(row['phone_target'], expected)
                self.assertEqual(row['phone_target_source'], 'all_label' if expected is not None else None)
                self.assertEqual(row['phone_collected'], 0)

    def test_scan_on_wrong_screen_reports_arrival_failed(self):
        # /* ===== 步骤3：没到点评页；数据源：酒店详情页样本；操作：1) 记 arrival_failed 2) 仍落报告 ===== */
        logger.info('开始扫描未到达测试')
        xml = (Path(__file__).parent / 'fixtures' / 'hotel-detail.xml').read_text('utf-8')
        with tempfile.TemporaryDirectory() as folder:
            adb = Adb('adb', 'test')
            with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', return_value=xml):
                report = scan_phone_target(adb, 'hotel', '样例酒店', Path(folder), object_id='25045127',
                                           identity_source='evidence_manifest')
            # 3.1 未知布局也是可数缺口：报告必须落盘并带 arrival_reason。
            self.assertEqual(report['reason'], 'arrival_failed')
            self.assertTrue(report['arrival_reason'])
            self.assertTrue(list(Path(folder).glob('report-*.json')))
        logger.info('扫描未到达测试完成')


if __name__ == '__main__':
    unittest.main()
