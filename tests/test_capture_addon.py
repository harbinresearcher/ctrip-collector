import gzip
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from capture_addon import Capture

logger = logging.getLogger(__name__)


class CaptureAddonTests(unittest.TestCase):
    def test_decoded_content_and_size_boundary(self):
        # /* ===== 步骤1：校验代理正文；数据源：压缩响应模型；操作：1) 读取解压内容 2) 超限只留元数据 ===== */
        logger.info('开始代理正文测试')
        payload = json.dumps({'data': {'totalCount': 1, 'groupList': [{'commentList': [
            {'id': 7, 'hotelId': 123, 'content': '正文'}]}]}}).encode()
        class Response:
            status_code = 200
            raw_content = gzip.compress(payload)
            @property
            def content(self):
                return gzip.decompress(self.raw_content)
        flow = SimpleNamespace(request=SimpleNamespace(
            pretty_url='https://m.ctrip.com/restapi/soa2/34308/getHotelCommentInfo?token=secret',
            content=json.dumps({'hotelId': 123, 'head': {'auth': 'secret'}}).encode()), response=Response())
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'CAPTURE_OUT': str(Path(folder, 'out.jsonl'))}):
            # 1.1 raw_content 是 gzip，addon 必须用 mitmproxy 的已解压 content。
            addon = Capture()
            addon.response(flow)
            flow.response = SimpleNamespace(status_code=200, content=b'x' * 4_000_001)
            addon.response(flow)
            rows = [json.loads(line) for line in Path(folder, 'out.jsonl').read_text('utf-8').splitlines()]
            self.assertEqual(rows[0]['kind'], 'hotel_reviews')
            self.assertEqual(rows[1]['kind'], 'endpoint_metadata')
            self.assertNotIn('secret', json.dumps(rows))
        logger.info('代理正文测试完成')

    def test_unrelated_host_body_is_not_accessed(self):
        # /* ===== 步骤2：拒绝无关流量；数据源：不允许的域名；操作：1) 不碰正文 2) 不生成文件 ===== */
        logger.info('开始无关流量测试')
        flow = SimpleNamespace(request=SimpleNamespace(pretty_url='https://ctrip.com.evil.test/login'),
                               response=SimpleNamespace(status_code=200))
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'CAPTURE_OUT': str(Path(folder, 'out.jsonl'))}):
            Capture().response(flow)
            self.assertFalse(Path(folder, 'out.jsonl').exists())
        logger.info('无关流量测试完成')
