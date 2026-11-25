import io
import json
import logging
import tempfile
import unittest
from unittest.mock import patch
from network import fetch_page, collect_network

logger = logging.getLogger(__name__)


class RestaurantPoiTests(unittest.TestCase):
    def test_upgrade_reads_new_route_despite_existing_target_count(self):
        # /* ===== 步骤1：核对升级续跑；数据源：旧库与新响应；操作：1) 保留旧数据 2) 确认新通道实际读取 ===== */
        logger.info('开始升级续跑测试')
        from batch import collect_batch
        target = dict(kind='restaurant', resource_id=9, resource_type=12, name='餐厅',
                      display_total=2, url='https://you.ctrip.com/food/city1/9.html')
        with tempfile.TemporaryDirectory() as folder:
            # 1.1 构造升级前已达标报告及数据库，模拟旧版本无修订号。
            def old_collector(*args):
                r = collect_network(target, folder, 1, fetch=lambda _: {'code': 200, 'result': {'items': [{'commentId': 1}], 'totalCount': 2}})
                r.pop('restaurant_request_revision', None)
                return r
            collect_batch([target['url']], folder, 1, old_collector)
            target['poi_id'] = 108786
            calls = []
            def new_collector(*args):
                def fetch(index):
                    calls.append(index)
                    return {'code': 200, 'result': {'items': [{'commentId': 2}], 'totalCount': 2}}
                return collect_network(target, folder, 1, fetch=fetch)
            report = collect_batch([target['url']], folder, 1, new_collector)
            self.assertEqual(report['processed'], 1)
            self.assertEqual(calls, [1])
            self.assertEqual(report['results'][0]['count'], 2)
            self.assertEqual(collect_batch([target['url']], folder, 1, new_collector)['skipped'], 1)
        logger.info('升级续跑测试完成')

    def test_request_and_report_match_observed_route(self):
        # /* ===== 步骤1：核验餐饮请求；数据源：模拟公开响应；操作：1) 核对POI参数 2) 核对报告 ===== */
        logger.info('开始餐饮POI测试')
        target = dict(kind='restaurant', resource_id=9, resource_type=12,
                      poi_id=108786, name='餐厅', display_total=1, url='https://you.ctrip.com/food/city1/9.html')
        # 1.1 返回真实格式的最小响应，断言实际发出的请求。
        response = {'code': 200, 'result': {'items': [{'commentId': 1}], 'totalCount': 1}}
        with tempfile.TemporaryDirectory() as folder, patch('network.urlopen', return_value=io.BytesIO(json.dumps(response).encode())) as opened:
            report = collect_network(target, folder, limit=1)
            arg = json.loads(opened.call_args.args[0].data)['arg']
            self.assertEqual(arg, dict(poiId=108786, sortType=3, commentTagId=0, pageIndex=1, pageSize=10, sourceType=1))
            self.assertEqual(report['page_size'], 10)
            self.assertEqual(report['source_method'], 'public_restaurant_poi_api')
            self.assertEqual(report['count'], 1)
        logger.info('餐饮POI测试完成')

    def test_other_objects_and_invalid_poi_keep_resource_route(self):
        # /* ===== 步骤1：核对兼容范围；数据源：业务对象；操作：1) 排除其他业务 2) 拒绝无效POI切换 ===== */
        logger.info('开始旧通道范围测试')
        for kind, poi in [('sight', 123), ('shopping', 123), ('restaurant', None), ('restaurant', True), ('restaurant', -1), ('restaurant', '123')]:
            # 1.1 每种输入独立读取响应，核对业务ID仍被使用。
            with self.subTest(kind=kind, poi=poi), patch('network.urlopen', return_value=io.BytesIO(b'{}')) as opened:
                fetch_page(dict(kind=kind, poi_id=poi, resource_id=9, resource_type=12), 2)
                arg = json.loads(opened.call_args.args[0].data)['arg']
                self.assertEqual(arg['resourceId'], 9)
                self.assertEqual(arg['pageSize'], 20)
                self.assertNotIn('poiId', arg)
        logger.info('旧通道范围测试完成')
