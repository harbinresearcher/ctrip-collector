import logging
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hotel_browser import collect_hotel_page, hotel_batch

logger = logging.getLogger(__name__)


class HotelBrowserTests(unittest.TestCase):
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
