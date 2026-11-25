"""从工作台恢复已归档的 App 响应。"""
import http.client
import json
import logging
import tempfile
import unittest
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock

from capture_io import capture_record
from workbench_capture_jobs import prepare_capture_job
from workbench_server import create_server

logger = logging.getLogger(__name__)


class CaptureRecoveryTests(unittest.TestCase):
    def test_archived_batches_can_be_selected_and_reimported(self):
        # /* ===== 步骤1：验证归档续导；数据源：合成响应；操作：1) 达到上限 2) 从工作台继续导入 ===== */
        logger.info('开始归档续导测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            captures = root / 'captures'
            captures.mkdir()
            payload = {'totalCount': 2, 'groupList': [{'commentList': [
                {'id': number, 'hotelId': 123, 'content': str(number)} for number in (1, 2)]}]}
            record = capture_record('https://m.ctrip.com/restapi/soa2/34308/getHotelCommentInfo',
                                    {'hotelId': 123}, {'data': payload}, 200)
            (captures / 'filtered.jsonl').write_text(json.dumps(record) + '\n', encoding='utf-8')
            first = prepare_capture_job({'mode': 'capture_import', 'capture_file': 'filtered.jsonl', 'limit': 1},
                                        root)(Event(), Mock())
            archive = Path(first['results'][0]['capture_archive'])
            self.assertTrue(first['results'][0]['last_batch_may_be_truncated'])
            server = create_server(root, port=0)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                # 1.1 列表只暴露规范归档名，并标记为工作台自身保存的批次。
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
                conn.request('GET', '/api/local-files')
                response = conn.getresponse()
                files = json.loads(response.read())
                self.assertIn('archive:' + archive.name, [item['name'] for item in files['capture_files']])
                conn.close()
            finally:
                server.manager.close()
                server.shutdown()
                server.server_close()
                thread.join(3)
            # 1.2 提高目标条数后重导，原有评论去重并恢复未入库评论。
            report = prepare_capture_job({'mode': 'capture_import', 'capture_file': 'archive:' + archive.name,
                                          'limit': 2}, root)(Event(), Mock())
            self.assertEqual(report['reason'], 'capture_imported')
            self.assertEqual(report['results'][0]['count'], 2)
        logger.info('归档续导测试完成')

    def test_recovery_rejects_unrecognized_paths_and_links(self):
        # /* ===== 步骤1：核验归档边界；数据源：工作台参数；操作：1) 拒绝任意路径 2) 拒绝符号链接 ===== */
        logger.info('开始归档边界测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('archive:../secret.jsonl', 'archive:other.jsonl', 'archive:hotel-0-app-capture-batches-' + 'a'*32 + '.jsonl'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    prepare_capture_job({'mode': 'capture_import', 'capture_file': name}, root)
            archive = root / ('hotel-123-app-capture-batches-' + 'a'*32 + '.jsonl')
            archive.write_text('{}', encoding='utf-8')
            with unittest.mock.patch.object(Path, 'is_symlink', return_value=True), self.assertRaises(ValueError):
                prepare_capture_job({'mode': 'capture_import', 'capture_file': 'archive:' + archive.name}, root)
        logger.info('归档边界测试完成')
