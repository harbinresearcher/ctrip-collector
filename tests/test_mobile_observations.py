import hashlib
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mobile_observations import read_observations
from mobile_scoped_store import MobileScopedStore

logger = logging.getLogger(__name__)


class MobileObservationsTests(unittest.TestCase):
    def test_history_is_projected_without_personal_or_raw_fields(self):
        # /* ===== 步骤1：核验观察预览；数据源：真实存储；操作：1) 对比原文变化 2) 隐藏个人信息 ===== */
        logger.info('开始观察预览测试')
        row = {'author':'私密昵称','date':'2026','date_kind':'stay_month','rating':'3.0',
               'content':'正文','has_expand':False,'raw_texts':['私密昵称','正文']}
        with TemporaryDirectory() as folder:
            store = MobileScopedStore(folder, 'hotel', '123')
            try:
                # 1.1 评分变更但原文不变，再变更原文，返回前后比较结果。
                store.save('酒店', [row], 'private-first.xml')
                store.save('酒店', [{**row, 'rating':'5.0'}], 'private-next.xml')
                store.save('酒店', [{**row, 'rating':'5.0','raw_texts':['私密昵称','正文','展开']}], 'private-third.xml')
                key = store.db.execute('SELECT key FROM reviews_scoped').fetchone()[0]
            finally:
                store.close()
            # 1.2 另一个作用域的相似评论不能混入该条历史。
            other = MobileScopedStore(folder, 'hotel', '124')
            try:
                other.save('另一酒店', [{**row, 'rating':'1.0'}], 'other-private.xml')
            finally:
                other.close()
            before_hash = hashlib.sha256((Path(folder)/'reviews.sqlite3').read_bytes()).hexdigest()
            result = read_observations(folder, key)
            after_hash = hashlib.sha256((Path(folder)/'reviews.sqlite3').read_bytes()).hexdigest()
            self.assertEqual(before_hash, after_hash)
            self.assertEqual(result['history_status'], 'available')
            self.assertEqual([x['raw_texts_changed'] for x in result['items']], [True, False, None])
            self.assertEqual([x['score'] for x in result['items']], ['5.0','5.0','3.0'])
            self.assertTrue(all(x['captured_at'] for x in result['items']))
            self.assertFalse(result['has_more'])
            self.assertEqual(result['review_identity'], 'content_fingerprint_not_platform_id')
            self.assertEqual(result['object_identity'], 'operator_declared_unverified')
            self.assertNotIn('私密昵称', str(result))
            self.assertNotIn('private-', str(result))
            self.assertNotIn('raw_texts', result['items'][0])
        logger.info('观察预览测试完成')

    def test_keyset_pages_and_first_page_boundary_comparison(self):
        # /* ===== 步骤1：核验历史翻页；数据源：25个状态；操作：1) 游标不重叠 2) 跨页比较 ===== */
        logger.info('开始观察分页测试')
        row={'author':'a','date':'2026','date_kind':'stay_month','rating':'0',
             'content':'正文','has_expand':False,'raw_texts':['initial']}
        with TemporaryDirectory() as folder:
            store=MobileScopedStore(folder,'hotel','456')
            try:
                for i in range(25):
                    # 1.1 第5个状态变化原文；它恰好位于第一页的最后一条。
                    current={**row,'rating':str(i),'raw_texts':['changed' if i>=5 else 'initial']}
                    store.save('酒店',[current],f'{i}.xml')
                key=store.db.execute('SELECT key FROM reviews_scoped').fetchone()[0]
            finally:
                store.close()
            first=read_observations(folder,key)
            self.assertEqual(len(first['items']),20)
            self.assertTrue(first['has_more'])
            self.assertTrue(first['items'][-1]['raw_texts_changed'])
            second=read_observations(folder,key,before=first['next_cursor'])
            self.assertEqual(len(second['items']),5)
            self.assertFalse(second['has_more'])
            self.assertIsNone(second['next_cursor'])
            self.assertEqual([i['score'] for i in first['items']]+[i['score'] for i in second['items']], [str(i) for i in range(24,-1,-1)])
            empty=read_observations(folder,key,before=1)
            self.assertEqual(empty['items'],[])
            self.assertEqual(empty['history_status'],'available')
        logger.info('观察分页测试完成')

    def test_missing_or_invalid_inputs_are_explicit(self):
        # /* ===== 步骤1：核验异常边界；数据源：空目录和旧库；操作：1) 拒绝非法参数 2) 区分缺失与无数据 ===== */
        logger.info('开始观察异常测试')
        key='a'*64
        with TemporaryDirectory() as folder:
            # 1.1 无数据库不能静默建库；错误参数在访问前拒绝。
            for bad in ('A'*64,'a'*63,'../secret',None,123):
                with self.subTest(key=bad), self.assertRaises(ValueError):
                    read_observations(folder,bad)
            for bad in (0,-1,True,'1',2**63):
                with self.subTest(before=bad), self.assertRaises(ValueError):
                    read_observations(folder,key,before=bad)
            with self.assertRaises(FileNotFoundError):
                read_observations(folder,key)
            self.assertFalse((Path(folder)/'reviews.sqlite3').exists())
            with closing(sqlite3.connect(Path(folder)/'reviews.sqlite3')) as db:
                db.execute('CREATE TABLE unrelated (id INTEGER)')
                db.commit()
            with self.assertRaisesRegex(ValueError, '作用域评论表不存在'):
                read_observations(folder,key)
            with closing(sqlite3.connect(Path(folder)/'reviews.sqlite3')) as db:
                db.execute('CREATE TABLE reviews_scoped (key TEXT PRIMARY KEY)')
                db.execute('INSERT INTO reviews_scoped VALUES (?)',(key,))
                db.commit()
            self.assertEqual(read_observations(folder,key)['history_status'],'unavailable')
            with self.assertRaises(LookupError):
                read_observations(folder,'b'*64)
            with closing(sqlite3.connect(Path(folder)/'reviews.sqlite3')) as db:
                db.execute('CREATE TABLE reviews_scoped_observations (observation_id INTEGER PRIMARY KEY,review_key TEXT,rating TEXT,has_expand INTEGER,raw_texts TEXT,source TEXT,captured_at TEXT)')
                db.commit()
            self.assertEqual(read_observations(folder,key)['history_status'],'not_recorded')
        logger.info('观察异常测试完成')
