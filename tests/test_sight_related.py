"""景点页面推荐对象及来源状态。"""
import json
import logging
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import MagicMock, Mock, patch

from sight_related import discover_sight_related, extract_sight_related

logger = logging.getLogger(__name__)
URL = 'https://you.ctrip.com/sight/tianjin154/112796.html'
TIME = '2026-11-13T18:00:00+08:00'


def state():
    return {'poiCommentInfo': {'resourceId': 112796, 'poiId': 10559411, 'resourceType': 66},
            'poiDetail': {'ResponseStatus': {'Ack': 'Success', 'Errors': []}, 'result': 0,
                'businessId': 112796, 'poiId': 10559411, 'poiType': 66,
                'sightRecommend': {'itemList': [
                    {'poiId': 10559411, 'name': '自身', 'jumpUrl': URL},
                    {'poiId': 87777, 'name': '景点', 'score': '4.3',
                     'jumpUrl': 'https://you.ctrip.com/sight/tianjin154/64504.html?scene=1'}]},
                'restaurantRecommend': {'itemList': [
                    {'poiId': 222665, 'name': '餐厅', 'price': '¥15/人',
                     'jumpUrl': 'https://you.ctrip.com/food/0/187935.html'}]},
                'subModuleList': [
                    {'subType': 'nearbySight', 'itemList': [
                        {'poiId': 87777, 'name': '景点', 'distanceDesc': '3.3km',
                         'jumpUrl': 'https://you.ctrip.com/sight/tianjin154/64504.html?poiType=3'}]},
                    {'subType': 'nearbyRestaurant', 'itemList': [
                        {'poiId': 222665, 'name': '餐厅', 'distanceDesc': '7.1km',
                         'jumpUrl': 'https://you.ctrip.com/food/154/187935.html'}]},
                    {'subType': 'nearbyShop', 'itemList': [
                        {'poiId': 18179130, 'name': '商店', 'jumpUrl': 'https://you.ctrip.com/shopping/154/1373439.html'},
                        {'poiId': 999, 'name': '不可信', 'jumpUrl': 'https://example.com/shopping/154/1.html'}]}]}}


