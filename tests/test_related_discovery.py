"""餐饮详情页相关对象提取测试。"""
import json
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from related_discovery import discover_related, extract_related


def page_state():
    return {
        'poiId': 108786,
        'restaurantDetail': {'poiId': 108786, 'name': '示例餐厅'},
        'commentInfo': {'poiInfo': {'resourceId': 290573, 'resourceType': 12, 'poiId': 108786}},
        'nearbyHotelsList': [
            {'poiId': 0, 'name': '示例酒店', 'url': 'https://hotels.ctrip.com/hotels/123.html#ref',
             'distance': '距离805m', 'commentScore': 4.9, 'commentCount': 71, 'price': '771', 'token': 'secret'},
            {'name': '无效酒店', 'url': 'https://evil.example/hotels/999.html'},
        ],
        'moreRestaurantList': [
            {'poiId': 108786, 'name': '自身'},
            {'poiId': 108712, 'name': '推荐餐厅', 'commentCount': 43, 'averagePrice': 85, 'signature': 'secret'},
            {'poiId': 108712, 'name': '重复餐厅'},
            {'poiId': 0, 'name': '无效餐厅'},
        ],
    }


class RelatedDiscoveryTests(unittest.TestCase):
    def test_preserves_explicit_related_ids_and_safe_fields(self):
        # 1.1 酒店链接来自页面；餐厅仅提供 POI，不猜业务 ID 和链接。
        report = extract_related('https://you.ctrip.com/yougourmet/restdetail/datong275/108786.html', page_state(), '2026-11-13T12:00:00Z')
        self.assertFalse(report['complete'])
        self.assertEqual(report['source']['poi_id'], 108786)
        self.assertEqual(report['source']['resource_id'], 290573)
        self.assertEqual(len(report['objects']), 2)
        hotel, restaurant = report['objects']
        self.assertEqual((hotel['kind'], hotel['hotel_id'], hotel['url']), ('hotel', 123, 'https://hotels.ctrip.com/hotels/123.html'))
        self.assertEqual((restaurant['kind'], restaurant['poi_id']), ('restaurant', 108712))
        self.assertNotIn('resource_id', restaurant)
        self.assertNotIn('url', restaurant)
        self.assertNotIn('secret', json.dumps(report))
        self.assertEqual(report['skipped_invalid'], 2)
        self.assertEqual(report['skipped_self'], 1)
        self.assertEqual(report['skipped_duplicate'], 1)
        self.assertEqual(report['list_status'], {'nearbyHotelsList': 'available', 'moreRestaurantList': 'available'})
        self.assertEqual(report['recommendations_status'], 'available')

    def test_rejects_mismatched_source_identity(self):
        state = page_state()
        state['poiId'] = 999
        with self.assertRaises(ValueError):
            extract_related('https://you.ctrip.com/yougourmet/restdetail/datong275/108786.html', state, '2026-11-13T12:00:00Z')

    def test_discovery_fetches_once_and_writes_report(self):
        # 2.1 保存候选和真实来源，不能把 POI 猜成点评 ID。
        url = 'https://you.ctrip.com/yougourmet/restdetail/datong275/108786.html'
        html = '<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': {'initialState': page_state()}}}) + '</script>'
        response = io.BytesIO(html.encode('utf-8'))
        response.geturl = lambda: url
        with TemporaryDirectory() as directory, patch('related_discovery.urlopen', return_value=response) as fetch:
            report = discover_related(url, directory)
            self.assertEqual(report['reason'], 'recommendations_read')
            self.assertFalse(report['complete'])
            self.assertEqual(len(report['objects']), 2)
            self.assertEqual(json.loads(Path(report['report_file']).read_text(encoding='utf-8')), report)
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.kwargs['timeout'], 30)

    def test_cancel_and_failure_are_not_empty_success(self):
        # 2.2 取消或页面故障必须留下真实状态，不冒充零候选。
        url = 'https://you.ctrip.com/yougourmet/restdetail/datong275/108786.html'
        stop = Event()
        stop.set()
        with TemporaryDirectory() as directory, patch('related_discovery.urlopen') as fetch:
            report = discover_related(url, directory, stop=stop)
            self.assertEqual(report['reason'], 'cancelled')
            fetch.assert_not_called()
        with TemporaryDirectory() as directory, patch('related_discovery.urlopen', side_effect=OSError('network failed')):
            report = discover_related(url, directory)
            self.assertEqual(report['reason'], 'error')
            self.assertIn('network failed', report['error'])
            self.assertFalse(report['complete'])

    def test_rejects_unrelated_url_before_network(self):
        with TemporaryDirectory() as directory, patch('related_discovery.urlopen') as fetch:
            with self.assertRaises(ValueError):
                discover_related('https://evil.example/', directory)
            fetch.assert_not_called()

    def test_changed_redirect_and_inflight_cancel_do_not_save_candidates(self):
        # 2.3 页面跳转对象或请求期间取消都不能保存候选。
        url = 'https://you.ctrip.com/yougourmet/restdetail/datong275/108786.html'
        redirected = io.BytesIO(b'')
        redirected.geturl = lambda: 'https://you.ctrip.com/yougourmet/restdetail/datong275/108712.html'
        with TemporaryDirectory() as directory, patch('related_discovery.urlopen', return_value=redirected):
            report = discover_related(url, directory)
            self.assertEqual(report['reason'], 'error')
            self.assertEqual(report['objects'], [])
        stop = Event()
        pending = io.BytesIO(b'')
        pending.geturl = lambda: url
        def open_and_cancel(*args, **kwargs):
            stop.set()
            return pending
        with TemporaryDirectory() as directory, patch('related_discovery.urlopen', side_effect=open_and_cancel):
            report = discover_related(url, directory, stop=stop)
            self.assertEqual(report['reason'], 'cancelled')
            self.assertEqual(report['objects'], [])

    def test_one_missing_or_invalid_list_keeps_other_candidates_as_partial(self):
        # /* ===== 步骤1：保留可用列表；数据源：模拟页面；操作：1) 标识缺失或错误 2) 不丢弃另一列表 ===== */
        state = page_state()
        del state['nearbyHotelsList']
        url = 'https://you.ctrip.com/yougourmet/restdetail/datong275/108786.html'
        report = extract_related(url, state, '2026-11-13T12:00:00Z')
        self.assertEqual(report['list_status'], {'nearbyHotelsList': 'missing', 'moreRestaurantList': 'available'})
        self.assertEqual(report['recommendations_status'], 'partial')
        self.assertEqual([(row['kind'], row['poi_id']) for row in report['objects']], [('restaurant', 108712)])
        state['nearbyHotelsList'] = {}
        report = extract_related(url, state, '2026-11-13T12:00:00Z')
        self.assertEqual(report['list_status']['nearbyHotelsList'], 'invalid')
        self.assertEqual(len(report['objects']), 1)
        self.assertFalse(report['complete'])
        state = page_state()
        del state['moreRestaurantList']
        report = extract_related(url, state, '2026-11-13T12:00:00Z')
        self.assertEqual(report['list_status']['moreRestaurantList'], 'missing')
        self.assertEqual([(row['kind'], row['hotel_id']) for row in report['objects']], [('hotel', 123)])

    def test_partial_and_unavailable_statuses_are_persisted(self):
        # /* ===== 步骤2：验证报告；数据源：模拟 HTML；操作：1) 标明部分结果 2) 都不可用时报告错误 ===== */
        url = 'https://you.ctrip.com/yougourmet/restdetail/datong275/108786.html'
        for both_unavailable in (False, True):
            with self.subTest(both_unavailable=both_unavailable), TemporaryDirectory() as directory:
                state = page_state()
                del state['moreRestaurantList']
                if both_unavailable:
                    state['nearbyHotelsList'] = None
                html = '<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': {'initialState': state}}}) + '</script>'
                response = io.BytesIO(html.encode('utf-8'))
                response.geturl = lambda: url
                with patch('related_discovery.urlopen', return_value=response):
                    report = discover_related(url, directory)
                self.assertEqual(report['reason'], 'error' if both_unavailable else 'recommendations_partial')
                self.assertEqual(report['recommendations_status'], 'unavailable' if both_unavailable else 'partial')
                self.assertEqual(report['list_status']['moreRestaurantList'], 'missing')
                self.assertEqual(report['list_status']['nearbyHotelsList'], 'invalid' if both_unavailable else 'available')
                self.assertEqual(len(report['objects']), 0 if both_unavailable else 1)
                self.assertEqual(json.loads(Path(report['report_file']).read_text(encoding='utf-8')), report)


if __name__ == '__main__':
    unittest.main()
