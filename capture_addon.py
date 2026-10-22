"""mitmdump -q -s capture_addon.py；不使用 -w 保存原始流量。"""
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_io import capture_record

logger = logging.getLogger(__name__)


class Capture:
    def __init__(self):
        self.connections = 0
        self.tls_failures = 0

    def client_connected(self, client):
        # 0.1 诊断仅记录连接次数，不保存流量或设备标识。
        self.connections += 1
        self.save_diagnostics()

    def tls_failed_client(self, data):
        # 0.2 握手失败只计数，不能据此区分证书信任与证书固定。
        self.tls_failures += 1
        self.save_diagnostics()

    def save_diagnostics(self):
        # /* ===== 步骤0：保存链路计数；数据源：代理事件；操作：1) 仅写数量 2) 不写主机或正文 ===== */
        logger.info('开始保存代理诊断')
        if os.environ.get('CAPTURE_DIAG'):
            Path(os.environ['CAPTURE_DIAG']).write_text(json.dumps({'connections': self.connections,
                'tls_failures': self.tls_failures}), encoding='utf-8')
        logger.info('代理诊断保存完成')

    def response(self, flow):
        # /* ===== 步骤1：接收受限响应；数据源：mitmproxy 解压正文；操作：1) 限制大小 2) 筛选结构 3) 逐条保存 ===== */
        logger.info('开始处理响应')
        # 1.1 先仅用地址筛选，不读取其他域名或非业务接口正文。
        record = capture_record(flow.request.pretty_url, {}, {}, flow.response.status_code)
        if record is None:
            return
        if record['service'] == '34308' and record['action'] == 'getHotelCommentInfo':
            try:
                request = flow.request.content or b''
                response = flow.response.content or b''
                if len(request) > 100_000 or len(response) > 4_000_000:
                    raise ValueError('正文过大')
                record = capture_record(flow.request.pretty_url, json.loads(request), json.loads(response), flow.response.status_code)
            except (ValueError, TypeError):
                record['reason'] = 'body_not_json_or_too_large'
        # 1.2 文件只含筛选后结构；未知接口无正文、认证头及查询串。
        output = Path(os.environ.get('CAPTURE_OUT', 'capture/filtered.jsonl'))
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('a', encoding='utf-8') as file:
            file.write(json.dumps(record, ensure_ascii=False) + '\n')
        logger.info('受限响应保存完成')


addons = [Capture()]
