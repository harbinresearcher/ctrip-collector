import logging
import json
import unittest
import tempfile
from threading import Event
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hotel_browser import collect_hotel, collect_hotel_page, hotel_batch

logger = logging.getLogger(__name__)


class HotelBrowserTests(unittest.TestCase):
    def test_hotel_wrapper_forwards_refresh_and_closes_browser(self):
        # /* ===== 步骤1：核验浏览器包装；数据源：受控浏览器；操作：1) 传递刷新 2) 错误时关闭 ===== */
        logger.info('开始酒店刷新包装测试')
        context = MagicMock()
        context.pages = ['页面']
        with patch('playwright.sync_api.sync_playwright'), patch('hotel_browser.open_hotel_context', return_value=context), \
             patch('hotel_browser.collect_hotel_page', side_effect=RuntimeError('模拟失败')) as collect:
            with self.assertRaisesRegex(RuntimeError, '模拟失败'):
                collect_hotel('https://hotels.ctrip.com/hotels/123.html', 'output', refresh=True)
            self.assertTrue(collect.call_args.kwargs['refresh'])
            context.close.assert_called_once()
        # 1.1 无效布尔值必须在启动浏览器前被拒绝。
        with patch('playwright.sync_api.sync_playwright') as launch, self.assertRaises(ValueError):
            collect_hotel('https://hotels.ctrip.com/hotels/123.html', 'output', refresh='true')
        launch.assert_not_called()
        logger.info('酒店刷新包装测试完成')

    def test_refresh_reads_browser_page_after_stored_target(self):
        # /* ===== 步骤1：验证浏览器刷新；数据源：两次酒店首屏；操作：1) 达到旧目标 2) 保存新内容 ===== */
        logger.info('开始酒店浏览器刷新测试')
        class Page:
            def __init__(self, content): self.content = content
            def on(self, *args): pass
            def goto(self, *args, **kwargs): pass
            def evaluate(self, *args):
                return {'totalCount':1, 'groupList':[{'commentList':[{'id':7,'hotelId':123,'content':self.content}]}]}
            def remove_listener(self, *args): pass
        with tempfile.TemporaryDirectory() as folder, patch('hotel_browser.load_target', return_value={'details': {}}):
            url = 'https://hotels.ctrip.com/hotels/123.html'
            first = collect_hotel_page(Page('原文'), url, folder, limit=1)
            self.assertEqual(first['count'], 1)
            # 1.1 显式刷新不可被旧库数量短路。
            result = collect_hotel_page(Page('原文补充'), url, folder, limit=1, refresh=True)
            self.assertEqual((result['pages'], result['target_scope'], result['count']), (1, 'current_run', 1))
            rows = [json.loads(line) for line in Path(result['observations_jsonl']).read_text('utf-8').splitlines()]
            self.assertEqual({row['data']['content'] for row in rows}, {'原文', '原文补充'})
            for bad in ('true', 1, None):
                with self.subTest(refresh=bad), self.assertRaises(ValueError):
                    collect_hotel_page(Page('原文'), url, folder, refresh=bad)
        logger.info('酒店浏览器刷新测试完成')

    def test_initial_empty_fields_remain_available_without_claiming_success(self):
        # /* ===== 步骤1：检查空值兼容；数据源：模拟初始页；操作：1) 保留原字段 2) 标记未采到内容 ===== */
        logger.info('开始检查空字段')
        class Page:
            def on(self, *args): pass
            def goto(self, *args, **kwargs): pass
            def evaluate(self, *args):
                return {'totalCount': 0, 'groupList': [], 'aiSummaryEntities': {'summaryItemList': [], 'message': 'empty'},
                        'selectedCommentPhotosEntities': {}}
            def wait_for_timeout(self, *args): raise AssertionError('不应等待')
            def remove_listener(self, *args): pass
        # 1.1 旧字段仍可直接读取，且空值不误报已采内容。
        with tempfile.TemporaryDirectory() as folder, patch('hotel_browser.load_target', return_value={'details': {}}):
            collect_hotel_page(Page(), 'https://hotels.ctrip.com/hotels/123.html', folder)
            target = json.loads(Path(folder, 'hotel-123-object.json').read_text('utf-8'))
            self.assertEqual(target['ai_summary']['content'].get('message'), 'empty')
            self.assertEqual(target['ai_summary']['status'], 'not_captured')
            self.assertEqual(target['ai_photos']['content'], {})
            self.assertEqual(target['ai_photos']['status'], 'not_captured')
        logger.info('空字段检查完成')

    def test_cancel_after_response_keeps_observed_ai(self):
        # /* ===== 步骤2：检查停止留存；数据源：模拟浏览器响应；操作：1) 保存后停止 2) 核对最终状态 ===== */
        logger.info('开始检查停止后的 AI 留存')
        stop = Event()
        initial = {'totalCount': 2, 'groupList': [{'commentList': [{'id': 1, 'hotelId': 123}]}]}
        data = {'totalCount': 2, 'groupList': [{'commentList': [{'id': 2, 'hotelId': 123}]}],
                'aiSummaryEntities': {'summaryItemList': [{'title': '停止前总结'}]}}
        options = {'pageIndex': 2, 'pageSize': 10, 'keyWord': '', 'commonStatisticList': ['1'],
                   'rooms': [], 'travelTypes': [], 'filterDateTypeList': [], 'orderTypes': [], 'repeatComment': 1}
        response = SimpleNamespace(url='https://m.ctrip.com/restapi/soa2/34308/getHotelCommentInfo',
            request=SimpleNamespace(post_data_json={'hotelId': 123, 'commentFilterOptions': options}),
            json=lambda: {'data': data})
        class Page:
            def on(self, event, callback): self.callback = callback
            def goto(self, *args, **kwargs): pass
            def evaluate(self, script): return initial if 'preloadedCommentData' in script else True
            def wait_for_timeout(self, *args): self.callback(response); stop.set()
            def remove_listener(self, *args): pass
        # 2.1 用户取消不删除已收到并校验的总结。
        with tempfile.TemporaryDirectory() as folder, patch('hotel_browser.load_target', return_value={'details': {}}):
            result = collect_hotel_page(Page(), 'https://hotels.ctrip.com/hotels/123.html', folder, limit=2, stop=stop)
            target = json.loads(Path(folder, 'hotel-123-object.json').read_text('utf-8'))
            self.assertEqual(result['reason'], 'cancelled')
            self.assertEqual(result['ai_summary_status'], 'response_captured')
            self.assertEqual(target['ai_summary']['content']['summaryItemList'][0]['title'], '停止前总结')
        logger.info('停止后的 AI 留存检查完成')

    def test_failed_atomic_update_keeps_previous_object_and_status(self):
        # /* ===== 步骤3：检查写入失败；数据源：模拟浏览器响应；操作：1) 中断替换 2) 保留旧对象 ===== */
        logger.info('开始检查 AI 字段写入失败')
        initial = {'totalCount': 2, 'groupList': [{'commentList': [{'id': 1, 'hotelId': 123}]}]}
        data = {'totalCount': 2, 'groupList': [{'commentList': [{'id': 2, 'hotelId': 123}]}],
                'aiSummaryEntities': {'summaryItemList': [{'title': '未落盘'}]}}
        options = {'pageIndex': 2, 'pageSize': 10, 'keyWord': '', 'commonStatisticList': ['1'],
                   'rooms': [], 'travelTypes': [], 'filterDateTypeList': [], 'orderTypes': [], 'repeatComment': 1}
        response = SimpleNamespace(url='https://m.ctrip.com/restapi/soa2/34308/getHotelCommentInfo',
            request=SimpleNamespace(post_data_json={'hotelId': 123, 'commentFilterOptions': options}),
            json=lambda: {'data': data})
        class Page:
            def on(self, event, callback): self.callback = callback
            def goto(self, *args, **kwargs): pass
            def evaluate(self, script): return initial if 'preloadedCommentData' in script else True
            def wait_for_timeout(self, *args): self.callback(response)
            def remove_listener(self, *args): pass
        # 3.1 首次网络对象写入正常，后续回调替换失败后保留旧文件。
        original = Path.replace
        calls = []
        def replace(path, destination):
            if str(destination).endswith('hotel-123-object.json'):
                calls.append(path)
                if len(calls) == 2:
                    raise OSError('模拟对象替换失败')
            return original(path, destination)
        with tempfile.TemporaryDirectory() as folder, patch('hotel_browser.load_target', return_value={'details': {}}), patch.object(Path, 'replace', replace):
            result = collect_hotel_page(Page(), 'https://hotels.ctrip.com/hotels/123.html', folder, limit=2)
            target = json.loads(Path(folder, 'hotel-123-object.json').read_text('utf-8'))
            self.assertEqual(result['reason'], 'error')
            self.assertEqual(result['ai_summary_status'], 'not_captured')
            self.assertEqual(target['ai_summary']['status'], 'not_captured')
            self.assertNotIn('未落盘', json.dumps(target, ensure_ascii=False))
        logger.info('AI 字段写入失败检查完成')

    def test_later_browser_batches_preserve_ai_fields_and_reject_other_hotel(self):
        # /* ===== 步骤1：检查翻页字段；数据源：模拟浏览器响应；操作：1) 留存各批字段 2) 拒绝错误酒店 ===== */
        logger.info('开始检查翻页 AI 字段')
        initial = {'totalCount': 4, 'groupList': [{'commentList': [{'id': 1, 'hotelId': 123}]}],
                   'aiSummaryEntities': {'summaryItemList': []}, 'selectedCommentPhotosEntities': {}}
        pages = [
            {'totalCount': 4, 'groupList': [{'commentList': [{'id': 2, 'hotelId': 123}]}],
             'aiSummaryEntities': {'summaryItemList': [{'title': '有效总结'}], 'token': 'DO_NOT_SAVE'},
             'selectedCommentPhotosEntities': {'items': [{'url': 'https://example.com/a', 'token': 'DO_NOT_SAVE'}]}},
            {'totalCount': 4, 'groupList': [{'commentList': [{'id': 3, 'hotelId': 123}]}],
             'aiSummaryEntities': None, 'selectedCommentPhotosEntities': {}},
            {'totalCount': 4, 'groupList': [{'commentList': [{'id': 4, 'hotelId': 456}]}],
             'aiSummaryEntities': {'summaryItemList': [{'title': '错误酒店数据'}]}}
        ]
        class Page:
            index = 0
            def on(self, event, callback): self.callback = callback
            def goto(self, *args, **kwargs): pass
            def evaluate(self, script): return initial if 'preloadedCommentData' in script else True
            def wait_for_timeout(self, *args):
                data = pages[self.index]
                self.index += 1
                options = {'pageIndex': self.index + 1, 'pageSize': 10, 'keyWord': '', 'commonStatisticList': ['1'],
                           'rooms': [], 'travelTypes': [], 'filterDateTypeList': [], 'orderTypes': [], 'repeatComment': 1}
                response = SimpleNamespace(url='https://m.ctrip.com/restapi/soa2/34308/getHotelCommentInfo',
                    request=SimpleNamespace(post_data_json={'hotelId': 123, 'commentFilterOptions': options}),
                    json=lambda: {'data': data})
                self.callback(response)
            def remove_listener(self, *args): pass
        # 1.1 第四页身份错误时保留前三页，并保留已观察到的空值而不当作有效字段。
        with tempfile.TemporaryDirectory() as folder, patch('hotel_browser.load_target', return_value={'details': {}}):
            result = collect_hotel_page(Page(), 'https://hotels.ctrip.com/hotels/123.html', folder, limit=4)
            target = json.loads(Path(folder, 'hotel-123-object.json').read_text('utf-8'))
            self.assertEqual(result['reason'], 'error')
            self.assertEqual(result['count'], 3)
            self.assertEqual(result['ai_summary_status'], 'response_captured')
            self.assertEqual(target['ai_summary']['status'], 'response_captured')
            self.assertEqual(target['ai_summary']['content']['summaryItemList'][0]['title'], '有效总结')
            self.assertEqual([row['page_index'] for row in target['ai_summary']['response_observations']], [1, 2, 3])
            self.assertIsNone(target['ai_summary']['response_observations'][2]['content'])
            self.assertEqual(target['ai_photos']['status'], 'response_captured')
            self.assertFalse(target['ai_photos']['files_downloaded'])
            self.assertEqual(len(target['ai_photos']['response_observations']), 3)
            self.assertNotIn('DO_NOT_SAVE', json.dumps(target, ensure_ascii=False))
            self.assertNotIn('错误酒店数据', json.dumps(target, ensure_ascii=False))
            persisted = list(Path(folder).glob('hotel-123-report-*.json'))
            self.assertEqual(json.loads(persisted[0].read_text('utf-8'))['ai_summary_status'], 'response_captured')
        logger.info('翻页 AI 字段检查完成')

    def test_response_pagination_rejects_changed_filters_and_unknown_data(self):
        # /* ===== 步骤3：核对浏览器事件；数据源：模拟翻页响应；操作：1) 连续保存 2) 拒绝改变条件 3) 不导出认证 ===== */
        logger.info('开始浏览器事件检查')
        for scenario in ('valid', 'filter_changed', 'bad_response'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as folder:
                options = {'pageIndex': 2, 'pageSize': 10, 'keyWord': '', 'commonStatisticList': ['1'],
                           'rooms': [], 'travelTypes': [], 'filterDateTypeList': [], 'orderTypes': [], 'repeatComment': 1}
                if scenario == 'filter_changed':
                    options['orderTypes'] = [1]
                initial = {'totalCount': 20, 'groupList': [{'commentList': [{'id': 1, 'hotelId': 123}]}]}
                next_data = {'totalCount': 20, 'groupList': [{'commentList': [{'id': 2, 'hotelId': 123}]}]}
                response = SimpleNamespace(url='https://m.ctrip.com/restapi/soa2/34308/getHotelCommentInfo',
                    request=SimpleNamespace(post_data_json={'hotelId': 123, 'commentFilterOptions': options,
                                                             'head': {'auth': 'DO_NOT_EXPORT'}}),
                    json=lambda: {'data': next_data} if scenario != 'bad_response' else {'ResponseStatus': {}})
                class Page:
                    def on(self, event, callback): self.callback = callback
                    def goto(self, *args, **kwargs): pass
                    def evaluate(self, script): return initial if 'preloadedCommentData' in script else True
                    def wait_for_timeout(self, *args): self.callback(response)
                    def remove_listener(self, *args): pass
                # 3.1 用真正的持久化层检查错误后已提交首批仍可导出。
                with patch('hotel_browser.load_target', return_value={'details': {}}):
                    result = collect_hotel_page(Page(), 'https://hotels.ctrip.com/hotels/123.html', folder, limit=2)
                self.assertEqual(result['count'], 2 if scenario == 'valid' else 1)
                self.assertEqual(result['reason'], 'target_reached' if scenario == 'valid' else 'error')
                self.assertNotIn('DO_NOT_EXPORT', Path(folder, 'hotel-123-filters.json').read_text('utf-8'))
        logger.info('浏览器事件检查完成')

    def test_valid_empty_hotel_does_not_wait_for_login(self):
        # /* ===== 步骤2：识别合法零点评；数据源：空酒店状态；操作：1) 不误报登录失败 2) 保存零条报告 ===== */
        logger.info('开始零点评检查')
        class EmptyPage:
            def on(self, *args): pass
            def goto(self, *args, **kwargs): pass
            def evaluate(self, *args):
                return {'totalCount': 0, 'groupList': [], 'hotelName': '零点评酒店'}
            def wait_for_timeout(self, *args):
                raise AssertionError('合法空状态不应等待登录')
            def remove_listener(self, *args): pass
        # 2.1 所有网络调用隔离，仅验证空状态分类与落盘。
        with tempfile.TemporaryDirectory() as folder, patch('hotel_browser.load_target', return_value={'details': {}}):
            result = collect_hotel_page(EmptyPage(), 'https://hotels.ctrip.com/hotels/123.html', folder)
            self.assertEqual(result['reason'], 'empty_page')
            self.assertEqual(result['count'], 0)
            self.assertFalse(result['complete'])
        logger.info('零点评检查完成')

    def test_login_navigation_can_replace_page_context(self):
        # /* ===== 步骤4：核对登录回跳；数据源：浏览器导航事件；操作：1) 重试已销毁上下文 2) 读取新页面 ===== */
        logger.info('开始登录回跳检查')
        from playwright.sync_api import Error
        class Page:
            reads = 0
            def on(self, *args): pass
            def goto(self, *args, **kwargs): pass
            def evaluate(self, *args):
                self.reads += 1
                if self.reads == 1:
                    raise Error('Execution context was destroyed, most likely because of a navigation')
                return {'totalCount': 0, 'groupList': []}
            def wait_for_timeout(self, *args): pass
            def remove_listener(self, *args): pass
        # 4.1 用户登录后的页面替换不应导致整个采集退出。
        with tempfile.TemporaryDirectory() as folder, patch('hotel_browser.load_target', return_value={'details': {}}):
            result = collect_hotel_page(Page(), 'https://hotels.ctrip.com/hotels/123.html', folder)
            self.assertEqual(result['reason'], 'empty_page')
        logger.info('登录回跳检查完成')

    def test_identity_schema_and_full_fields(self):
        # /* ===== 步骤1：核对响应边界；数据源：模拟酒店响应；操作：1) 保留公开字段 2) 拒绝错误对象与未知结构 ===== */
        logger.info('开始酒店响应检查')
        data = {'totalCount': 100, 'groupList': [{'commentList': [
            {'id': 'a', 'hotelId': 123, 'content': '内容', 'rating': 5,
             'checkinDate': '2026-09', 'imageList': ['photo'], 'userInfo': {'userId': 'private'}}]}]}
        # 1.1 对象不一致、未知结构均不能记作正常空页。
        result = hotel_batch(data, 123)['result']
        self.assertEqual(result['items'][0]['commentId'], 'a')
        self.assertEqual(result['items'][0]['checkinDate'], '2026-09')
        self.assertNotIn('userId', result['items'][0]['userInfo'])
        with self.assertRaises(ValueError):
            hotel_batch(data, 456)
        with self.assertRaises(ValueError):
            hotel_batch({'ResponseStatus': {}}, 123)
        self.assertEqual(hotel_batch({'totalCount': 0, 'groupList': []}, 123)['result']['items'], [])
        logger.info('酒店响应检查完成')
