import json
import copy
import logging
import unittest
import tempfile
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from hotel_discovery import hotel_directory_batch, hotel_initial, discover_hotels_page

logger = logging.getLogger(__name__)


def request(page=1, city=2):
    return {'destination': {'type': 1, 'geo': {'cityId': city}, 'keyword': {'word': ''}},
            'paging': {'pageIndex': page, 'pageSize': 10}, 'date': {'dateType': 1},
            'filters': [], 'roomQuantity': 1, 'head': {'token': 'secret'}}


def card(identity, city=2, ad=False):
    return {'hotelInfo': {'summary': {'hotelId': str(identity)}, 'nameInfo': {'name': '酒店'},
            'positionInfo': {'cityId': city, 'address': '地址'},
            'advertiseInfo': {'isAdHotel': ad, 'adTraceid': 'secret'},
            'commentInfo': {'commenterNumber': '12条点评'}, 'extend': {'token': 'secret'}},
            'roomInfo': {'memberPrice': 1}}


def initial_html():
    props = {'initListData': {'pagingInfo': {'pageIndex': 1}, 'hotelList': [card(1)]},
             'initListRequest': request()}
    return '<script>self.__next_f.push(' + json.dumps([1, 'J9:' + json.dumps(props) + '\n']) + ')</script>'


class DirectoryPage:
    def __init__(self, response=None, stop=None):
        self.response = response
        self.stop = stop
        self.listeners = []
        self.url = ''
        self.scrolls = 0

    def on(self, event, listener):
        self.listeners.append(listener)

    def remove_listener(self, event, listener):
        self.listeners.remove(listener)

    def goto(self, url, **kwargs):
        self.url = url

    def content(self):
        return initial_html()

    def evaluate(self, script):
        self.scrolls += 1
        if self.stop:
            self.stop.set()
        if self.response:
            for listener in list(self.listeners):
                listener(self.response)

    def wait_for_timeout(self, milliseconds):
        pass


def response(items, ack='Success', page=2, city=2):
    return SimpleNamespace(url='https://m.ctrip.com/restapi/soa2/34951/fetchHotelList', status=200,
        request=SimpleNamespace(post_data_json=request(page, city)),
        json=lambda: {'ResponseStatus': {'Ack': ack},
                      'data': {'pagingInfo': {'pageIndex': page}, 'hotelList': items}})


