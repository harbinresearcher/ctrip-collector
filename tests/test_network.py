import json
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from network import collect_network, parse_target, sanitize, shopping_target

logger = logging.getLogger(__name__)


def page(items, total=3):
    return {'code': 200, 'result': {'items': items, 'totalCount': total,
                                  'totalHideCount': 8, 'totalCollapseCount': 2}}


class NetworkTests(unittest.TestCase):
    def test_zero_review_shop_without_entry_is_explicit_snapshot(self):
        # /* ===== 步骤0：核对零点评商铺；数据源：成功详情响应；操作：1) 明确零数量 2) 不猜分页类型 ===== */
        logger.info('开始零点评商铺检查')
        overview = {'ResultCode': 0, 'PoiOverviewInfo': {'BusinessId': 15, 'PoiId': 99, 'PoiName': '商铺'}}
        comments = {'ResultCode': 0, 'ResponseStatus': {'Ack': 'Success', 'Errors': []},
                    'commentDetail': {'totalCount': 0, 'commentList': None, 'moreUrl': None}}
        target = shopping_target('https://you.ctrip.com/shopping/city2/15.html', overview, comments)
        self.assertIsNone(target['resource_type'])
        with tempfile.TemporaryDirectory() as folder, patch('network.urlopen', side_effect=AssertionError('不应猜测评论请求')):
            result = collect_network(target, folder, 3)
            self.assertEqual(result['count'], 0)
            self.assertEqual(result['reason'], 'public_snapshot_only')
        comments['commentDetail']['totalCount'] = 2
        with self.assertRaises(ValueError):
            shopping_target('https://you.ctrip.com/shopping/city2/15.html', overview, comments)
        logger.info('零点评商铺检查完成')

    def test_cumulative_limit_and_locked_export_preserve_previous_files(self):
        # /* ===== 步骤0：核对续采与导出；数据源：已有数据库；操作：1) 不超过累计目标 2) 失败保留旧文件 ===== */
        logger.info('开始续采边界检查')
        target = {'kind': 'sight', 'resource_id': 12, 'resource_type': 11, 'poi_id': 99,
                  'name': '景区', 'display_total': 11, 'details': {}, 'url': 'https://you.ctrip.com/sight/city1/12.html'}
        with tempfile.TemporaryDirectory() as folder:
            result = collect_network(target, Path(folder), 2, fetch=lambda _: page([{'commentId': 1}, {'commentId': 2}]), delay=0)
            old = Path(result['jsonl']).read_bytes()
            again = collect_network(target, Path(folder), 2, fetch=lambda _: self.fail('已有目标数量不应请求'), delay=0)
            self.assertEqual(again['count'], 2)
            self.assertEqual(again['error'], '')
            original_open = Path.open
            def locked(path, *args, **kwargs):
                if '.csv' in str(path) and args and args[0] == 'w':
                    raise PermissionError('Excel lock')
                return original_open(path, *args, **kwargs)
            # 0.1 模拟第二个导出文件打开失败。
            with patch.object(Path, 'open', locked):
                collect_network(target, Path(folder), 2, fetch=lambda _: page([]), delay=0)
            self.assertEqual(Path(result['jsonl']).read_bytes(), old)
        logger.info('续采边界检查完成')

    def test_shopping_identity_from_official_response(self):
        # /* ===== 步骤0：核对购物映射；数据源：公开响应；操作：1) 验证点评链接身份 ===== */
        logger.info('开始购物身份检查')
        overview = {'ResultCode': 0, 'PoiOverviewInfo': {'BusinessId': 5, 'PoiId': 9,
                    'PoiName': '商场', 'CommentCount': 123, 'PoiAddress': '地址'}}
        detail = {'commentDetail': {'moreUrl': 'https://m.ctrip.com/webapp/you/commentWeb/commentList?businessId=5&businessType=13'}}
        target = shopping_target('https://gs.ctrip.com/html5/you/shops/1/5.html', overview, detail)
        self.assertEqual((target['resource_id'], target['resource_type'], target['poi_id']), (5, 13, 9))
        self.assertEqual(target['display_total'], 123)
        logger.info('购物身份检查完成')

    def test_details_and_resource_identity(self):
        # /* ===== 步骤1：核对对象身份；数据源：页面状态；操作：1) 区分 POI 与业务 ID ===== */
        logger.info('开始核对身份')
        state = {'poiDetail': {'poiName': '景区', 'poiId': 99, 'businessId': 12,
                              'commentCount': 11, 'introduction': '完整介绍'},
                 'poiCommentInfo': {'resourceId': 12, 'resourceType': 11, 'poiId': 99}}
        html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(
            {'props': {'pageProps': {'initialState': state}}}) + '</script>'
        # 1.1 业务 ID 用于请求，POI ID 另外保留。
        target = parse_target('https://you.ctrip.com/sight/city1/12.html', html)
        self.assertEqual((target['resource_id'], target['poi_id']), (12, 99))
        self.assertEqual(target['details']['introduction'], '完整介绍')
        with self.assertRaises(ValueError):
            parse_target('https://example.com/sight/city1/12.html', html)
        logger.info('身份核对完成')

    def test_private_keys_removed_but_public_fields_kept(self):
        # /* ===== 步骤2：验证字段留存；数据源：接口样本；操作：1) 移除凭据 2) 保留媒体及回复 ===== */
        logger.info('开始字段检查')
        # 2.1 递归处理用户、回复和媒体对象。
        data = sanitize({'content': '正文', 'userInfo': {'userNick': '昵称', 'clientAuth': 'secret'},
                         'images': [{'url': 'https://example.com/a.jpg'}], 'ip': '1.2.3.4',
                         'replyInfo': {'content': '回复'}, 'ipLocatedName': '吉林'})
        self.assertNotIn('clientAuth', data['userInfo'])
        self.assertNotIn('ip', data)
        self.assertEqual(data['replyInfo']['content'], '回复')
        self.assertEqual(data['ipLocatedName'], '吉林')
        logger.info('字段检查完成')

    def test_pagination_resume_and_truthful_counts(self):
        # /* ===== 步骤3：验证分页与续采；数据源：可重复分页；操作：1) 稳定 ID 去重 2) 不误报全量 ===== */
        logger.info('开始分页检查')
        target = {'kind': 'sight', 'resource_id': 12, 'resource_type': 11, 'poi_id': 99,
                  'name': '景区', 'display_total': 11, 'details': {}, 'url': 'https://you.ctrip.com/sight/city1/12.html'}
        responses = [page([{'commentId': 1, 'content': '=正文', 'images': [{'url': 'a'}]},
                          {'commentId': 2, 'content': '长文', 'originContent': 'original'}]),
                     page([{'commentId': 2, 'content': '长文'}, {'commentId': 3, 'content': '末条'}]), page(None)]
        def fetch(index):
            return responses[min(index - 1, 2)]
        with tempfile.TemporaryDirectory() as folder:
            # 3.1 重复运行后仍只有三个稳定 ID。
            result = collect_network(target, Path(folder), 500, fetch=fetch, delay=0)
            repeated = collect_network(target, Path(folder), 500, fetch=fetch, delay=0)
            self.assertEqual(result['count'], 3)
            self.assertEqual(repeated['new_count'], 0)
            self.assertEqual(result['reason'], 'empty_page')
            self.assertFalse(result['complete'])
            self.assertEqual(result['display_total'], 11)
            self.assertEqual(result['server_counts']['totalHideCount'], 8)
            rows = [json.loads(line) for line in Path(result['jsonl']).read_text('utf-8').splitlines()]
            self.assertEqual(len(rows), 3)
            self.assertTrue(any(row['data'].get('images') for row in rows))
            self.assertIn("'=正文", Path(result['csv']).read_text('utf-8-sig'))
        logger.info('分页检查完成')

    def test_block_and_cancel_are_not_empty_success(self):
        # /* ===== 步骤4：验证中止；数据源：拦截码及停止事件；操作：1) 停止请求 2) 保存报告 ===== */
        logger.info('开始中止检查')
        target = {'kind': 'restaurant', 'resource_id': 1, 'resource_type': 12, 'poi_id': 2,
                  'name': '餐馆', 'display_total': 20, 'details': {}, 'url': 'https://you.ctrip.com/food/a1/1.html'}
        with tempfile.TemporaryDirectory() as folder:
            # 4.1 HTTP 200 的拦截体必须作为错误。
            result = collect_network(target, Path(folder), 500,
                                     fetch=lambda _: {'data': {'htlSpiderActionErrorCode': 203}}, delay=0)
            self.assertEqual(result['reason'], 'error')
            self.assertTrue(result['error'])
            stop = Event()
            stop.set()
            result = collect_network(target, Path(folder), 500, stop=stop,
                                     fetch=lambda _: self.fail('取消后不应请求'), delay=0)
            self.assertEqual(result['reason'], 'cancelled')
            # 4.2 请求过程中取消也不能记成 empty_page。
            stop.clear()
            def cancel_during_fetch(index):
                stop.set()
                return page([])
            result = collect_network(target, Path(folder), 500, stop=stop,
                                     fetch=cancel_during_fetch, delay=0)
            self.assertEqual(result['reason'], 'cancelled')
            self.assertEqual(result['pages'], 0)
        logger.info('中止检查完成')


if __name__ == '__main__':
    unittest.main()
