"""端到端下载接口：用真实任务快照和真实文件验证 200 与 404 分支。"""
import http.client
import json
import logging
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from zipfile import ZipFile

from workbench_server import create_server

logger = logging.getLogger(__name__)
RUN = '20261126T101010-abcdef'
REPORT = 'c' * 32


def wait_finished(server):
    deadline = monotonic() + 10
    while monotonic() < deadline:
        if server.manager.snapshot()['status'] != 'running':
            return server.manager.snapshot()
        sleep(0.02)
    raise AssertionError('任务没有在 10 秒内结束')


class DownloadEndpointTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.server = create_server(self.folder.name, port=0)
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()
        self.folder.cleanup()

    def get(self, path):
        self.conn.request('GET', path)
        response = self.conn.getresponse()
        return response.status, response.read(), dict(response.getheaders())

    def test_restaurant_review_thumbnails_downloads_and_reports_correct_name(self):
        # /* ===== 步骤1：餐厅点评裁图；数据源：真实任务快照；操作：1) 下载 ZIP 2) 核对文件名 ===== */
        logger.info('开始餐厅点评裁图下载测试')
        archive = f'restaurant-review-thumbnails-{RUN}.zip'
        with ZipFile(self.root / archive, 'x') as stream:
            stream.writestr('manifest.json', json.dumps({'run_id': RUN}))
            stream.writestr('thumbnails/00001-0001.png', b'\x89PNG\r\n\x1a\n' + b'0' * 32)
        result = {'kind': 'restaurant', 'run_id': RUN, 'review_thumbnails': {'archive': archive, 'saved_count': 1}}
        self.server.manager.start(lambda stop, emit: result)
        wait_finished(self.server)

        status, body, headers = self.get(f'/api/review-thumbnails?run={RUN}')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Disposition'], 'attachment; filename="restaurant-review-thumbnails.zip"')
        self.assertTrue(body.startswith(b'PK'), '返回的应是 ZIP')
        # 1.1 换成别的任务编号或酒店类型时不能拿到这份文件。
        self.assertEqual(self.get('/api/review-thumbnails?run=20261126T101010-000000')[0], 404)
        logger.info('餐厅点评裁图下载测试完成')

    def test_sight_gallery_zip_and_single_tile(self):
        # /* ===== 步骤2：景点晒图；数据源：真实任务快照；操作：1) 整包下载 2) 单张裁图 ===== */
        logger.info('开始景点晒图下载测试')
        archive = f'sight-ai-thumbnails-{REPORT}.zip'
        with ZipFile(self.root / archive, 'x') as stream:
            stream.writestr('manifest.json', json.dumps({'report_id': REPORT}))
            stream.writestr('thumbnails/01.png', b'\x89PNG\r\n\x1a\n' + b'1' * 32)
        result = {'source_method': 'android_sight_ai_snapshot',
                  'gallery_thumbnails': {'report_id': REPORT, 'archive': archive}}
        self.server.manager.start(lambda stop, emit: result)
        wait_finished(self.server)

        status, body, headers = self.get(f'/api/sight-gallery?run={REPORT}')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Disposition'], 'attachment; filename="sight-gallery-thumbnails.zip"')
        self.assertTrue(body.startswith(b'PK'))
        # 2.1 单张裁图走 PNG 分支；越界序号必须是 404，不能 500。
        status, picture, headers = self.get(f'/api/sight-gallery?run={REPORT}&tile=1')
        self.assertEqual((status, headers['Content-Type']), (200, 'image/png'))
        self.assertTrue(picture.startswith(b'\x89PNG'))
        for tile in ('0', '9', '99', 'abc'):
            self.assertEqual(self.get(f'/api/sight-gallery?run={REPORT}&tile={tile}')[0], 404, tile)
        logger.info('景点晒图下载测试完成')

    def test_preview_returns_503_while_database_is_locked(self):
        # /* ===== 步骤4：库被占用；数据源：并发写事务；操作：1) 返回 503 2) 不静默断连 ===== */
        logger.info('开始预览占用测试')
        import sqlite3
        from contextlib import closing
        database = self.root / 'network.sqlite3'
        # 4.0 建表和写锁各用独立连接，结束后关闭，否则 Windows 无法清理临时目录。
        with closing(sqlite3.connect(database)) as db:
            db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
            db.commit()
        writer = sqlite3.connect(database, timeout=1)
        writer.execute('BEGIN EXCLUSIVE')
        writer.execute("INSERT INTO network_reviews VALUES ('sight-1','1','{}','now')")
        try:
            status, body, _ = self.get('/api/preview')
            self.assertEqual(status, 503, body[:120])
            self.assertIn('正被写入', json.loads(body)['error'])
        finally:
            writer.rollback()
            writer.close()
        # 4.1 释放写锁后同一接口恢复正常，说明只影响占用期间。
        status, body, _ = self.get('/api/preview')
        self.assertEqual(status, 200, body[:120])
        logger.info('预览占用测试完成')

    def test_no_task_result_never_serves_files(self):
        # /* ===== 步骤3：没有任务结果；数据源：空闲工作台；操作：1) 各下载接口一律 404 ===== */
        logger.info('开始空闲工作台下载测试')
        for path in (f'/api/review-thumbnails?run={RUN}', f'/api/sight-gallery?run={REPORT}',
                     f'/api/sight-photo-gallery?run={REPORT}', f'/api/hotel-review-bundle?run={RUN}',
                     '/api/task-csv', '/api/sight-review-replay?run=' + REPORT):
            self.assertEqual(self.get(path)[0], 404, path)
        logger.info('空闲工作台下载测试完成')


if __name__ == '__main__':
    unittest.main()