class HotelDiscoveryTests(unittest.TestCase):
    def test_city_entry_routes_to_hotel_collector(self):
        # /* ===== 步骤6：核对统一入口；数据源：酒店城市链接；操作：1) 路由酒店 2) 保留预算 ===== */
        logger.info('开始酒店目录入口测试')
        from discovery import discover_objects
        with patch('hotel_discovery.discover_hotels', return_value={'kind': 'hotel'}) as collect:
            result = discover_objects('https://hotels.ctrip.com/hotel/shanghai2', 'unused', 2, 20)
            self.assertEqual(result['kind'], 'hotel')
            collect.assert_called_once_with('https://hotels.ctrip.com/hotel/shanghai2', 'unused', 2, 20)
        logger.info('酒店目录入口测试完成')

    def test_browser_pages_checkpoint_and_deduplicate(self):
        # /* ===== 步骤3：核对浏览器分页；数据源：模拟事件；操作：1) 连续页码去重 2) 清理监听并保存清单 ===== */
        logger.info('开始酒店分页测试')
        page = DirectoryPage(response([card(1), card(2)]))
        with tempfile.TemporaryDirectory() as folder:
            result = discover_hotels_page(page, 'https://hotels.ctrip.com/hotel/shanghai2', folder, 2, 10)
            self.assertEqual([x['resource_id'] for x in result['objects']], [1, 2])
            self.assertEqual(result['pages'], 2)
            self.assertEqual(result['reason'], 'page_budget_reached')
            self.assertFalse(result['complete'])
            self.assertEqual(len(Path(result['links_file']).read_text('utf-8').splitlines()), 2)
        self.assertEqual(page.listeners, [])
        logger.info('酒店分页测试完成')

    def test_browser_failure_scope_change_and_timeout_keep_first_page(self):
        # /* ===== 步骤4：核对异常保存；数据源：失败、异城、筛选变化和缺页；操作：1) 拒绝异常页 2) 保留首屏 ===== */
        logger.info('开始酒店异常分页测试')
        changed = response([card(2)])
        changed.request.post_data_json['roomQuantity'] = 2
        for reply in (response([], ack='Failure'), response([card(2)], city=3), changed, None):
            page = DirectoryPage(reply)
            with tempfile.TemporaryDirectory() as folder:
                result = discover_hotels_page(page, 'https://hotels.ctrip.com/hotel/shanghai2', folder, 2, 10, page_timeout=0.002)
                self.assertEqual(result['reason'], 'error')
                self.assertEqual(result['pages'], 1)
                self.assertEqual(len(result['objects']), 1)
                self.assertTrue(Path(result['links_file']).exists())
            self.assertEqual(page.listeners, [])
        logger.info('酒店异常分页测试完成')

    def test_browser_cancel_discards_inflight_page(self):
        # /* ===== 步骤5：核对取消；数据源：滚动期间取消；操作：1) 不接收晚到批次 2) 保存已验页面 ===== */
        logger.info('开始酒店取消测试')
        stop = Event()
        page = DirectoryPage(response([card(2)]), stop)
        with tempfile.TemporaryDirectory() as folder:
            result = discover_hotels_page(page, 'https://hotels.ctrip.com/hotel/shanghai2', folder, 2, 10, stop=stop)
            self.assertEqual(result['reason'], 'cancelled')
            self.assertEqual(len(result['objects']), 1)
        self.assertEqual(page.listeners, [])
        logger.info('酒店取消测试完成')

    def test_default_query_variants_match_but_date_and_sort_changes_do_not(self):
        # /* ===== 步骤0：核对查询条件；数据源：真实请求结构差异；操作：1) 规范默认值 2) 保留日期排序变化 ===== */
        logger.info('开始酒店查询条件测试')
        first = request()
        first['date'] = {'dateType': 1, 'dateInfo': {'checkInDate': '20261026', 'checkOutDate': '20261027', 'offset': 0}, 'flexibleDateOptions': {}}
        first['filters'] = [{'filterId': '80|2', 'type': '80', 'value': '2', 'title': ''},
                            {'filterId': '29|1', 'type': '29', 'value': '1|1', 'subType': '2'}]
        second = copy.deepcopy(first)
        second['date'].pop('flexibleDateOptions')
        second['date']['dateInfo'].pop('offset')
        second['filters'][1].pop('subType')
        second['filters'].insert(0, {'filterId': '17|1', 'type': '17', 'value': '1', 'title': '欢迎度排序'})
        data = {'pagingInfo': {'pageIndex': 1}, 'hotelList': [card(1)]}
        expected = hotel_directory_batch(data, first, 2, 1)['scope_hash']
        self.assertEqual(hotel_directory_batch(data, second, 2, 1)['scope_hash'], expected)
        for changed in ('sort', 'date'):
            altered = copy.deepcopy(second)
            if changed == 'sort':
                altered['filters'][0]['value'] = '2'
            else:
                altered['date']['dateInfo']['checkInDate'] = '20261028'
            self.assertNotEqual(hotel_directory_batch(data, altered, 2, 1)['scope_hash'], expected)
        logger.info('酒店查询条件测试完成')

    def test_city_page_ad_and_public_fields(self):
        # /* ===== 步骤1：核对列表边界；数据源：模拟卡片；操作：1) 过滤广告邻城 2) 不保存会员价和追踪 ===== */
        logger.info('开始酒店目录字段测试')
        data = {'pagingInfo': {'pageIndex': 1}, 'hotelList': [card(1), card(2, ad=True), card(3, city=4)]}
        result = hotel_directory_batch(data, request(), 2, 1)
        self.assertEqual([x['resource_id'] for x in result['objects']], [1])
        self.assertEqual(result['skipped_ads'], 1)
        self.assertEqual(result['skipped_nearby'], 1)
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('memberPrice', json.dumps(result))
        for bad_request, index in [(request(city=4), 1), (request(page=2), 1), (request(), 2)]:
            with self.assertRaises(ValueError):
                hotel_directory_batch(data, bad_request, 2, index)
        logger.info('酒店目录字段测试完成')

    def test_flight_chunks_and_missing_or_wrong_initial(self):
        # /* ===== 步骤2：核对首屏解析；数据源：分段JSON；操作：1) 拼接记录 2) 验证城市 ===== */
        logger.info('开始酒店首屏测试')
        props = {'initListData': {'pagingInfo': {'pageIndex': 1}, 'hotelList': [card(1)]},
                 'initListRequest': request(), 'userInfo': {'token': 'secret'}}
        stream = 'J9:' + json.dumps(['$', 'component', None, props]) + '\n'
        html = ''.join('<script>self.__next_f.push(' + json.dumps([1, part]) + ')</script>'
                       for part in (stream[:40], stream[40:]))
        result = hotel_initial(html, 2)
        self.assertEqual(result['objects'][0]['resource_id'], 1)
        self.assertNotIn('secret', json.dumps(result))
        with self.assertRaises(ValueError):
            hotel_initial(html, 3)
        self.assertIsNone(hotel_initial('<html>登录</html>', 2))
        logger.info('酒店首屏测试完成')
