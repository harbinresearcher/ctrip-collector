"""扫描时的「页面还在转场」要多看两屏，别记成未知布局。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner import Adb, scan_phone_target

logger = logging.getLogger(__name__)

FIXTURES = Path(__file__).parent / 'fixtures'
LOADING = '<hierarchy><node package="ctrip.android.view" bounds="[0,0][1080,2400]">' \
          '<node text="精彩即将呈现" bounds="[0,0][500,80]"/></node></hierarchy>'


class ScanWaitTests(unittest.TestCase):
    def test_loading_screen_is_waited_for(self):
        # /* ===== 步骤1：第一屏还在加载；数据源：占位屏 + 真实点评页；操作：1) 等待 2) 再判 ===== */
        logger.info('开始加载等待测试')
        review = (FIXTURES / 'sight-review-live-yungang.xml').read_text('utf-8')
        adb = Adb('adb', 'test')
        frames = [LOADING, review]
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(adb, 'ensure_foreground'), \
                patch.object(adb, 'snapshot', side_effect=lambda: frames.pop(0) if len(frames) > 1 else frames[0]), \
                patch('time.sleep'):
            result = scan_phone_target(adb, 'sight', '云冈石窟', Path(folder), object_id='1',
                                       identity_source='evidence_manifest')
        # 1.1 等到真页面后应当记成 target_scanned，而不是 arrival_failed。
        self.assertEqual(result['reason'], 'target_scanned')
        self.assertIsNotNone(result['sampling_context'])
        logger.info('加载等待测试完成')

    def test_loading_forever_still_reports_arrival_failed(self):
        # /* ===== 步骤2：一直加载不完；数据源：只有占位屏；操作：1) 三次后记 arrival_failed ===== */
        logger.info('开始一直加载失败测试')
        adb = Adb('adb', 'test')
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(adb, 'ensure_foreground'), \
                patch.object(adb, 'snapshot', return_value=LOADING), \
                patch('time.sleep'):
            result = scan_phone_target(adb, 'sight', '云冈石窟', Path(folder), object_id='2',
                                       identity_source='evidence_manifest')
        self.assertEqual(result['reason'], 'arrival_failed')
        self.assertEqual(result['arrival_reason'], 'no_review_anchor')
        logger.info('一直加载失败测试完成')


if __name__ == '__main__':
    unittest.main()