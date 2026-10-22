import json
import logging
import tempfile
import unittest
from pathlib import Path

from discovery import discover_sights, discover_restaurants

logger = logging.getLogger(__name__)


def fixture(page, cards, more=True):
    state = {'urlParams': {'page': page, 'districtId': '2479'},
             'listInitData': {'districtId': 2479, 'hasMore': more, 'totalCount': 3000,
                              'attractionList': [{'card': card} for card in cards]}}
    return '<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': {'initialState': state}}}) + '</script>'


def card(identity, city=2479):
    return {'businessId': identity, 'poiId': identity + 100, 'districtId': city,
            'poiName': '景点', 'commentCount': 5,
            'detailUrl': f'https://you.ctrip.com/sight/city{city}/{identity}.html?scene=online'}


class DiscoveryTests(unittest.TestCase):
    def test_restaurant_poi_directory_keeps_identity_and_page_guard(self):
        # /* ===== 步骤0：核对餐饮目录；数据源：结构化页面；操作：1) POI去重 2) 页码验证 ===== */
        logger.info('开始餐饮目录检查')
        def page(index):
            state = {'districtId': 2479, 'urlParam': {'p': str(index)},
                     'listPageData': {'ResponseStatus': {'Ack': 'Success', 'Errors': []}, 'total': 440, 'restaurants': [
                         {'poiId': 112271476, 'name': '餐厅', 'signature': 'discard'},
                         {'poiId': 112271476, 'name': '重复餐厅'}]}}
            return '<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': {'initialState': state}}}) + '</script>'
        with tempfile.TemporaryDirectory() as folder:
            # 0.1 POI不能冒充评论业务ID，目录保留独立标识。
            result = discover_restaurants('https://you.ctrip.com/yougourmet/restaurantlist/fusong2479.html',
                                          folder, max_pages=1, fetch=page, delay=0)
            self.assertEqual(len(result['objects']), 1)
            self.assertEqual(result['objects'][0]['poi_id'], 112271476)
            self.assertNotIn('resource_id', result['objects'][0])
            self.assertNotIn('signature', str(result['objects']))
            self.assertFalse(result['complete'])
            wrong = discover_restaurants('https://you.ctrip.com/yougourmet/restaurantlist/fusong2479.html',
                                         folder, fetch=lambda _: page(99), delay=0)
            self.assertEqual(wrong['reason'], 'error')
        logger.info('餐饮目录检查完成')

    def test_restaurant_failed_response_is_not_listing_end(self):
        # /* ===== 步骤1：核对失败响应；数据源：模拟接口错误；操作：1) 空列表 2) 禁止误报末页 ===== */
        logger.info('开始餐饮失败响应检查')
        state = {'districtId': 2479, 'urlParam': {'p': '1'},
                 'listPageData': {'ResponseStatus': {'Ack': 'Failure', 'Errors': [{'Message': 'Unavailable'}]},
                                  'restaurants': [], 'total': 0}}
        html = '<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': {'initialState': state}}}) + '</script>'
        with tempfile.TemporaryDirectory() as folder:
            # 1.1 错误不能当作正常数据耗尽。
            report = discover_restaurants('https://you.ctrip.com/yougourmet/restaurantlist/fusong2479.html',
                                          folder, fetch=lambda _: html, delay=0)
            self.assertEqual(report['reason'], 'error')
            self.assertTrue(report['error'])
            self.assertEqual(report['pages'], 0)
        logger.info('餐饮失败响应检查完成')

    def test_deduplicate_exclude_nearby_and_keep_limit(self):
        # /* ===== 步骤1：验证目录边界；数据源：分页列表；操作：1) 去重 2) 剔除邻城推荐 3) 限制对象数 ===== */
        logger.info('开始目录边界检查')
        pages = {1: fixture(1, [card(1), card(2, 643), card(1)]), 2: fixture(2, [card(3), card(4)])}
        with tempfile.TemporaryDirectory() as folder:
            # 1.1 实际清单不能超过用户给定对象预算。
            report = discover_sights('https://you.ctrip.com/sight/fusong2479/s0-p1.html', Path(folder),
                                     max_pages=5, max_objects=2, fetch=lambda p: pages[p], delay=0)
            self.assertEqual([x['resource_id'] for x in report['objects']], [1, 3])
            self.assertEqual(report['skipped_nearby'], 1)
            self.assertFalse(report['complete'])
            self.assertEqual(report['reason'], 'object_budget_reached')
            self.assertEqual(len(Path(report['links_file']).read_text('utf-8').splitlines()), 2)
        logger.info('目录边界检查完成')

    def test_wrong_page_and_repeated_pages_stop(self):
        # /* ===== 步骤2：验证重复响应；数据源：失效分页；操作：1) 拒绝页码错配 2) 重复页面中止 ===== */
        logger.info('开始分页真实性检查')
        with tempfile.TemporaryDirectory() as folder:
            # 2.1 即使 HTTP 请求成功，页码错误也必须报告。
            report = discover_sights('https://you.ctrip.com/sight/fusong2479/s0-p1.html', Path(folder),
                                     fetch=lambda _: fixture(99, [card(1)]), delay=0)
            self.assertEqual(report['reason'], 'error')
            self.assertEqual(report['objects'], [])
            report = discover_sights('https://you.ctrip.com/sight/fusong2479/s0-p1.html', Path(folder),
                                     fetch=lambda p: fixture(p, [card(1)]), delay=0)
            self.assertEqual(report['reason'], 'repeated_pages')
            self.assertEqual(report['pages'], 3)
        logger.info('分页真实性检查完成')
