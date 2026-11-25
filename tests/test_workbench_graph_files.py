"""工作台列出本目录可恢复的发现队列。"""
import http.client
import json
import logging
import tempfile
import unittest
from pathlib import Path
from threading import Thread

from workbench_server import create_server

logger = logging.getLogger(__name__)


class GraphFilesTests(unittest.TestCase):
    def test_lists_only_well_named_root_graph_reports(self):
        # /* ===== 步骤1：核验队列清单；数据源：本地目录；操作：1) 限定文件名 2) 拒绝其他文件 ===== */
        logger.info('开始工作台队列清单测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            name = 'related-graph-' + 'a'*32 + '.json'
            (root / name).write_text('{}', encoding='utf-8')
            (root / 'related-graph-other.json').write_text('{}', encoding='utf-8')
            server = create_server(root, port=0)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
                conn.request('GET', '/api/local-files')
                response = conn.getresponse()
                data = json.loads(response.read())
                self.assertEqual([item['name'] for item in data['graph_reports']], [name])
                conn.close()
            finally:
                server.manager.close()
                server.shutdown()
                server.server_close()
                thread.join(3)
        logger.info('工作台队列清单测试完成')
