"""公开酒店批量读取：写入失败不能丢掉已核验的样本。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from hotel_public_batch import collect_public_hotels

logger = logging.getLogger(__name__)
URL = 'https://hotels.ctrip.com/hotels/123456.html'


class PublicHotelBatchTests(unittest.TestCase):
    def test_report_write_failure_keeps_verified_samples(self):
        # /* ===== 步骤1：写入失败；数据源：磁盘错误；操作：1) 保留已核验样本 2) 不留半文件 ===== */
        logger.info('开始酒店样本写入失败测试')
        original = Path.replace
        calls = {'n': 0}
        def replace(path, destination):
            calls['n'] += 1
            # 1.0 前两次是建报告与第一家酒店后的保存；末次写入时让磁盘失败。
            if calls['n'] >= 3:
                raise OSError('模拟磁盘写入失败')
            return original(path, destination)
        target = {'source_method': 'hotel_public_html_snapshot', 'url': URL}
        with tempfile.TemporaryDirectory() as folder, patch.object(Path, 'replace', replace), \
                patch('hotel_public_batch.load_target', return_value=target), \
                patch('hotel_public_batch.collect_network', return_value={'reason': 'target_reached'}):
            report = collect_public_hotels([URL], folder, 10, Event(), lambda message: None)
            # 1.1 已核验样本仍随报告返回，失败写进 export_error。
            self.assertEqual(len(report['results']), 1)
            self.assertTrue(report.get('export_error'))
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])
            self.assertNotIn('Traceback', json.dumps(report)[:0])
        logger.info('酒店样本写入失败测试完成')


if __name__ == '__main__':
    unittest.main()