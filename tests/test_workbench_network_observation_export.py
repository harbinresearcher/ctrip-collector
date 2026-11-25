"""工作台网页评论观察文件的受限预览和下载。"""
import http.client
import json
import logging
import sqlite3
import stat
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace
from unittest.mock import patch

from workbench_preview import open_network_observations, read_preview
from workbench_server import create_server

logger = logging.getLogger(__name__)


class NetworkObservationExportTests(unittest.TestCase):
    def test_only_existing_object_observations_are_advertised_and_downloaded(self):
        # /* ===== 步骤1：核验历史下载；数据源：本地评论和观察文件；操作：1) 按对象判断 2) 限定下载 ===== */
        logger.info('开始网页评论观察下载测试')
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with closing(sqlite3.connect(root / 'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews(object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.executemany('INSERT INTO network_reviews VALUES (?,?,?,?)', [
                    ('sight-1', '1', json.dumps({'content':'第一条'}), '2026'),
                    ('sight-2', '2', json.dumps({'content':'第二条'}), '2026')])
                db.commit()
            observed = root / 'sight-1-observations.jsonl'
            observed.write_bytes(b'{"comment_id":"1"}\n')
            items = {item['object_key']: item for item in read_preview(root)['items']}
            self.assertTrue(items['sight-1']['observations_available'])
            self.assertFalse(items['sight-2']['observations_available'])
            server = create_server(root, port=0)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                conn.request('GET', '/api/export?object=sight-1&format=observations')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), observed.read_bytes())
                for url, expected in [('/api/export?object=sight-2&format=observations', 404),
                                      ('/api/export?object=../secret&format=observations', 400),
                                      ('/api/export?object=sight-0&format=observations', 400)]:
                    conn.request('GET', url)
                    response = conn.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                # 1.1 即使链接指向目录内部，也不得作为普通文件下载。
                with patch.object(Path, 'is_symlink', return_value=True):
                    conn.request('GET', '/api/export?object=sight-1&format=observations')
                    response = conn.getresponse()
                    self.assertEqual(response.status, 404)
                    response.read()
                    self.assertFalse({item['object_key']: item for item in read_preview(root)['items']}
                                     ['sight-1']['observations_available'])
                # 1.2 Windows 重解析点句柄也不能被误认为可下载普通文件。
                fake_stat = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
                with patch('workbench_preview.os.fstat', return_value=fake_stat), self.assertRaises(ValueError):
                    open_network_observations(root, 'sight-1')
                # 1.3 预览后文件消失时，下载前必须重新检查。
                observed.unlink()
                conn.request('GET', '/api/export?object=sight-1&format=observations')
                response = conn.getresponse()
                self.assertEqual(response.status, 404)
                response.read()
                conn.close()
            finally:
                server.manager.close()
                server.shutdown()
                server.server_close()
                thread.join(3)
        logger.info('网页评论观察下载测试完成')
