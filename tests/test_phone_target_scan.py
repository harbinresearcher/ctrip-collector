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
        # /* ===== 步骤2：对账能读到目标；数据源：扫描报告 + 网页快照；操作：1) phone_target 非空 ===== */
        logger.info('开始扫描目标入账测试')
        xml = FIXTURE.read_text('utf-8')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sight-143364-object.json').write_text(json.dumps(
                {'kind': 'sight', 'resource_id': 143364, 'name': '云冈石窟', 'display_total': 100,
                 'url': 'https://you.ctrip.com/sight/a1/143364.html'}), encoding='utf-8')
            adb = Adb('adb', 'test')
            with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', return_value=xml):
                scan_phone_target(adb, 'sight', '云冈石窟', root, object_id='143364',
                                  identity_source='evidence_manifest')
            row = [item for item in write_coverage_audit(root)['objects']
                   if item['object_key'] == 'sight-143364'][0]
            # 2.1 目标来自扫描报告，来源标注仍然是「屏幕上看到的全部」。
            self.assertIsNotNone(row['phone_target'])
            self.assertEqual(row['phone_target_source'], 'all_label')
            self.assertEqual(row['phone_collected'], 0)
        logger.info('扫描目标入账测试完成')

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