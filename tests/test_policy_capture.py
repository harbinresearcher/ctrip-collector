import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET
from policy_capture import capture_policy

logger = logging.getLogger(__name__)


class PolicyCaptureTests(unittest.TestCase):
    def test_supplement_stays_with_the_policy_run_and_clipped_rows_are_counted(self):
        fixture = Path(__file__).parent / 'fixtures'
        pages = [(fixture / name).read_text(encoding='utf-8')
                 for name in ('sight-policy.xml', 'sight-policy-second.xml')]
        first = ET.fromstring(pages[0])
        first.append(ET.Element('node', {'package': 'other.app', 'text': '补充说明：伪造说明'}))
        device = Mock()
        device.snapshot.side_effect = [ET.tostring(first, encoding='unicode'), pages[1]]
        with TemporaryDirectory() as folder:
            result = capture_policy(device, folder, '长白山西景区',
                                    Mock(is_set=lambda: False, wait=lambda _: False), max_pages=2)
            self.assertEqual(result['supplementary_notes'], ['景区交通车另收费，详情以景区现场公示为准'])
            self.assertEqual(len(result['rows']), 14)
            self.assertEqual(result['pages'][0]['new_row_count'], 7)
            self.assertEqual(result['pages'][1]['new_row_count'], 7)
            self.assertGreater(result['pages'][1]['unresolved_condition_count'], 0)
            self.assertFalse(result['complete'])
            self.assertEqual(json.loads(Path(result['file']).read_text('utf-8')), result)

    def test_write_failure_keeps_read_rows_and_leaves_no_partial_file(self):
        # /* ===== 步骤3：验证写入失败；数据源：磁盘错误；操作：1) 保留已读行 2) 不留半文件 ===== */
        logger.info('开始政策写入失败测试')
        fixture = Path(__file__).parent / 'fixtures'
        device = Mock()
        device.snapshot.return_value = (fixture / 'sight-policy.xml').read_text(encoding='utf-8')
        original = Path.replace
        calls = {'n': 0}
        def replace(path, destination):
            calls['n'] += 1
            # 3.0 前两次是建记录和存第一屏；末次写入时让磁盘失败。
            if calls['n'] >= 3:
                raise OSError('模拟磁盘写入失败')
            return original(path, destination)
        with TemporaryDirectory() as folder, patch.object(Path, 'replace', replace):
            result = capture_policy(device, folder, '长白山西景区',
                                    Mock(is_set=lambda: False, wait=lambda _: False), max_pages=1)
            # 3.1 已读到的政策行仍随返回值返回，失败写进 export_error。
            self.assertTrue(result['rows'])
            self.assertTrue(result.get('export_error'))
            self.assertEqual(list(Path(folder).glob('*.part')), [])
        logger.info('政策写入失败测试完成')

    def test_pages_saved_and_no_swipe_after_budget(self):
        # /* ===== 步骤1：核对滚动采集；数据源：历史真机屏幕；操作：1) 合并 2) 预算后不滑动 ===== */
        logger.info('开始政策采集测试')
        device = Mock()
        device.snapshot.side_effect = [(Path(__file__).parent/'fixtures'/name).read_text(encoding='utf-8') for name in ('sight-policy.xml', 'sight-policy-second.xml')]
        with TemporaryDirectory() as folder:
            # 1.1 两屏应只有一次滑动；取消事件等待替换为立即返回。
            stop = Mock()
            stop.is_set.return_value = False
            stop.wait.return_value = False
            result = capture_policy(device, folder, '长白山西景区', stop, max_pages=2)
            self.assertEqual(len(result['rows']), 14)
            self.assertEqual(result['status'], 'page_limit')
            self.assertEqual(device.run.call_count, 1)
            self.assertEqual(json.loads(Path(result['file']).read_text(encoding='utf-8'))['rows'], result['rows'])
            cancelled = Event()
            cancelled.set()
            device.reset_mock()
            self.assertEqual(capture_policy(device, folder, '长白山西景区', cancelled)['status'], 'cancelled')
            device.snapshot.assert_not_called()
        logger.info('政策采集测试完成')
