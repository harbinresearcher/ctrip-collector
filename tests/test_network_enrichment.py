"""重复评论的字段补全和来源观察。"""
import json
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path

from network import collect_network

logger = logging.getLogger(__name__)


def page(items):
    return {'code': 200, 'result': {'items': items, 'totalCount': 2}}


class NetworkEnrichmentTests(unittest.TestCase):
    def test_cross_object_response_does_not_merge_or_partially_write(self):
        # /* ===== 步骤1：验证对象身份；数据源：重复评论页；操作：1) 写入旧数据 2) 拒绝其他对象的评论 ===== */
        logger.info('开始跨对象重复评论测试')
        target = {'kind': 'sight', 'resource_id': 12, 'resource_type': 11,
                  'name': '景区', 'display_total': 2, 'url': 'https://you.ctrip.com/sight/city1/12.html'}
        for wrong_id in (99, '99'):
            with self.subTest(resource_id=wrong_id), tempfile.TemporaryDirectory() as folder:
                first = {'commentId': 1, 'resourceId': 12, 'content': '已有评论'}
                collect_network(target, folder, limit=2, max_pages=1, fetch=lambda _: page([first]), delay=0)
                # 1.1 一页中先有当前对象评论，后有同 ID 但属于其他对象的评论。
                items = [{'commentId': 2, 'resourceId': 12, 'content': '本对象新评论'},
                         {'commentId': 1, 'resourceId': wrong_id, 'content': '已有评论被污染'}]
                result = collect_network(target, folder, limit=2, max_pages=1, fetch=lambda _: page(items), delay=0)
                self.assertEqual(result['reason'], 'error')
                self.assertIn('对象', result['error'])
                self.assertEqual(result['count'], 1)
                records = [json.loads(line)['data'] for line in Path(result['jsonl']).read_text(encoding='utf-8').splitlines()]
                self.assertEqual(records, [first])
                observed = [json.loads(line) for line in Path(result['observations_jsonl']).read_text(encoding='utf-8').splitlines()]
                self.assertEqual(len(observed), 1)
        logger.info('跨对象重复评论测试完成')

    def test_later_response_preserves_original_and_adds_new_fields(self):
        # /* ===== 步骤1：检查重复评论补全；数据源：公开样本和完整响应；操作：1) 保存两版 2) 检查导出 ===== */
        logger.info('开始评论补全测试')
        target = {'kind': 'hotel', 'resource_id': 12, 'name': '酒店', 'display_total': 2,
                  'url': 'https://hotels.ctrip.com/hotels/12.html', 'source_method': 'hotel_public_html_snapshot'}
        with tempfile.TemporaryDirectory() as folder:
            # 1.1 同一服务端 ID 的后续响应新增正文与图片，不能因为去重而丢弃。
            first = {'commentId': 1, 'content': '好', 'images': [{'url': 'https://example.com/0.jpg'}], 'source_schema': 'hotel_public_html'}
            collect_network(target, folder, limit=5, fetch=lambda _: page([first]), delay=0)
            target['source_method'] = 'hotel_browser_response'
            later = {'commentId': 1, 'content': '好住', 'images': [{'url': 'https://example.com/0.jpg'}, {'url': 'https://example.com/1.jpg'}],
                     'replyInfo': {'content': '谢谢'}, 'source_schema': 'hotel_browser_response'}
            result = collect_network(target, folder, limit=2,
                                     fetch=lambda _: page([later, {'commentId': 2, 'content': '第二条'}]),
                                     delay=0)
            self.assertEqual((result['count'], result['new_count']), (2, 1))
            records = [json.loads(line)['data'] for line in Path(result['jsonl']).read_text(encoding='utf-8').splitlines()]
            merged = next(row for row in records if row['commentId'] == 1)
            self.assertEqual(merged['content'], '好住')
            self.assertEqual(merged['images'], later['images'])
            self.assertEqual(merged['replyInfo'], later['replyInfo'])
            self.assertTrue(merged['_collector_field_merge'])
            # 1.2 旧值与后续响应均可追溯，不能将混合字段误称单次原始响应。
            observations = [json.loads(line) for line in Path(result['observations_jsonl']).read_text(encoding='utf-8').splitlines()]
            same = [row for row in observations if row['comment_id'] == '1']
            self.assertEqual({row['data']['content'] for row in same}, {'好', '好住'})
            self.assertEqual({row['source_method'] for row in same}, {'hotel_public_html_snapshot', 'hotel_browser_response'})
        logger.info('评论补全测试完成')

    def test_existing_database_is_migrated_without_losing_first_value(self):
        # /* ===== 步骤1：兼容旧库；数据源：已有评论；操作：1) 保留旧值 2) 记录新观察 ===== */
        logger.info('开始旧评论数据库迁移测试')
        target = {'kind': 'sight', 'resource_id': 12, 'resource_type': 11, 'name': '景区',
                  'display_total': 2, 'url': 'https://you.ctrip.com/sight/city1/12.html'}
        with tempfile.TemporaryDirectory() as folder:
            with sqlite3.connect(Path(folder) / 'network.sqlite3') as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(object_key,comment_id))')
                db.execute('INSERT INTO network_reviews VALUES (?,?,?,?)',
                           ('sight-12', '1', json.dumps({'commentId': 1, 'content': '旧值'}), '2026-01-01T00:00:00+00:00'))
            db.close()
            # 1.1 冲突文本保留首条主值，独立观察保存后续文本。
            result = collect_network(target, folder, limit=2,
                                     fetch=lambda _: page([{'commentId': 1, 'content': '新值'}, {'commentId': 2}]), delay=0)
            observations = [json.loads(line) for line in Path(result['observations_jsonl']).read_text(encoding='utf-8').splitlines()]
            same = [row for row in observations if row['comment_id'] == '1']
            self.assertEqual({row['data']['content'] for row in same}, {'旧值', '新值'})
            self.assertIn('historical_unknown', {row['source_method'] for row in same})
            main = [json.loads(line)['data'] for line in Path(result['jsonl']).read_text(encoding='utf-8').splitlines()]
            self.assertEqual(next(row for row in main if row['commentId'] == 1)['content'], '旧值')
        logger.info('旧评论数据库迁移测试完成')

    def test_identical_repeated_pages_do_not_duplicate_observations(self):
        # /* ===== 步骤1：检查观察去重；数据源：同一公开评论页；操作：1) 重复采集 2) 验证记录数 ===== */
        logger.info('开始重复观察测试')
        target = {'kind': 'restaurant', 'resource_id': 12, 'resource_type': 12, 'poi_id': 99,
                  'name': '餐厅', 'display_total': 2, 'url': 'https://you.ctrip.com/food/city1/12.html'}
        with tempfile.TemporaryDirectory() as folder:
            rows = [{'commentId': 1, 'content': '文本'}, {'commentId': 2, 'content': '内容'}]
            for _ in range(2):
                # 1.1 餐饮通道每次重读页面，同源同内容只留一条观察。
                report = collect_network(target, folder, limit=2, fetch=lambda _: page(rows), delay=0)
                self.assertEqual(report['count'], 2)
            with sqlite3.connect(Path(folder) / 'network.sqlite3') as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM network_review_observations').fetchone()[0], 2)
            db.close()
        logger.info('重复观察测试完成')
