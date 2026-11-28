"""详情页加载慢：探测模式下要等到正文再判。"""
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner import Adb, scan_phone_target

logger = logging.getLogger(__name__)

FIXTURES = Path(__file__).parent / 'fixtures'
LOADING = ('<hierarchy><node package="ctrip.android.view" bounds="[0,0][1080,2400]">'
           '<node text="精彩即将呈现" bounds="[0,0][500,80]"/></node></hierarchy>')


class ProbeWaitTests(unittest.TestCase):
    def test_probe_waits_through_loading_placeholder(self):
        # /* ===== 步骤1：占位屏出现 5 次后才是详情页；数据源：真实详情页样本；操作：1) 等到正文 ===== */
        logger.info('开始详情页长等待测试')
        detail = (FIXTURES / 'sight-detail-no-reviews.xml').read_text('utf-8')
        frames = [LOADING] * 5 + [detail]
        adb = Adb('adb', 'test')

        def take():
            return frames.pop(0) if len(frames) > 1 else frames[0]

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(adb, 'ensure_foreground'), \
                patch.object(adb, 'snapshot', side_effect=take), \
                patch('time.sleep'):
            result = scan_phone_target(adb, 'sight', '长白山大峡谷漂流', Path(folder), object_id='1',
                                       identity_source='evidence_manifest', allow_detail_probe=True)
        # 1.1 5 秒的占位屏不该被记成失败：等到正文后应判为「没有点评入口」。
        self.assertEqual(result['reason'], 'no_review_entry')
        self.assertTrue(result['detail_seen'])
        logger.info('详情页长等待测试完成')

    def test_without_probe_keeps_short_wait(self):
        # /* ===== 步骤2：不开探测时仍是短等待；数据源：一直占位；操作：1) 如实记 arrival_failed ===== */
        logger.info('开始短等待对照测试')
        adb = Adb('adb', 'test')
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(adb, 'ensure_foreground'), \
                patch.object(adb, 'snapshot', return_value=LOADING), \
                patch('time.sleep'):
            result = scan_phone_target(adb, 'sight', '某景点', Path(folder), object_id='2',
                                       identity_source='evidence_manifest')
        self.assertEqual(result['reason'], 'arrival_failed')
        logger.info('短等待对照测试完成')


if __name__ == '__main__':
    unittest.main()