class SightRelatedTests(unittest.TestCase):
    def test_detail_and_comment_types_can_differ_when_identity_matches(self):
        # /* ===== 步骤0：核对不同类型命名空间；数据源：真实页面字段；操作：1) 核对对象 2) 保留原类型 ===== */
        logger.info('开始景点不同类型字段测试')
        sample = state()
        sample['poiDetail']['poiType'] = 3
        sample['poiCommentInfo']['resourceType'] = 11
        result = extract_sight_related(URL, sample, TIME)
        self.assertEqual(result['source']['poi_id'], 10559411)
        self.assertEqual(result['source']['detail_poi_type'], 3)
        self.assertEqual(result['source']['resource_type'], 11)
        logger.info('景点不同类型字段测试完成')

    def test_retains_three_categories_and_multiple_relationships(self):
        # /* ===== 步骤1：核验对象发现；数据源：合成景点页面；操作：1) 提取三类 2) 保留不同来源关系 ===== */
        logger.info('开始景点推荐提取测试')
        report = extract_sight_related(URL, state(), TIME)
        self.assertFalse(report['complete'])
        self.assertEqual(report['source']['resource_id'], 112796)
        self.assertEqual(report['source']['poi_id'], 10559411)
        self.assertEqual(report['recommendations_status'], 'available')
        objects = {(item['kind'], item['resource_id']): item for item in report['objects']}
        self.assertEqual(set(objects), {('sight', 64504), ('restaurant', 187935), ('shopping', 1373439)})
        self.assertEqual([x['relation'] for x in objects['sight', 64504]['observations']], ['sightRecommend', 'nearbySight'])
        self.assertEqual([x['relation'] for x in objects['restaurant', 187935]['observations']], ['restaurantRecommend', 'nearbyRestaurant'])
        self.assertEqual(objects['restaurant', 187935]['observations'][0]['price'], '¥15/人')
        self.assertEqual(objects['sight', 64504]['url'], 'https://you.ctrip.com/sight/tianjin154/64504.html')
        self.assertEqual(objects['sight', 64504]['identity_status'], 'page_link_only')
        self.assertEqual(report['skipped_invalid'], 1)
        self.assertEqual(report['skipped_self'], 1)
        self.assertNotIn('scene=', json.dumps(report))
        logger.info('景点推荐提取测试完成')

    def test_partial_lists_do_not_erase_available_objects(self):
        # /* ===== 步骤1：核验缺失；数据源：部分可用页面；操作：1) 标示缺口 2) 留存可用条目 ===== */
        logger.info('开始景点推荐缺口测试')
        sample = state()
        del sample['poiDetail']['restaurantRecommend']
        sample['poiDetail']['subModuleList'][1]['itemList'] = {}
        report = extract_sight_related(URL, sample, TIME)
        self.assertEqual(report['recommendations_status'], 'partial')
        self.assertEqual(report['list_status']['restaurantRecommend'], 'missing')
        self.assertEqual(report['list_status']['nearbyRestaurant'], 'invalid')
        self.assertIn(('sight', 64504), {(x['kind'], x['resource_id']) for x in report['objects']})
        logger.info('景点推荐缺口测试完成')

    def test_bad_optional_image_does_not_discard_candidate(self):
        # /* ===== 步骤1：核验图片字段；数据源：页面可选链接；操作：1) 拒绝异常地址 2) 保留对象 ===== */
        logger.info('开始景点推荐图片字段测试')
        sample = state()
        sample['poiDetail']['sightRecommend']['itemList'][1]['image'] = 'https://[broken'
        report = extract_sight_related(URL, sample, TIME)
        item = next(x for x in report['objects'] if x['resource_id'] == 64504)
        self.assertEqual(item['poi_id'], 87777)
        self.assertNotIn('image', item['observations'][0])
        logger.info('景点推荐图片字段测试完成')

    def test_rejects_identity_mismatch_and_unavailable_lists(self):
        # /* ===== 步骤1：核验身份；数据源：页面与链接；操作：1) 拒绝跨对象 2) 不把未知页面当空结果 ===== */
        logger.info('开始景点身份核验测试')
        for mutate in ('businessId', 'poiId'):
            sample = state()
            sample['poiDetail'][mutate] = 999
            with self.subTest(field=mutate), self.assertRaises(ValueError):
                extract_sight_related(URL, sample, TIME)
        for scope, key in (('poiDetail','poiType'), ('poiCommentInfo','resourceType')):
            sample = state()
            sample[scope][key] = 'not-an-int'
            with self.subTest(field=key), self.assertRaises(ValueError):
                extract_sight_related(URL, sample, TIME)
        sample = state()
        sample['poiDetail'] = {key: value for key, value in sample['poiDetail'].items()
                               if key not in ('sightRecommend', 'restaurantRecommend', 'subModuleList')}
        with self.assertRaises(ValueError):
            extract_sight_related(URL, sample, TIME)
        logger.info('景点身份核验测试完成')

    def test_conflicting_poi_does_not_silently_merge(self):
        # /* ===== 步骤1：核验重复对象；数据源：多个推荐列表；操作：1) 拒绝冲突编号 2) 留下异常计数 ===== */
        logger.info('开始景点重复关系测试')
        sample = deepcopy(state())
        sample['poiDetail']['subModuleList'][0]['itemList'][0]['poiId'] = 999
        report = extract_sight_related(URL, sample, TIME)
        item = next(x for x in report['objects'] if x['resource_id'] == 64504)
        self.assertEqual(item['poi_id'], 87777)
        self.assertEqual(len(item['observations']), 1)
        self.assertEqual(report['skipped_conflict'], 1)
        logger.info('景点重复关系测试完成')

    def test_fetch_saved_report_and_redirect_rejection(self):
        # /* ===== 步骤1：验证保存；数据源：受控网页；操作：1) 解析完整响应 2) 拒绝跨对象跳转 ===== */
        logger.info('开始景点推荐入口测试')
        body = ('<script id="__NEXT_DATA__" type="application/json">' +
                json.dumps({'props': {'pageProps': {'initialState': state()}}}) + '</script>').encode()
        response = MagicMock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = body
        response.geturl.return_value = URL
        opener = Mock()
        opener.open.return_value = response
        with TemporaryDirectory() as folder, patch('sight_related.build_opener', return_value=opener):
            report = discover_sight_related(URL, folder)
            self.assertEqual(report['reason'], 'recommendations_read')
            self.assertEqual(len(report['source_raw_hash']), 64)
            self.assertEqual(json.loads(Path(report['report_file']).read_text(encoding='utf-8')), report)
            response.geturl.return_value = 'https://you.ctrip.com/sight/tianjin154/64504.html'
            failed = discover_sight_related(URL, folder)
            self.assertEqual(failed['reason'], 'error')
            self.assertEqual(failed['objects'], [])
        logger.info('景点推荐入口测试完成')

    def test_cancel_before_request_and_during_read(self):
        # /* ===== 步骤1：验证取消；数据源：停止事件；操作：1) 请求前停止 2) 请求返回后停止 ===== */
        logger.info('开始景点推荐取消测试')
        with TemporaryDirectory() as folder:
            stop = Event()
            stop.set()
            with patch('sight_related.build_opener') as make_opener:
                result = discover_sight_related(URL, folder, stop)
                self.assertEqual(result['reason'], 'cancelled')
                make_opener.assert_not_called()
            stop.clear()
            response = MagicMock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.geturl.return_value = URL
            def read(_):
                stop.set()
                return b'invalid'
            response.read.side_effect = read
            opener = Mock()
            opener.open.return_value = response
            with patch('sight_related.build_opener', return_value=opener):
                result = discover_sight_related(URL, folder, stop)
                self.assertEqual(result['reason'], 'cancelled')
                self.assertEqual(result['objects'], [])
        logger.info('景点推荐取消测试完成')
