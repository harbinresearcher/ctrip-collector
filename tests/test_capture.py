import json
import hashlib
import logging
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

from capture_io import capture_object_lock, capture_record, import_capture

logger = logging.getLogger(__name__)
URL = 'https://m.ctrip.com/restapi/soa2/34308/getHotelCommentInfo?token=secret'


class CaptureTests(unittest.TestCase):
    def test_refresh_reads_later_app_capture_after_stored_target(self):
        # /* ===== 步骤1：验证 App 刷新；数据源：同一酒店两次响应；操作：1) 达到旧目标 2) 重读并保留补充字段 ===== */
        logger.info('开始 App 抓包刷新测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'capture.jsonl'
            for content in ('原文', '原文补充'):
                data = {'totalCount': 1, 'groupList': [{'commentList': [
                    {'id': 7, 'hotelId': 123, 'content': content}]}]}
                record = capture_record(URL, {'hotelId':123}, {'data':data}, 200)
                source.write_text(json.dumps(record, ensure_ascii=False)+'\n', encoding='utf-8')
                if content == '原文':
                    self.assertEqual(import_capture(source, root / 'output', limit=1)[0]['count'], 1)
            # 1.1 旧库已达目标时，显式刷新按本次响应计数并留存新观察。
            report = import_capture(source, root / 'output', limit=1, refresh=True)[0]
            self.assertEqual((report['pages'], report['target_scope'], report['count']), (1, 'current_run', 1))
            rows = [json.loads(line) for line in Path(report['observations_jsonl']).read_text('utf-8').splitlines()]
            self.assertEqual({row['data']['content'] for row in rows}, {'原文', '原文补充'})
            self.assertEqual(json.loads(Path(report['jsonl']).read_text('utf-8').splitlines()[0])['data']['content'], '原文补充')
            for bad in ('true', 1, None):
                with self.subTest(refresh=bad), self.assertRaises(ValueError):
                    import_capture(source, root / 'output', refresh=bad)
            # 1.2 启动前取消不打开输入，也不创建新目录。
            stop = Event()
            stop.set()
            self.assertEqual(import_capture(root / 'missing.jsonl', root / 'cancelled', stop=stop, refresh=True), [])
            self.assertFalse((root / 'cancelled').exists())
        logger.info('App 抓包刷新测试完成')

    def test_unread_batches_are_cleaned_and_recoverable(self):
        # /* ===== 步骤1：验证批次归档；数据源：两次酒店响应；操作：1) 达到上限 2) 清洗后重导 ===== */
        logger.info('开始未读取批次归档测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'data'
            source = root / 'in.jsonl'
            for number in (101, 102):
                # 1.1 第二次导入之前已达到目标，评论仍应保留供后续恢复。
                data = {'totalCount': 2, 'groupList': [{'commentList': [{'id': number, 'hotelId': 123}]}]}
                record = capture_record(URL, {'hotelId': 123, 'commentFilterOptions': {'pageIndex': 2, 'pageSize': 10}}, {'data': data}, 200)
                record['captured_at'] = '2026-11-13T10:00:00+08:00'
                record['authorization'] = 'secret-token'
                record['payload']['unknownExtra'] = 'secret-extra'
                record['payload']['groupList'][0]['commentList'][0]['password'] = 'secret-password'
                record['payload_sha256'] = hashlib.sha256(json.dumps(record['payload'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                source.write_text(json.dumps(record) + '\n', encoding='utf-8')
                report = import_capture(source, output, limit=1)[0]
            self.assertEqual(report['pages'], 0)
            self.assertEqual(report['unread_batch_count'], 1)
            archive = Path(report['capture_archive'])
            content = archive.read_text('utf-8')
            self.assertNotIn('secret-', content)
            archived = json.loads(content)
            self.assertEqual(archived['captured_at'], record['captured_at'])
            self.assertEqual(archived['paging'], {'pageIndex': 2, 'pageSize': 10})
            self.assertEqual(archived['hotel_id'], 123)
            self.assertEqual(archived['payload']['groupList'][0]['commentList'][0]['id'], 102)
            # 1.2 原文件删除后，归档仍可用原导入器恢复评论。
            source.unlink()
            restored = import_capture(archive, output, limit=2)[0]
            self.assertEqual(restored['count'], 2)
        logger.info('未读取批次归档测试完成')

    def test_partially_read_batch_is_reported_and_recoverable(self):
        # /* ===== 步骤1：验证批次截断；数据源：双评论响应；操作：1) 限制条数 2) 用归档恢复 ===== */
        logger.info('开始批次截断测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = {'totalCount': 2, 'groupList': [{'commentList': [{'id': i, 'hotelId': 123} for i in (1, 2)]}]}
            source = root / 'in.jsonl'
            source.write_text(json.dumps(capture_record(URL, {'hotelId': 123}, {'data': data}, 200)) + '\n', encoding='utf-8')
            # 1.1 已读取一批不代表整批评论均已入库。
            report = import_capture(source, root / 'data', limit=1)[0]
            self.assertEqual(report['unread_batch_count'], 0)
            self.assertTrue(report['last_batch_may_be_truncated'])
            restored = import_capture(report['capture_archive'], root / 'data', limit=2)[0]
            self.assertEqual(restored['count'], 2)
        logger.info('批次截断测试完成')

    def test_batch_archive_failure_stops_import(self):
        # /* ===== 步骤1：验证归档失败；数据源：酒店响应；操作：1) 注入替换失败 2) 不触碰既有对象 ===== */
        logger.info('开始批次归档失败测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'data'
            output.mkdir()
            old = output / 'hotel-123-app-capture-object.json'
            old.write_text('{"previous":true}', encoding='utf-8')
            data = {'totalCount': 1, 'groupList': [{'commentList': [{'id': 1, 'hotelId': 123}]}]}
            source = root / 'in.jsonl'
            source.write_text(json.dumps(capture_record(URL, {'hotelId': 123}, {'data': data}, 200)) + '\n', encoding='utf-8')
            # 1.1 归档未发布时禁止开始数据库导入。
            with patch('capture_io.os.replace', side_effect=OSError('模拟归档失败')):
                with patch('capture_io.collect_network') as collect:
                    with self.assertRaisesRegex(OSError, '模拟归档失败'):
                        import_capture(source, output)
            collect.assert_not_called()
            self.assertEqual(json.loads(old.read_text('utf-8')), {'previous': True})
            self.assertFalse(list(output.glob('*.tmp')))
            self.assertFalse(list(output.glob('*-app-capture-batches-*.jsonl')))
        logger.info('批次归档失败测试完成')

    def test_callback_receives_completed_hotels_before_later_failure(self):
        # /* ===== 步骤1：验证部分结果；数据源：两酒店响应；操作：1) 注入后续失败 2) 保留已完成报告 ===== */
        logger.info('开始部分结果测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            records = []
            for identity in (123, 456):
                data = {'totalCount': 1, 'groupList': [{'commentList': [{'id': identity, 'hotelId': identity}]}]}
                records.append(capture_record(URL, {'hotelId': identity}, {'data': data}, 200))
            source = root / 'in.jsonl'
            source.write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')
            reports = []
            # 1.1 第二次写历史失败不应丢掉首个成功报告。
            import capture_io
            original = capture_io.save_capture_history
            def fail_second(folder, identity, **kwargs):
                if identity == 456:
                    raise OSError('模拟失败')
                return original(folder, identity, **kwargs)
            with patch('capture_io.save_capture_history', side_effect=fail_second):
                with self.assertRaisesRegex(OSError, '模拟失败'):
                    import_capture(source, root / 'data', on_result=reports.append)
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]['object_key'], 'hotel-123')
        logger.info('部分结果测试完成')

    def test_reported_error_stops_remaining_hotels(self):
        # /* ===== 步骤1：验证错误停止；数据源：多酒店响应；操作：1) 模拟写出错误 2) 不处理后续酒店 ===== */
        logger.info('开始错误停止测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            records = []
            for identity in (123, 456):
                data = {'totalCount': 1, 'groupList': [{'commentList': [{'id': identity, 'hotelId': identity}]}]}
                records.append(capture_record(URL, {'hotelId': identity}, {'data': data}, 200))
            source = root / 'in.jsonl'
            source.write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')
            # 1.1 已返回的导出错误也应中止后续对象。
            with patch('capture_io.collect_network', return_value={'reason': 'capture_exhausted', 'error': '', 'export_error': '失败', 'pages': 1}) as collect:
                reports = import_capture(source, root / 'data')
            self.assertEqual(collect.call_count, 1)
            self.assertEqual(len(reports), 1)
        logger.info('错误停止测试完成')

    def test_reimports_keep_legacy_and_each_capture_when_limit_already_met(self):
        # /* ===== 步骤1：保留历史观察；数据源：旧对象及两次导入；操作：1) 保存独立快照 2) 核验零页导入 ===== */
        logger.info('开始重复导入历史测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'data'
            output.mkdir()
            # 1.1 原有对象和浏览器对象都必须保持可找回。
            legacy = {'legacy_marker': '旧文件'}
            old_object = output / 'hotel-123-app-capture-object.json'
            old_object.write_text(json.dumps(legacy, ensure_ascii=False), encoding='utf-8')
            browser = output / 'hotel-123-object.json'
            browser.write_text('浏览器原内容', encoding='utf-8')
            for marker in ('第一次', '第二次'):
                data = {'hotelName': '酒店', 'totalCount': 1,
                        'groupList': [{'commentList': [{'id': 5, 'hotelId': 123}]}],
                        'aiSummaryEntities': {'summaryItemList': [{'title': marker}]},
                        'selectedCommentPhotosEntities': {'marker': marker}}
                source = root / f'{marker}.jsonl'
                source.write_text(json.dumps(capture_record(URL, {'hotelId': 123}, {'data': data}, 200), ensure_ascii=False) + '\n', encoding='utf-8')
                report = import_capture(source, output, limit=1)[0]
            self.assertEqual(report['pages'], 0)
            self.assertEqual(report['new_count'], 0)
            snapshots = [json.loads(p.read_text('utf-8')) for p in output.glob('hotel-123-app-capture-history-*.json')]
            self.assertIn(legacy, snapshots)
            titles = [obj['ai_summary']['content']['summaryItemList'][0]['title'] for obj in snapshots if 'ai_summary' in obj]
            self.assertEqual(set(titles), {'第一次', '第二次'})
            self.assertEqual(browser.read_text('utf-8'), '浏览器原内容')
        logger.info('重复导入历史测试完成')

    def test_failed_import_keeps_previous_and_new_observations(self):
        # /* ===== 步骤2：核验失败保护；数据源：旧对象及新响应；操作：1) 注入写库失败 2) 核对两份历史 ===== */
        logger.info('开始失败保护测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'data'
            output.mkdir()
            # 2.1 写入最新对象失败，旧对象不能被清空，新响应仍有历史快照。
            old = output / 'hotel-123-app-capture-object.json'
            old.write_text('{"previous":true}', encoding='utf-8')
            data = {'hotelName': '酒店', 'totalCount': 1, 'groupList': [{'commentList': [{'id': 5, 'hotelId': 123}]}],
                    'aiSummaryEntities': {'summaryItemList': [{'title': '新总结'}]}}
            source = root / 'in.jsonl'
            source.write_text(json.dumps(capture_record(URL, {'hotelId': 123}, {'data': data}, 200), ensure_ascii=False) + '\n', encoding='utf-8')
            with patch('capture_io.collect_network', side_effect=RuntimeError('模拟失败')):
                with self.assertRaisesRegex(RuntimeError, '模拟失败'):
                    import_capture(source, output)
            self.assertEqual(json.loads(old.read_text('utf-8')), {'previous': True})
            snapshots = [json.loads(p.read_text('utf-8')) for p in output.glob('hotel-123-app-capture-history-*.json')]
            self.assertIn({'previous': True}, snapshots)
            self.assertTrue(any(p.get('ai_summary', {}).get('content', {}).get('summaryItemList', [{}])[0].get('title') == '新总结' for p in snapshots))
        logger.info('失败保护测试完成')

    def test_concurrent_imports_preserve_both_observations(self):
        # /* ===== 步骤3：核验并发；数据源：同酒店两次导入；操作：1) 并发提交 2) 保留两个独立记录 ===== */
        logger.info('开始并发导入测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'data'
            sources = []
            for number in (1, 2):
                # 3.1 每次导入携带不同观察，不能因为相同酒店 ID 而互相覆盖。
                data = {'hotelName': '酒店', 'totalCount': 2,
                        'groupList': [{'commentList': [{'id': number, 'hotelId': 123}]}],
                        'aiSummaryEntities': {'summaryItemList': [{'title': str(number)}]}}
                source = root / f'{number}.jsonl'
                source.write_text(json.dumps(capture_record(URL, {'hotelId': 123}, {'data': data}, 200)) + '\n', encoding='utf-8')
                sources.append(source)
            with ThreadPoolExecutor(max_workers=2) as pool:
                reports = list(pool.map(lambda path: import_capture(path, output, limit=10)[0], sources))
            self.assertTrue(all(not report['error'] for report in reports))
            snapshots = [json.loads(p.read_text('utf-8')) for p in output.glob('hotel-123-app-capture-history-*.json')]
            titles = {obj['ai_summary']['content']['summaryItemList'][0]['title'] for obj in snapshots if obj.get('ai_summary', {}).get('content', {}).get('summaryItemList')}
            self.assertEqual(titles, {'1', '2'})
        logger.info('并发导入测试完成')

    def test_empty_summary_is_not_claimed_as_captured(self):
        # /* ===== 步骤4：核验空总结；数据源：空总结模拟响应；操作：1) 保留证据 2) 不标记采到总结 ===== */
        logger.info('开始空总结状态测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # 4.1 空 summaryItemList 只说明接口返回了空结构。
            data = {'totalCount': 0, 'groupList': [], 'aiSummaryEntities': {'summaryItemList': []},
                    'selectedCommentPhotosEntities': {}}
            source = root / 'in.jsonl'
            source.write_text(json.dumps(capture_record(URL, {'hotelId': 123}, {'data': data}, 200)) + '\n', encoding='utf-8')
            import_capture(source, root / 'data')
            snapshots = [json.loads(p.read_text('utf-8')) for p in (root / 'data').glob('hotel-123-app-capture-history-*.json')]
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]['ai_summary']['status'], 'not_captured')
            self.assertEqual(snapshots[0]['ai_summary']['response_observations'][0]['content']['summaryItemList'], [])
            self.assertEqual(snapshots[0]['ai_photos']['status'], 'not_captured')
            self.assertEqual(snapshots[0]['ai_photos']['response_observations'][0]['content'], {})
        logger.info('空总结状态测试完成')

    def test_empty_nested_summary_and_photos_are_not_claimed_as_captured(self):
        # /* ===== 步骤4：核验嵌套空值；数据源：空内容响应；操作：1) 留存空结构 2) 不声称已采集 ===== */
        logger.info('开始嵌套空值状态测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # 4.2 空字典组成的列表不能当成总结条目或图片元数据。
            data = {'totalCount': 0, 'groupList': [], 'aiSummaryEntities': {'summaryItemList': [{}]},
                    'selectedCommentPhotosEntities': {'items': []}}
            source = root / 'in.jsonl'
            source.write_text(json.dumps(capture_record(URL, {'hotelId': 123}, {'data': data}, 200)) + '\n', encoding='utf-8')
            import_capture(source, root / 'data')
            saved = json.loads((root / 'data' / 'hotel-123-app-capture-object.json').read_text('utf-8'))
            self.assertEqual(saved['ai_summary']['status'], 'not_captured')
            self.assertEqual(saved['ai_photos']['status'], 'not_captured')
            self.assertEqual(saved['ai_summary']['response_observations'][0]['content'], data['aiSummaryEntities'])
            self.assertEqual(saved['ai_photos']['response_observations'][0]['content'], data['selectedCommentPhotosEntities'])
        logger.info('嵌套空值状态测试完成')

    def test_archive_failure_never_starts_overwrite(self):
        # /* ===== 步骤5：核验归档失败；数据源：旧对象；操作：1) 注入归档失败 2) 不启动最新对象写入 ===== */
        logger.info('开始归档失败测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'data'
            output.mkdir()
            old = output / 'hotel-123-app-capture-object.json'
            old.write_text('{"previous":true}', encoding='utf-8')
            source = root / 'in.jsonl'
            data = {'totalCount': 0, 'groupList': []}
            source.write_text(json.dumps(capture_record(URL, {'hotelId': 123}, {'data': data}, 200)) + '\n', encoding='utf-8')
            # 5.1 归档未提交时不能调用原采集器覆盖旧对象。
            with patch('capture_io.os.replace', side_effect=OSError('磁盘故障')), patch('capture_io.collect_network') as collector:
                with self.assertRaisesRegex(OSError, '磁盘故障'):
                    import_capture(source, output)
                collector.assert_not_called()
            self.assertEqual(json.loads(old.read_text('utf-8')), {'previous': True})
            self.assertEqual(list(output.glob('*.tmp')), [])
        logger.info('归档失败测试完成')

    def test_os_lock_is_released_after_process_termination(self):
        # /* ===== 步骤6：核验进程终止；数据源：独立进程锁；操作：1) 终止持锁进程 2) 再次获取 ===== */
        logger.info('开始进程锁释放测试')
        with tempfile.TemporaryDirectory() as folder:
            # 6.1 硬终止不会遗留永久占用的锁；锁文件存在不能作为占用依据。
            script = ('import sys,time; from pathlib import Path; from capture_io import capture_object_lock\n'
                      'with capture_object_lock(Path(sys.argv[1]), 123, None):\n'
                      ' Path(sys.argv[1], "ready").write_text("ok"); time.sleep(30)\n')
            process = subprocess.Popen([sys.executable, '-c', script, folder], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not Path(folder, 'ready').exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(Path(folder, 'ready').exists())
            finally:
                process.kill()
                process.communicate(timeout=5)
            with capture_object_lock(Path(folder), 123, None):
                self.assertTrue(Path(folder, '.hotel-123-app-capture.lock').exists())
        logger.info('进程锁释放测试完成')
    def test_photo_metadata_in_later_batch_is_preserved_without_claiming_images(self):
        # /* ===== 步骤1：核验图集元数据；数据源：两批模拟响应；操作：1) 保留后续非空值 2) 明示非图片文件 ===== */
        logger.info('开始抓包图集字段测试')
        first = {'hotelName':'酒店','totalCount':3,'groupList':[{'commentList':[{'id':1,'hotelId':123}]}],
                 'aiSummaryEntities':{'summaryItemList':[], 'label':'空首批'}}
        later = {'hotelName':'酒店','totalCount':3,'groupList':[{'commentList':[{'id':2,'hotelId':123}]}],
                 'selectedCommentPhotosEntities':{'items':[{'url':'https://example.com/photo','token':'private'}]}}
        third = {'hotelName':'酒店','totalCount':3,'groupList':[{'commentList':[{'id':3,'hotelId':123}]}],
                 'aiSummaryEntities':{'summaryItemList':[{'title':'第二版'}]},
                 'selectedCommentPhotosEntities':{'items':[{'url':'https://example.com/other'}]}}
        records = [capture_record(URL, {'hotelId':123}, {'data':data}, 200) for data in (first,later,third)]
        with tempfile.TemporaryDirectory() as folder:
            # 1.1 模拟记录不代表真实App已捕获该字段。
            source = Path(folder, 'capture.jsonl')
            source.write_text('\n'.join(json.dumps(row) for row in records)+'\n', encoding='utf-8')
            result = import_capture(source, Path(folder, 'data'), 500)[0]
            saved = json.loads(Path(folder,'data/hotel-123-app-capture-object.json').read_text(encoding='utf-8'))
            self.assertEqual(result['count'], 3)
            self.assertFalse(result['complete'])
            self.assertEqual(saved['ai_photos']['status'], 'response_captured')
            self.assertEqual(saved['ai_photos']['content']['items'][0]['url'], 'https://example.com/photo')
            self.assertNotIn('token', saved['ai_photos']['content']['items'][0])
            self.assertFalse(saved['ai_photos']['files_downloaded'])
            self.assertEqual(len(saved['ai_photos']['response_observations']), 2)
            self.assertEqual(saved['ai_photos']['response_observations'][1]['content']['items'][0]['url'], 'https://example.com/other')
            self.assertEqual(saved['ai_summary']['content']['summaryItemList'][0]['title'], '第二版')
            self.assertEqual(len(saved['ai_summary']['response_observations']), 2)
            self.assertEqual(saved['ai_summary']['response_observations'][1]['content']['summaryItemList'][0]['title'], '第二版')
        logger.info('抓包图集字段测试完成')

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
