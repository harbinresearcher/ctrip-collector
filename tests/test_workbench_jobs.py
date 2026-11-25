import logging
import json
from pathlib import Path
import tempfile
import unittest
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch
from workbench_jobs import make_job

logger = logging.getLogger(__name__)


class JobTests(unittest.TestCase):
    def test_app_shopping_list_uses_android_and_its_own_page_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder).resolve()
            request = {'mode': 'app_shopping_list', 'name': ' 上海 '}
            capture = Mock()
            module = SimpleNamespace(capture_shopping_list=capture)
            with patch('workbench_jobs.Adb') as adb, patch.dict('sys.modules', {'app_shopping_list': module}):
                device = adb.return_value.__enter__.return_value
                stop = Event()
                for settings, pages in (({'restaurant_max_pages': 100}, 1), ({'shopping_max_pages': 20}, 20)):
                    with self.subTest(pages=pages):
                        job = make_job({**request, **settings}, output, 'adb', adb_serial='emulator-5556')
                        self.assertIs(job(stop, lambda _: None), capture.return_value)
                        adb.assert_called_once_with('adb', serial='emulator-5556', ui_backend='uiautomator2')
                        device.connect.assert_called_once_with()
                        capture.assert_called_once_with(device, output, '上海', stop, max_pages=pages)
                        adb.reset_mock()
                        capture.reset_mock()
            for invalid in (False, 0, 21, '2'):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, '1至20'):
                    make_job({**request, 'shopping_max_pages': invalid}, output, 'adb')

    def test_app_shopping_detail_routes_exact_name_and_honors_pre_cancel(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder).resolve()
            request = {'mode': 'app_shopping_detail', 'name': ' 上海比斯特购物村 '}
            with self.assertRaisesRegex(ValueError, 'Android'):
                make_job(request, output)
            capture = Mock()
            module = SimpleNamespace(capture_shopping_detail=capture)
            with patch('workbench_jobs.Adb') as adb, patch.dict('sys.modules', {'app_shopping_detail': module}):
                job = make_job(request, output, 'adb')
                stop = Event()
                stop.set()
                self.assertEqual(job(stop, lambda _: None), {'reason': 'cancelled', 'complete': False})
                adb.assert_not_called()
                capture.assert_not_called()
                self.assertIs(job(Event(), lambda _: None), capture.return_value)
                device = adb.return_value.__enter__.return_value
                device.connect.assert_called_once_with()
                capture.assert_called_once_with(device, output, '上海比斯特购物村')

    def test_app_restaurant_list_uses_android_and_its_own_page_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder).resolve()
            request = {'mode': 'app_restaurant_list', 'name': ' 上海 '}
            for settings, pages in (({}, 1), ({'restaurant_max_pages': 20}, 20)):
                capture = Mock()
                module = SimpleNamespace(capture_restaurant_list=capture)
                with self.subTest(pages=pages), patch('workbench_jobs.Adb') as adb, \
                        patch.dict('sys.modules', {'app_restaurant_list': module}):
                    job = make_job({**request, **settings}, output, 'adb', adb_serial='emulator-5556')
                    stop = Event()
                    self.assertIs(job(stop, lambda _: None), capture.return_value)
                    adb.assert_called_once_with('adb', serial='emulator-5556', ui_backend='uiautomator2')
                    device = adb.return_value.__enter__.return_value
                    device.connect.assert_called_once_with()
                    capture.assert_called_once_with(device, output, '上海', stop, max_pages=pages)
            for invalid in (False, 0, 21, '2'):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, '1至20'):
                    make_job({**request, 'restaurant_max_pages': invalid}, output, 'adb')

    def test_app_restaurant_detail_routes_exact_name_and_honors_pre_cancel(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder).resolve()
            for invalid in (None, '', 1):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    make_job({'mode': 'app_restaurant_detail', 'name': invalid}, output, 'adb')
            request = {'mode': 'app_restaurant_detail', 'name': ' Mercato by Jean-Georges意大利餐厅 '}
            with self.assertRaisesRegex(ValueError, 'Android'):
                make_job(request, output)
            capture = Mock()
            module = SimpleNamespace(capture_restaurant_detail=capture)
            with patch('workbench_jobs.Adb') as adb, patch.dict('sys.modules', {'app_restaurant_detail': module}):
                job = make_job(request, output, 'adb')
                stop = Event()
                stop.set()
                self.assertEqual(job(stop, lambda _: None), {'reason': 'cancelled', 'complete': False})
                adb.assert_not_called()
                capture.assert_not_called()
                self.assertIs(job(Event(), lambda _: None), capture.return_value)
                device = adb.return_value.__enter__.return_value
                device.connect.assert_called_once_with()
                capture.assert_called_once_with(device, output, 'Mercato by Jean-Georges意大利餐厅')

    def test_sight_gallery_uses_android_job_and_its_own_page_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder).resolve()
            request = {'mode': 'sight_gallery', 'name': ' 东方明珠 '}
            jobs = [make_job(request, output, 'adb', adb_serial='emulator-5556'),
                    make_job({**request, 'max_pages': 500}, output, 'adb')]
            for pages, job in zip((10, 500), jobs):
                with self.subTest(pages=pages), patch('workbench_jobs.Adb') as adb, \
                        patch('workbench_jobs.capture_sight_photo_gallery') as capture:
                    device = adb.return_value.__enter__.return_value
                    stop = Event()
                    self.assertIs(job(stop, lambda _: None), capture.return_value)
                    device.connect.assert_called_once_with()
                    capture.assert_called_once_with(device, output, '东方明珠', stop, max_pages=pages, resume_run=None)
            for invalid in (True, 0, 501):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, '1至500'):
                    make_job({**request, 'max_pages': invalid}, output, 'adb')
            with self.assertRaisesRegex(ValueError, '1至50'):
                make_job({**request, 'mode': 'sight_policy', 'max_pages': 51}, output, 'adb')

    def test_sight_gallery_resume_passes_only_a_valid_previous_run(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder).resolve()
            request = {'mode': 'sight_gallery', 'name': '东方明珠', 'max_pages': 3}
            for run_id in ('a' * 32, ''):
                with self.subTest(run_id=run_id), patch('workbench_jobs.Adb') as adb, \
                        patch('workbench_jobs.capture_sight_photo_gallery') as capture:
                    stop = Event()
                    make_job({**request, 'resume_sight_gallery_run': run_id}, output, 'adb')(stop, lambda _: None)
                    capture.assert_called_once_with(adb.return_value.__enter__.return_value, output, '东方明珠',
                                                    stop, max_pages=3, resume_run=run_id or None)
            for invalid in (None, False, 'A' * 32, '.' + 'a' * 31, 'a' * 31, '../' + 'a' * 32):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, '任务编号'):
                    make_job({**request, 'resume_sight_gallery_run': invalid}, output, 'adb')

    def test_coverage_reads_existing_local_database_without_network(self):
        # /* ===== 步骤1：核对覆盖报告；数据源：本地评论库；操作：1) 统计字段 2) 不请求网络 ===== */
        logger.info('开始工作台覆盖统计测试')
        import json
        import sqlite3
        from contextlib import closing
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / 'network.sqlite3'
            with closing(sqlite3.connect(database)) as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.execute('INSERT INTO network_reviews VALUES(?,?,?,?)', ('sight-1', '1', json.dumps({'content':'不可复制的评论', 'images':[]}), 'time'))
                db.commit()
            before = database.read_bytes()
            # 1.1 不读取其他来源，不把已存条数或显示总数当作平台全量。
            with patch('urllib.request.urlopen', side_effect=AssertionError('不可访问网络')):
                result = make_job({'mode':'coverage'}, root)(Event(), lambda _: None)
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(result['scope'], 'local_network_database_only')
            self.assertFalse(result['complete'])
            self.assertEqual(result['objects'][0]['review_count'], 1)
            self.assertEqual(result['objects'][0]['fields']['images']['present'], 1)
            self.assertEqual(result['objects'][0]['fields']['images']['nonempty'], 0)
            self.assertNotIn('不可复制的评论', json.dumps(result, ensure_ascii=False))
            self.assertEqual(json.loads(Path(result['report_file']).read_text(encoding='utf-8')), result)
        logger.info('工作台覆盖统计测试完成')

    def test_coverage_missing_database_and_pre_cancel(self):
        # /* ===== 步骤1：核对空目录与取消；数据源：本地工作目录；操作：1) 不新建数据库 2) 取消不写报告 ===== */
        logger.info('开始覆盖统计空目录测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            job = make_job({'mode':'coverage'}, root)
            cancelled = Event()
            cancelled.set()
            # 1.1 已取消时立即返回，不应打开数据库或生成报告。
            with patch('workbench_jobs.write_coverage_report', side_effect=AssertionError('不可调用')):
                self.assertEqual(job(cancelled, lambda _: None)['reason'], 'cancelled')
            self.assertEqual(list(root.iterdir()), [])
            with self.assertRaisesRegex(ValueError, '没有网络评论数据库'):
                job(Event(), lambda _: None)
            self.assertEqual(list(root.iterdir()), [])
        logger.info('覆盖统计空目录测试完成')

    def test_resume_discovery_reuses_original_budget_and_only_retries_remaining(self):
        # /* ===== 步骤1：核对工作台续跑；数据源：真实中断报告；操作：1) 复用原预算 2) 只重试未完成地区 ===== */
        logger.info('开始工作台地区续跑测试')
        from multi_discovery import discover_many
        entries = ['https://you.ctrip.com/sight/a1/s0-p1.html', 'https://you.ctrip.com/sight/b2/s0-p1.html']
        stop = Event()
        calls = []
        def first(entry, folder, pages, objects, event):
            calls.append((entry, pages, objects))
            event.set()
            return {'reason':'page_budget_reached', 'objects':[{'url':'https://you.ctrip.com/sight/a1/100.html'}]}
        def second(entry, folder, pages, objects, event):
            calls.append((entry, pages, objects))
            return {'reason':'page_budget_reached', 'objects':[{'url':'https://you.ctrip.com/sight/b2/200.html'}]}
        with tempfile.TemporaryDirectory() as folder:
            # 1.1 首次已完成地区留存；续跑表单不能覆盖报告的页数和对象预算。
            original = discover_many(entries, folder, 2, 7, stop, first)
            self.assertEqual(original['reason'], 'cancelled')
            calls.clear()
            request = {'mode':'resume_discovery', 'report_file':Path(original['report_file']).name,
                       'discovery_pages':99, 'max_objects':99}
            with patch('workbench_jobs.discover_objects', side_effect=second), \
                 patch('workbench_jobs.discover_regions') as seeds:
                result = make_job(request, folder)(Event(), lambda _: None)
            self.assertEqual(calls, [(entries[1], 2, 7)])
            self.assertEqual(result['discovery']['resumed_regions'], 1)
            self.assertEqual(len(result['discovery']['objects']), 2)
            self.assertFalse(result['complete'])
            self.assertIsNone(result['collection'])
            seeds.assert_not_called()
        logger.info('工作台地区续跑测试完成')

    def test_resume_rejects_invalid_file_and_tampered_checkpoint(self):
        # /* ===== 步骤1：核对续跑边界；数据源：文件名及快照；操作：1) 拒绝越界 2) 校验后才请求 ===== */
        logger.info('开始续跑安全检查')
        from multi_discovery import discover_many
        with tempfile.TemporaryDirectory() as folder:
            # 1.1 禁止任意路径和不属于输出目录的报告。
            for filename in ('../secret.json', 'other.json', '/tmp/a.json', ''):
                with self.subTest(filename=filename), self.assertRaises(ValueError):
                    make_job({'mode':'resume_discovery', 'report_file':filename}, folder)
            entry = 'https://you.ctrip.com/sight/a1/s0-p1.html'
            original = discover_many([entry], folder, 1, 1, Event(),
                lambda *args: {'reason':'page_budget_reached','objects':[{'url':'https://you.ctrip.com/sight/a1/100.html'}]})
            path = Path(original['report_file'])
            saved = json.loads(path.read_text(encoding='utf-8'))
            saved['objects'][0]['url'] = 'https://you.ctrip.com/sight/a1/999.html'
            path.write_text(json.dumps(saved), encoding='utf-8')
            job = make_job({'mode':'resume_discovery', 'report_file':path.name}, folder)
            with patch('workbench_jobs.discover_objects') as finder, self.assertRaises(ValueError):
                job(Event(), lambda _: None)
            finder.assert_not_called()
        logger.info('续跑安全检查完成')

    def test_resume_uses_right_finder_and_stops_before_collection_on_error(self):
        # /* ===== 步骤1：核验续跑类型；数据源：购物及酒店报告；操作：1) 路由目录 2) 错误不采点评 ===== */
        logger.info('开始续跑路由测试')
        from multi_discovery import discover_many
        with tempfile.TemporaryDirectory() as folder:
            for entry, name in [('2', 'shopping'), ('https://hotels.ctrip.com/hotel/shanghai2', 'hotel')]:
                stop = Event()
                stop.set()
                # 1.1 已取消报告没有目录结果，续跑时仅允许对应业务适配器。
                old = discover_many([entry], folder, 1, 1, stop, lambda *args: None)
                req = {'mode':'resume_discovery', 'report_file':Path(old['report_file']).name, 'collect_discovered':True}
                with self.subTest(kind=name), patch('workbench_jobs.discover_shopping') as shopping, \
                     patch('workbench_jobs.discover_objects') as objects, \
                     patch('workbench_jobs.collect_public_hotels') as hotels, \
                     patch('workbench_jobs.collect_batch') as comments:
                    finder = shopping if name == 'shopping' else objects
                    finder.return_value = {'reason':'error', 'error':'failed', 'objects':[]}
                    result = make_job(req, folder)(Event(), lambda _: None)
                    self.assertEqual(result['reason'], 'error')
                    self.assertIsNone(result['collection'])
                    finder.assert_called_once()
                    hotels.assert_not_called()
                    comments.assert_not_called()
        logger.info('续跑路由测试完成')

    def test_resume_requires_homogeneous_trusted_entries(self):
        # /* ===== 步骤1：核对入口类型；数据源：旧任务报告；操作：1) 拒绝混合 2) 拒绝伪造域名 ===== */
        logger.info('开始续跑入口测试')
        from multi_discovery import discover_many
        with tempfile.TemporaryDirectory() as folder:
            stop = Event()
            stop.set()
            for entries in ([ 'https://you.ctrip.com/sight/a1/s0-p1.html', 'https://hotels.ctrip.com/hotel/shanghai2'],
                            ['https://you.ctrip.com.evil.example/sight/a1/s0-p1.html']):
                # 1.1 旧报告格式虽有效，也不能触发与其类型不匹配的外部请求。
                old = discover_many(entries, folder, 1, 1, stop, lambda *args: None)
                with self.subTest(entries=entries), self.assertRaises(ValueError):
                    make_job({'mode':'resume_discovery', 'report_file':Path(old['report_file']).name}, folder)
        logger.info('续跑入口测试完成')
    def test_app_modes_use_mobile_adapter_before_other_validation(self):
        # /* ===== 步骤1：核对手机入口；数据源：工作台表单；操作：1) 交给手机适配器 2) 保留500页预算 ===== */
        logger.info('开始手机入口委托测试')
        with tempfile.TemporaryDirectory() as folder:
            # 1.1 酒店图集允许超过旧景点政策上限的页数。
            marker = object()
            with patch('workbench_jobs.prepare_app_job', return_value=marker) as prepare:
                for mode in ('app_reviews', 'app_ai', 'app_gallery'):
                    request = {'mode': mode, 'max_pages': 500}
                    self.assertIs(make_job(request, folder, 'adb.exe'), marker)
                    prepare.assert_called_with(request, folder, 'adb.exe')
        logger.info('手机入口委托测试完成')

    def test_auto_discovery_validates_and_runs_selected_stages(self):
        # /* ===== 步骤1：核验自动目录；数据源：表单与模拟返回；操作：1) 拒绝无效参数 2) 核对串接 ===== */
        logger.info('开始自动目录适配测试')
        with tempfile.TemporaryDirectory() as folder:
            for override in ({'kind': 'unknown'}, {'max_regions': True}, {'max_objects': 0},
                             {'discovery_pages': 0}, {'collect_discovered': 'false'}):
                request = {'mode': 'auto_discovery', 'kind': 'sight', **override}
                with self.subTest(request=request), self.assertRaises(ValueError):
                    make_job(request, folder)
            # 1.1 地区目录和采集使用相同取消事件；默认只发现。
            seed = {'entries': ['city'], 'report_file': 'seed.json', 'total_visible': 9}
            found = {'reason': 'queue_exhausted', 'error': '', 'objects': [{'url': 'https://you.ctrip.com/sight/city1/9.html'}]}
            stop = Event()
            with patch('workbench_jobs.discover_regions', return_value=seed) as regions, \
                 patch('workbench_jobs.discover_many', return_value=found) as discover, \
                 patch('workbench_jobs.collect_batch', return_value={'reason': 'queue_exhausted'}) as collect:
                request = {'mode': 'auto_discovery', 'kind': 'sight', 'max_regions': 2,
                           'discovery_pages': 3, 'max_objects': 4, 'limit': 5}
                result = make_job(request, folder)(stop, lambda _: None)
                self.assertFalse(result['complete'])
                self.assertIsNone(result['collection'])
                collect.assert_not_called()
                self.assertIs(discover.call_args.args[4], stop)
                self.assertEqual(regions.call_args.args[1:], ('sight', 2))
                result = make_job({**request, 'collect_discovered': True}, folder)(stop, lambda _: None)
                self.assertEqual(result['collection']['reason'], 'queue_exhausted')
                self.assertEqual(collect.call_args.args[0], [found['objects'][0]['url']])
                self.assertIs(collect.call_args.kwargs['stop'], stop)
        logger.info('自动目录适配测试完成')

    def test_discovery_error_and_cancellation_never_start_collection(self):
        # /* ===== 步骤1：核对中止边界；数据源：目录阶段；操作：1) 失败停下 2) 取消停下 ===== */
        logger.info('开始自动目录中止测试')
        with tempfile.TemporaryDirectory() as folder:
            request = {'mode': 'auto_discovery', 'kind': 'hotel', 'collect_discovered': True}
            seed = {'entries': ['city'], 'report_file': 'seed.json', 'total_visible': 1}
            with patch('workbench_jobs.discover_regions', return_value=seed), \
                 patch('workbench_jobs.discover_many') as discover, \
                 patch('workbench_jobs.collect_public_hotels') as collect:
                for reason in ('error', 'cancelled'):
                    # 1.1 已发现对象不意味着失败或取消后可继续请求。
                    discover.return_value = {'reason': reason, 'error': 'bad' if reason == 'error' else '',
                                             'objects': [{'url': 'https://hotels.ctrip.com/hotels/1.html'}]}
                    result = make_job(request, folder)(Event(), lambda _: None)
                    self.assertEqual(result['reason'], reason)
                    self.assertIsNone(result['collection'])
                    collect.assert_not_called()
                discover.return_value = {'reason': 'queue_exhausted', 'error': '',
                                         'objects': [{'url': 'https://hotels.ctrip.com/hotels/1.html'}]}
                collect.return_value = {'reason': 'queue_exhausted', 'complete': False}
                result = make_job(request, folder)(Event(), lambda _: None)
                self.assertFalse(result['complete'])
                collect.assert_called_once()
        logger.info('自动目录中止测试完成')

    def test_cancellation_during_seed_and_shopping_finder(self):
        # /* ===== 步骤1：核对阶段交接；数据源：地区首页与购物目录；操作：1) 取消检查 2) 使用购物适配器 ===== */
        logger.info('开始地区阶段交接测试')
        with tempfile.TemporaryDirectory() as folder:
            stop = Event()
            seed = {'entries': ['2'], 'report_file': 'seed.json', 'total_visible': 1}
            def cancel_seed(*args):
                stop.set()
                return seed
            # 1.1 首页读取期间取消，必须保留首页记录并禁止后续目录请求。
            with patch('workbench_jobs.discover_regions', side_effect=cancel_seed), \
                 patch('workbench_jobs.discover_many') as discover:
                result = make_job({'mode': 'auto_discovery', 'kind': 'shopping'}, folder)(stop, lambda _: None)
                self.assertEqual(result['reason'], 'cancelled')
                self.assertEqual(result['seed'], seed)
                discover.assert_not_called()
            stop.clear()
            # 1.2 购物使用地区数字，不与其他业务目录解析器混用。
            with patch('workbench_jobs.discover_regions', return_value=seed), \
                 patch('workbench_jobs.discover_many', return_value={'reason': 'queue_exhausted', 'objects': []}) as discover:
                make_job({'mode': 'auto_discovery', 'kind': 'shopping'}, folder)(stop, lambda _: None)
                from workbench_jobs import discover_shopping
                self.assertIs(discover.call_args.args[5], discover_shopping)
        logger.info('地区阶段交接测试完成')

    def test_validate_before_start_and_forward_cancellation(self):
        # /* ===== 步骤1：核验任务适配；数据源：工作台表单；操作：1) 提前拒绝错误 2) 传递事件 ===== */
        logger.info('开始任务适配测试')
        with tempfile.TemporaryDirectory() as folder:
            for request in ({'mode': 'unknown'}, {'mode': 'reviews', 'urls': ['http://localhost'], 'limit': 1}, {'mode': 'reviews', 'urls': ['https://you.ctrip.com/food/city1/9.html'], 'limit': True}):
                with self.assertRaises(ValueError):
                    make_job(request, folder)
            # 1.1 规范链接去重，取消事件不替换为另一个实例。
            stop = Event()
            emit = Mock()
            url = 'https://you.ctrip.com/food/city1/9.html'
            with patch('workbench_jobs.collect_batch', return_value={'reason': 'cancelled'}) as collect:
                job = make_job({'mode': 'reviews', 'urls': [url, url], 'limit': 5}, folder)
                self.assertEqual(job(stop, emit)['reason'], 'cancelled')
                self.assertEqual(collect.call_args.args[0], [url])
                self.assertIs(collect.call_args.kwargs['stop'], stop)
                self.assertIs(collect.call_args.kwargs['emit'], emit)
                emit.assert_called()
        logger.info('任务适配测试完成')
