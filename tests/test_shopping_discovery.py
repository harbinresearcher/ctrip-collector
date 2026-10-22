import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from shopping_discovery import discover_shopping

logger = logging.getLogger(__name__)


class ShoppingDiscoveryTests(unittest.TestCase):
    def test_filter_dedupe_and_bound(self):
        # /* ===== 步骤1：核对购物目录；数据源：模拟分页；操作：1) 过滤广告和邻城 2) 业务ID去重 ===== */
        logger.info('开始购物目录边界检查')
        def fetch(page):
            return {'ResponseStatus': {'Ack': 'Success', 'Errors': []}, 'total': 100,
                    'shoppingPoiList': [
                        {'businessId': 1, 'poiId': 101, 'districtId': 2, 'chineseName': '店铺'},
                        {'businessId': 2, 'poiId': 102, 'districtId': 1},
                        {'businessId': 3, 'poiId': 103, 'districtId': 2, 'isAdvertiesment': True}]}
        with TemporaryDirectory() as folder:
            result = discover_shopping(2, folder, max_pages=3, fetch=fetch, delay=0)
            self.assertEqual([r['resource_id'] for r in result['objects']], [1])
            self.assertEqual(result['objects'][0]['poi_id'], 101)
            self.assertEqual(result['reason'], 'repeated_pages')
            self.assertFalse(result['complete'])
            self.assertFalse(result['server_pages_verified'])
            self.assertEqual(len(Path(result['links_file']).read_text('utf-8').splitlines()), 1)
        logger.info('购物目录边界检查完成')

    def test_failure_is_not_empty_end(self):
        # /* ===== 步骤2：核对接口错误；数据源：失败空响应；操作：1) 状态验证 2) 禁止误报末页 ===== */
        logger.info('开始购物接口错误检查')
        with TemporaryDirectory() as folder:
            result = discover_shopping(2, folder, fetch=lambda _: {
                'ResponseStatus': {'Ack': 'Failure', 'Errors': ['blocked']}, 'shoppingPoiList': []}, delay=0)
            self.assertEqual(result['reason'], 'error')
            self.assertEqual(result['pages'], 0)
        logger.info('购物接口错误检查完成')
