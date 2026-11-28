"""简化详情页（标题 + 暂无评分）也要判成「没有点评」。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner import Adb, scan_phone_target
from sight_snapshot import sight_detail_without_reviews

FIXTURE = Path(__file__).parent / 'fixtures' / 'sight-detail-no-reviews.xml'


class NoReviewDetailTests(unittest.TestCase):
    def test_detector_matches_real_page(self):
        # /* ===== 步骤1：真实详情页样本；数据源：真机保存的 XML；操作：1) 认出没有点评 ===== */
        xml = FIXTURE.read_text('utf-8')
        found = sight_detail_without_reviews(xml, '长白山大峡谷漂流')
        self.assertEqual(found['status'], 'no_reviews')
        # 1.1 名字不符时不能借用别人的页面下结论。
        self.assertIsNone(sight_detail_without_reviews(xml, '长白山西景区'))

    def test_scan_records_no_review_entry(self):
        # /* ===== 步骤2：扫描停在这种页面；数据源：同一份样本；操作：1) 记 no_review_entry ===== */
        xml = FIXTURE.read_text('utf-8')
        adb = Adb('adb', 'test')
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(adb, 'ensure_foreground'), \
                patch.object(adb, 'snapshot', return_value=xml), \
                patch('time.sleep'):
            result = scan_phone_target(adb, 'sight', '长白山大峡谷漂流', Path(folder), object_id='134085038',
                                       identity_source='evidence_manifest', allow_detail_probe=True)
        self.assertEqual(result['reason'], 'no_review_entry')
        self.assertTrue(result['detail_seen'])
        self.assertTrue(result['review_entry_missing'])
        self.assertIsNone(result['sampling_context'])


if __name__ == '__main__':
    unittest.main()