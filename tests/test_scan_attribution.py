"""身份没核对上时不能把屏幕上的数字记到本对象头上。"""
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


class ScanAttributionTests(unittest.TestCase):
    def test_mismatched_screen_records_no_count(self):
        # /* ===== 步骤1：屏幕停在别的对象上；数据源：真实点评页样本 + 错误目标名；操作：1) 不记数字 ===== */
        logger.info('开始扫描归属检查')
        xml = FIXTURE.read_text('utf-8')
        with tempfile.TemporaryDirectory() as folder:
            adb = Adb('adb', 'test')
            with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', return_value=xml):
                report = scan_phone_target(adb, 'sight', '完全不同的景点', Path(folder),
                                           object_id='136039', identity_source='evidence_manifest')
            # 1.1 没核对上就不写观察：否则会把别的对象的「全部」算到本对象头上。
            self.assertEqual(report['reason'], 'arrival_failed')
            self.assertIsNone(report['sampling_context'])
        logger.info('扫描归属检查完成')

    def test_mismatched_scan_does_not_create_phone_target(self):
        # /* ===== 步骤2：对账不能因此出现手机目标；数据源：错配的扫描报告；操作：1) phone_target 仍为空 ===== */
        logger.info('开始错配不产生目标检查')
        xml = FIXTURE.read_text('utf-8')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sight-136039-object.json').write_text(json.dumps(
                {'kind': 'sight', 'resource_id': 136039, 'name': '长白山西景区', 'display_total': 1099,
                 'url': 'https://you.ctrip.com/sight/a1/136039.html'}), encoding='utf-8')
            adb = Adb('adb', 'test')
            with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', return_value=xml):
                scan_phone_target(adb, 'sight', '完全不同的景点', root,
                                  object_id='136039', identity_source='evidence_manifest')
            row = [item for item in write_coverage_audit(root)['objects']
                   if item['object_key'] == 'sight-136039'][0]
            # 2.1 分母不能被污染，但「这次没认出页面」仍要留下记录。
            self.assertIsNone(row['phone_target'])
            self.assertTrue(row['phone_layout_unknown'])
        logger.info('错配不产生目标检查完成')


if __name__ == '__main__':
    unittest.main()