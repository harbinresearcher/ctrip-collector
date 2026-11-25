import json
import logging
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from workbench_preview import read_preview

logger = logging.getLogger(__name__)


class PreviewTests(unittest.TestCase):
    def test_preview_identifies_first_saved_screen_without_returning_untrusted_paths(self):
        from mobile_scoped_store import MobileScopedStore
        from workbench_preview import read_scoped_mobile_preview
        with TemporaryDirectory() as folder:
            first = {'author': 'a', 'date': '2026', 'date_kind': 'published', 'rating': '5',
                     'content': '第一条', 'has_expand': False, 'raw_texts': []}
            store = MobileScopedStore(folder, 'sight', '123')
            try:
                store.save('景点', [first], r'raw\20261119T080000-123abc\00002.xml')
                store.save('景点', [{**first, 'rating': '4'}], 'raw/20261119T083000-abc123/00000.xml')
                store.save('景点', [{**first, 'content': '无效路径'}],
                           'raw/20261119T080000-123abc/../private.xml')
            finally:
                store.close()
            result = read_scoped_mobile_preview(folder)
            by_content = {item['content']: item for item in result['items']}
            self.assertEqual(by_content['第一条']['first_saved_screen'],
                             {'run_id': '20261119T080000-123abc', 'screen': 3})
            self.assertNotIn('first_saved_screen', by_content['无效路径'])
            self.assertNotIn('private.xml', json.dumps(result))
            self.assertNotIn('source', by_content['第一条'])

    def test_scoped_preview_uses_separate_table_and_declared_id(self):
        # /* ===== 步骤1：核验ID作用域；数据源：新手机库；操作：1) 独立显示 2) 保留声明边界 ===== */
        logger.info('开始作用域预览测试')
        from mobile_scoped_store import MobileScopedStore
        from workbench_preview import read_scoped_mobile_preview
        with TemporaryDirectory() as folder:
            self.assertEqual(read_scoped_mobile_preview(folder)['items'], [])
            store = MobileScopedStore(folder, 'sight', '123')
            # 1.1 新库可以没有旧表，仍须预览新记录。
            store.save('同名景点', [{'author':'a','date':'2026','date_kind':'published','rating':'5','content':'测试正文','has_expand':False,'raw_texts':[]}], 'fake.xml')
            store.close()
            result = read_scoped_mobile_preview(folder)
            self.assertEqual(len(result['items']), 1)
            self.assertEqual(result['items'][0]['object_id'], '123')
            self.assertRegex(result['items'][0]['local_key'], r'^[0-9a-f]{64}$')
            self.assertEqual(result['items'][0]['identity_verification'], 'operator_declared_unverified')
            self.assertNotIn('author', result['items'][0])
        logger.info('作用域预览测试完成')

    def test_mobile_preview_is_separate_and_does_not_create_database(self):
        # /* ===== 步骤1：核验手机预览；数据源：独立评论库；操作：1) 只读 2) 分页并标记身份未核验 ===== */
        logger.info('开始手机评论预览测试')
        from workbench_preview import read_mobile_preview
        from core import Store
        with TemporaryDirectory() as folder:
            # 1.1 空目录不得自动创建数据库。
            self.assertEqual(read_mobile_preview(folder)['items'], [])
            self.assertFalse((Path(folder) / 'reviews.sqlite3').exists())
            store = Store(folder)
            rows = [{'author':str(i), 'date':'2026-01-01', 'date_kind':'published', 'rating':'5', 'content':f'评论{i}', 'has_expand':False, 'raw_texts':[]} for i in range(25)]
            store.save('hotel', '测试酒店', rows, 'raw/test.xml')
            store.close()
            first = read_mobile_preview(folder)
            second = read_mobile_preview(folder, before=first['next_cursor'])
            self.assertEqual((len(first['items']), len(second['items'])), (20, 5))
            self.assertEqual(first['items'][0]['identity_verification'], 'operator_declared_unverified')
            self.assertNotIn('author', first['items'][0])
            self.assertNotIn('comment_id', first['items'][0])
            self.assertEqual(first['items'][0]['place'], '测试酒店')
        logger.info('手机评论预览测试完成')

    def test_readonly_bounded_preview(self):
        # /* ===== 步骤1：核对本地预览；数据源：临时数据库；操作：1) 空目录不建库 2) 限量读取 ===== */
        logger.info('开始预览测试')
        with TemporaryDirectory() as folder:
            self.assertEqual(read_preview(folder)['items'], [])
            self.assertFalse((Path(folder)/'network.sqlite3').exists())
            db = sqlite3.connect(Path(folder)/'network.sqlite3')
            db.execute('CREATE TABLE network_reviews(object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
            db.executemany('INSERT INTO network_reviews VALUES(?,?,?,?)', [('sight-1',str(i),json.dumps({'content':'正文','token':'secret','score':4}),'2026-11-10') for i in range(25)])
            db.commit()
            db.close()
            # 1.1 不将原始响应中凭据或其他未知字段传给界面。
            result = read_preview(folder)
            self.assertEqual(len(result['items']), 20)
            self.assertTrue(result['has_more'])
            second = read_preview(folder, before=result['next_cursor'])
            self.assertEqual(len(second['items']), 5)
            self.assertFalse(second['has_more'])
            self.assertFalse({i['comment_id'] for i in result['items']} & {i['comment_id'] for i in second['items']})
            with self.assertRaises(ValueError):
                read_preview(folder, before=-1)
            self.assertNotIn('secret', json.dumps(result))
        logger.info('预览测试完成')
