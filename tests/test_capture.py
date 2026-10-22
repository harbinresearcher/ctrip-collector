import json
import logging
import tempfile
import unittest
from pathlib import Path

from capture_io import capture_record, import_capture

logger = logging.getLogger(__name__)
URL = 'https://m.ctrip.com/restapi/soa2/34308/getHotelCommentInfo?token=secret'


class CaptureTests(unittest.TestCase):
    def test_empty_and_repeated_batches_do_not_hide_later_records(self):
        # /* ===== 步骤3：遍历离线批次；数据源：重复与空响应；操作：1) 保留后续新 ID 2) 真正文件结束才停止 ===== */
        logger.info('开始离线批次边界测试')
        rows = []
        for identity in (1, 1, 1, 1, None, 2):
            items = [] if identity is None else [{'id': identity, 'hotelId': 123}]
            rows.append(capture_record(URL, {'hotelId': 123}, {'data': {
                'totalCount': 99, 'groupList': [{'commentList': items}]}}, 200))
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, 'capture.jsonl')
            source.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
            result = import_capture(source, Path(folder, 'data'), 500)[0]
            self.assertEqual(result['count'], 2)
            self.assertEqual(result['reason'], 'capture_exhausted')
            self.assertEqual(result['pages'], 6)
        logger.info('离线批次边界测试完成')

    def test_domain_and_private_requests(self):
        # /* ===== 步骤1：验证捕获边界；数据源：伪造流量；操作：1) 拒绝相似域名 2) 不写请求与登录响应 ===== */
        logger.info('开始捕获边界测试')
        self.assertIsNone(capture_record(URL.replace('m.ctrip.com', 'ctrip.com.evil.test'), {}, {}, 200))
        record = capture_record('https://m.ctrip.com/restapi/soa2/1/login?token=secret',
                                {'password': 'secret'}, {'token': 'secret'}, 200)
        self.assertNotIn('secret', json.dumps(record))
        self.assertEqual(record['kind'], 'endpoint_metadata')
        logger.info('捕获边界测试完成')

    def test_full_response_identity_and_repeat_import(self):
        # /* ===== 步骤2：验证导入与去重；数据源：已知酒店响应；操作：1) 删除凭据 2) 保留字段 3) 重复导入零新增 ===== */
        logger.info('开始响应导入测试')
        request = {'hotelId': 123, 'head': {'auth': 'secret'}, 'commentFilterOptions': {'pageIndex': 2, 'pageSize': 10}}
        data = {'data': {'hotelName': '酒店', 'totalCount': 99, 'groupList': [{'commentList': [
            {'id': 5, 'hotelId': 123, 'content': '正文', 'imageList': ['image'],
             'userInfo': {'userId': 'secret', 'phone': 'secret'}}]}], 'aiSummaryEntities': {'summaryItemList': [{'title': 'AI'}]}}}
        record = capture_record(URL, request, data, 200)
        self.assertNotIn('secret', json.dumps(record))
        self.assertEqual(record['kind'], 'hotel_reviews')
        record['paging']['authorization'] = 'secret'
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, 'capture.jsonl')
            source.write_text(json.dumps(record) + '\n', encoding='utf-8')
            first = import_capture(source, Path(folder, 'data'), 500)
            second = import_capture(source, Path(folder, 'data'), 500)
            self.assertEqual(first[0]['count'], 1)
            self.assertEqual(first[0]['reason'], 'capture_exhausted')
            self.assertEqual(second[0]['new_count'], 0)
            self.assertFalse(first[0]['complete'])
            self.assertNotIn('secret', Path(folder, 'data/hotel-123-app-capture-object.json').read_text('utf-8'))
            saved = json.loads(Path(first[0]['jsonl']).read_text('utf-8').splitlines()[0])
            self.assertEqual(saved['data']['source_schema'], 'app_proxy_capture')
        data['data']['groupList'][0]['commentList'][0]['hotelId'] = 456
        self.assertEqual(capture_record(URL, request, data, 200)['kind'], 'endpoint_metadata')
        logger.info('响应导入测试完成')
