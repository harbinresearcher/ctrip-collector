import json
import logging
import unittest

from network import parse_target

logger = logging.getLogger(__name__)


class RestaurantRouteTests(unittest.TestCase):
    def test_poi_maps_to_different_review_resource(self):
        # /* ===== 步骤1：核对餐饮身份映射；数据源：详情状态；操作：1) POI匹配 2) 保留业务ID ===== */
        logger.info('开始餐饮身份映射检查')
        state = {'poiId': 112271476, 'districtId': 2479,
                 'commentInfo': {'poiInfo': {'poiId': 112271476, 'resourceId': 25045127,
                                             'resourceType': 12, 'districtId': 2479, 'name': '餐厅'}},
                 'restaurantDetail': {'poiId': 112271476, 'name': '餐厅'}}
        html = '<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': {'initialState': state}}}) + '</script>'
        # 1.1 两套ID不能互换。
        target = parse_target('https://you.ctrip.com/yougourmet/restdetail/fusong2479/112271476.html', html)
        self.assertEqual(target['resource_id'], 25045127)
        self.assertEqual(target['poi_id'], 112271476)
        self.assertEqual(target['kind'], 'restaurant')
        with self.assertRaises(ValueError):
            parse_target('https://you.ctrip.com/yougourmet/restdetail/fusong2479/112271477.html', html)
        logger.info('餐饮身份映射检查完成')
