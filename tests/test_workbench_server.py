import http.client
import json
import logging
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from workbench_server import create_server

logger = logging.getLogger(__name__)


class ServerTests(unittest.TestCase):
    def test_local_files_lists_resumable_sight_gallery_reports(self):
        from zipfile import ZipFile

        with TemporaryDirectory() as folder:
            server = create_server(folder, port=0)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                root = Path(folder)
                for letter, last_page, method in [('a', 3, 'android_sight_photo_gallery'),
                                                  ('b', 12, 'android_sight_photo_gallery'),
                                                  ('c', 3, 'other'),
                                                  ('e', 3, 'android_sight_photo_gallery')]:
                    run_id = letter * 32
                    name = f'sight-photo-gallery-{run_id}'
                    report = {'kind': 'sight', 'source': 'ctrip_android_ui', 'source_method': method,
                              'run_id': run_id, 'name': '东方明珠', 'gallery_total': 12,
                              'pages': [{'page': last_page}], 'archive': f'{name}.zip'}
                    (root / f'{name}.json').write_text(json.dumps(report), encoding='utf-8')
                    if letter != 'e':
                        with ZipFile(root / f'{name}.zip', 'w') as archive:
                            archive.writestr('manifest.json', json.dumps(report))
                (root / f'sight-photo-gallery-{"d" * 32}.json').write_text('{', encoding='utf-8')
                conn.request('GET', '/api/local-files')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                runs = json.loads(response.read())['sight_gallery_runs']
                self.assertEqual(len(runs), 1)
                run = runs[0]
                self.assertEqual(set(run), {'run_id', 'name', 'last_page', 'modified'})
                self.assertEqual((run['run_id'], run['name'], run['last_page']), ('a' * 32, '东方明珠', 3))
            finally:
                conn.close()
                server.shutdown()
                server.server_close()
                thread.join(3)

    def test_current_sight_photo_gallery_zip_download(self):
        from io import BytesIO
        from zipfile import ZipFile

        with TemporaryDirectory() as folder:
            server = create_server(folder, port=0)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                root = Path(folder).resolve()
                run = 'a' * 32
                name = f'sight-photo-gallery-{run}.zip'
                report_path = root / f'sight-photo-gallery-{run}.json'
                payload = BytesIO()
                with ZipFile(payload, 'w') as archive:
                    archive.writestr('manifest.json', '{}')
                (root / name).write_bytes(payload.getvalue())
                result = {'kind': 'sight', 'source': 'ctrip_android_ui', 'source_method': 'android_sight_photo_gallery',
                          'run_id': run, 'archive': name, 'file': str(report_path), 'reason': 'cancelled'}
                report_path.write_text(json.dumps(result), encoding='utf-8')
                server.manager.start(lambda stop, emit: result)
                server.manager.worker.join(3)
                conn.request('GET', f'/api/sight-photo-gallery?run={run}')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader('Content-Type'), 'application/zip')
                self.assertEqual(response.getheader('Content-Disposition'), 'attachment; filename="sight-photo-gallery.zip"')
                self.assertEqual(response.read(), payload.getvalue())

                for query in ('', 'run=' + 'b' * 32, 'run=..%2F' + run):
                    with self.subTest(query=query):
                        conn.request('GET', f'/api/sight-photo-gallery?{query}')
                        response = conn.getresponse()
                        self.assertEqual(response.status, 404)
                        response.read()
                server.manager.start(lambda stop, emit: {**result, 'archive': '../' + name})
                server.manager.worker.join(3)
                conn.request('GET', f'/api/sight-photo-gallery?run={run}')
                response = conn.getresponse()
                self.assertEqual(response.status, 404)
                response.read()
            finally:
                conn.close()
                server.shutdown()
                server.server_close()
                thread.join(3)

    def test_process_interrupt_waits_for_cancelled_task_save(self):
        # /* ===== 步骤1：核对进程关闭；数据源：独立子进程；操作：1) 模拟中断 2) 验证任务完成保存 ===== */
        logger.info('开始服务关闭保存测试')
        with TemporaryDirectory() as folder:
            marker = Path(folder) / 'saved.txt'
            script = r'''
import runpy, sys, time
from unittest.mock import patch
from http.server import ThreadingHTTPServer
marker = sys.argv[1]
def interrupted(server):
    def job(stop, emit):
        stop.wait(3)
        time.sleep(.2)
        with open(marker, 'w', encoding='utf-8') as out:
            out.write('saved')
        return {'reason':'cancelled'}
    server.manager.start(job)
    raise KeyboardInterrupt()
sys.argv = ['workbench_server.py', '--output', sys.argv[2], '--port', '0']
with patch.object(ThreadingHTTPServer, 'serve_forever', interrupted):
    runpy.run_path('workbench_server.py', run_name='__main__')
'''
            # 1.1 与主入口一致触发中断，禁止将取消事件误当作保存已完成。
            result = subprocess.run([sys.executable, '-c', script, str(marker), folder],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', errors='replace'))
            self.assertTrue(marker.is_file(), '服务退出前必须等待任务完成写入')
            self.assertEqual(marker.read_text(encoding='utf-8'), 'saved')
        logger.info('服务关闭保存测试完成')

    def test_favicon_request_has_no_404_noise(self):
        # /* ===== 步骤1：核对图标请求；数据源：浏览器默认请求；操作：1) 返回空响应 2) 不写错误日志 ===== */
        logger.info('开始图标请求测试')
        with TemporaryDirectory() as folder:
            server = create_server(folder, port=0)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                conn.request('GET', '/favicon.ico')
                response = conn.getresponse()
                self.assertEqual(response.status, 204)
                self.assertEqual(response.read(), b'')
            finally:
                server.shutdown()
                server.server_close()
        logger.info('图标请求测试完成')

    def test_origin_validation_and_real_status(self):
        # /* ===== 步骤1：核验本地接口；数据源：真实HTTP请求；操作：1) 状态读取 2) 拒绝外站控制 ===== */
        logger.info('开始工作台HTTP测试')
        with TemporaryDirectory() as folder:
            server = create_server(folder, port=0)
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                # 1.1 测试仅读取状态和非法任务，不启动网络采集。
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                # 1.0 续跑与图片入口只能列出当前输出目录内的规范文件。
                report_name = 'discovery-regions-' + 'a' * 32 + '.json'
                (Path(folder) / report_name).write_text('{}', encoding='utf-8')
                (Path(folder) / 'sight-123.jsonl').write_text('', encoding='utf-8')
                (Path(folder) / 'private.jsonl').write_text('', encoding='utf-8')
                (Path(folder) / 'captures').mkdir()
                (Path(folder) / 'captures' / 'filtered.jsonl').write_text('', encoding='utf-8')
                (Path(folder) / 'captures' / 'secret.har').write_text('', encoding='utf-8')
                conn.request('GET', '/api/local-files')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                files = json.loads(response.read())
                self.assertEqual([x['name'] for x in files['reports']], [report_name])
                self.assertEqual([x['name'] for x in files['media_sources']], ['sight-123.jsonl'])
                self.assertEqual([x['name'] for x in files['capture_files']], ['filtered.jsonl'])
                # 1.0 只能下载输出目录内规范命名的评论文件。
                (Path(folder)/'sight-1.csv').write_text('content\nexample', encoding='utf-8')
                conn.request('GET', '/api/export?object=sight-1&format=csv')
                export = conn.getresponse()
                self.assertEqual(export.status, 200)
                self.assertEqual(export.read(), (Path(folder)/'sight-1.csv').read_bytes())
                conn.request('GET', '/api/export?object=../secret&format=csv')
                export = conn.getresponse()
                self.assertEqual(export.status, 400)
                export.read()
                conn.request('GET', '/')
                page = conn.getresponse()
                self.assertEqual(page.status, 200)
                page_text = page.read().decode('utf-8')
                self.assertIn('采集工作台', page_text)
                self.assertIn('value="auto_discovery"', page_text)
                conn.request('GET', '/api/state')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())['status'], 'idle')
                # 1.2.1 观察历史只按本地指纹查指定评论，不泄漏作者和原始界面。
                from mobile_scoped_store import MobileScopedStore
                store = MobileScopedStore(folder, 'hotel', '123')
                row = {'author':'隐私昵称','date':'2026','date_kind':'published','rating':'4','content':'正文','has_expand':False,'raw_texts':['隐私昵称']}
                store.save('酒店', [row], 'private.xml')
                review_key = store.db.execute('SELECT key FROM reviews_scoped').fetchone()[0]
                store.save('酒店', [{**row, 'rating':'5'}], 'later.xml')
                store.close()
                conn.request('GET', '/api/mobile-observations?review='+review_key)
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                history = json.loads(response.read())
                self.assertEqual([x['score'] for x in history['items']], ['5','4'])
                self.assertNotIn('隐私昵称', json.dumps(history, ensure_ascii=False))
                conn.request('GET', '/api/mobile-observations?review=../../secret')
                response = conn.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
                # 1.2 只能下载当前任务报告引用的手机评论CSV。
                phone_csv = Path(folder).resolve() / 'hotel-123456abcdef.csv'
                phone_csv.write_bytes(b'captured\n')
                server.manager.start(lambda stop, emit: {'kind':'hotel','csv':str(phone_csv),'reason':'stopped_by_user'})
                server.manager.worker.join(2)
                conn.request('GET', '/api/task-csv')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b'captured\n')
                conn.request('POST', '/api/stop', '{}', {'Content-Type':'application/json','Origin':'https://other.example'})
                response = conn.getresponse()
                self.assertEqual(response.status, 403)
                response.read()
                conn.request('POST', '/api/start', '{}', {'Content-Type':'application/json'})
                response = conn.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
                conn.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)
        logger.info('工作台HTTP测试完成')
