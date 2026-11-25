import csv
import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from core import Store
from mobile_scoped_store import MobileScopedStore

logger = logging.getLogger(__name__)


class MobileScopedStoreTests(unittest.TestCase):
    def test_same_fingerprint_preserves_distinct_observation_states(self):
        # /* ===== 步骤1：核验观察历史；数据源：同指纹多屏；操作：1) 保留变化 2) 去除完全重复状态 ===== */
        logger.info('开始观察历史测试')
        row = {'author':'甲','date':'2026','date_kind':'stay_month','rating':'3.0',
               'content':'相同正文','has_expand':False,'raw_texts':['相同正文']}
        with TemporaryDirectory() as folder:
            store = MobileScopedStore(folder, 'hotel', '123')
            try:
                # 1.1 首次记录初值；只有来源变化不产生新状态。
                self.assertEqual(store.save('酒店', [row], 'first.xml'), 1)
                self.assertEqual(store.save('酒店', [row], 'duplicate.xml'), 0)
                changed = {**row, 'rating':'5.0', 'has_expand':True}
                self.assertEqual(store.save('酒店', [changed], 'changed.xml'), 0)
                raw_changed = {**changed, 'raw_texts':['相同正文','其他可见信息']}
                self.assertEqual(store.save('酒店', [raw_changed], 'raw.xml'), 0)
                # 1.2 恢复原状态也是实际发生过的变化，保留对应来源。
                self.assertEqual(store.save('酒店', [row], 'reverted.xml'), 0)
                self.assertEqual(store.count(), 1)
                with closing(sqlite3.connect(Path(folder)/'reviews.sqlite3')) as db:
                    history = db.execute('SELECT rating,has_expand,raw_texts,source,captured_at FROM reviews_scoped_observations ORDER BY observation_id').fetchall()
                self.assertEqual(len(history), 4)
                self.assertEqual([(v[0],v[1],v[3]) for v in history],
                                 [('3.0',0,'first.xml'),('5.0',1,'changed.xml'),('5.0',1,'raw.xml'),('3.0',0,'reverted.xml')])
                self.assertNotEqual(history[1][2], history[2][2])
                self.assertTrue(all(v[4] for v in history))
                with store.export().open(encoding='utf-8-sig', newline='') as stream:
                    exported = list(csv.DictReader(stream))
                self.assertEqual(len(exported), 1)
                self.assertEqual(exported[0]['rating'], '3.0')
            finally:
                store.close()
        logger.info('观察历史测试完成')

    def test_existing_scoped_record_is_backfilled_on_next_sighting(self):
        # /* ===== 步骤1：核验历史作用域迁移；数据源：旧版新表；操作：1) 保留旧状态 2) 记录后续变化 ===== */
        logger.info('开始历史作用域观察测试')
        row = {'author':'甲','date':'2026','date_kind':'stay_month','rating':'3.0',
               'content':'正文','has_expand':False,'raw_texts':['正文']}
        scope = 'declared:ctrip:hotel:123'
        key = hashlib.sha256(json.dumps(['scoped-v1','hotel',scope,'甲','2026','正文'],ensure_ascii=False).encode('utf-8')).hexdigest()
        with TemporaryDirectory() as folder:
            # 1.1 模拟升级前只有reviews_scoped、没有观察表的真实结构。
            with closing(sqlite3.connect(Path(folder)/'reviews.sqlite3')) as db:
                db.execute('CREATE TABLE reviews_scoped (key TEXT PRIMARY KEY,kind TEXT NOT NULL,scope TEXT NOT NULL,object_id TEXT NOT NULL,identity_source TEXT NOT NULL,place TEXT NOT NULL,author TEXT,date TEXT,date_kind TEXT,rating TEXT,content TEXT,has_expand INTEGER,raw_texts TEXT,source TEXT,captured_at TEXT)')
                db.execute('INSERT INTO reviews_scoped VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (key,'hotel',scope,'123','operator_declared_unverified','酒店','甲','2026','stay_month','3.0','正文',0,json.dumps(['正文'],ensure_ascii=False),'old.xml','2026-01-01T00:00:00+00:00'))
                db.commit()
            store = MobileScopedStore(folder, 'hotel', '123')
            try:
                self.assertEqual(store.save('酒店',[{**row,'rating':'5.0'}],'new.xml'),0)
                self.assertEqual(store.count(),1)
                with closing(sqlite3.connect(Path(folder)/'reviews.sqlite3')) as db:
                    history=db.execute('SELECT rating,source,captured_at FROM reviews_scoped_observations ORDER BY observation_id').fetchall()
                self.assertEqual(len(history),2)
                self.assertEqual(history[0],('3.0','old.xml','2026-01-01T00:00:00+00:00'))
                self.assertEqual(history[1][:2],('5.0','new.xml'))
            finally:
                store.close()
        logger.info('历史作用域观察测试完成')
    def test_page_write_rolls_back_and_scope_survives_restart(self):
        # /* ===== 步骤1：核对事务与续跑；数据源：一页评论；操作：1) 错误整页回滚 2) 重开继续去重 ===== */
        logger.info('开始作用域事务续跑测试')
        row = {'author': '甲', 'date': '2026', 'date_kind': 'stay_month', 'rating': '5.0',
               'content': '真实正文', 'has_expand': False, 'raw_texts': ['真实正文']}
        with TemporaryDirectory() as folder:
            store = MobileScopedStore(folder, 'hotel', '123')
            try:
                # 1.1 同一页后续记录无字段，前面有效记录也不能被半页提交。
                with self.assertRaises(KeyError):
                    store.save('酒店', [row, {'author': '乙'}], 'one.xml')
                self.assertEqual(store.count(), 0)
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM reviews_scoped_observations').fetchone()[0], 0)
                self.assertEqual(store.save('酒店', [row], 'one.xml'), 1)
            finally:
                store.close()
            restarted = MobileScopedStore(folder, 'hotel', '123')
            try:
                self.assertEqual(restarted.count(), 1)
                self.assertEqual(restarted.save('酒店新名', [row], 'two.xml'), 0)
            finally:
                restarted.close()
        logger.info('作用域事务续跑测试完成')

    def test_existing_database_and_export_remain_compatible(self):
        # /* ===== 步骤1：核对旧数据；数据源：旧版存储；操作：1) 保留旧表和文件 2) 验证旧版仍可写入 ===== */
        logger.info('开始旧版兼容测试')
        row = {'author': '甲', 'date': '2026年01月入住', 'date_kind': 'stay_month',
               'rating': '5.0', 'content': '旧评论', 'has_expand': False, 'raw_texts': ['旧评论']}
        with TemporaryDirectory() as folder:
            legacy = Store(folder)
            try:
                legacy.save('hotel', '同名酒店', [row], 'old.xml')
                old_csv = legacy.export('hotel', '同名酒店')
                original = old_csv.read_bytes()
            finally:
                legacy.close()
            # 1.1 新作用域初始为空，不能让旧记录直接触发目标达成。
            scoped = MobileScopedStore(folder, 'hotel', '100')
            try:
                self.assertEqual(scoped.count(), 0)
                self.assertEqual(scoped.save('同名酒店', [row], 'new.xml'), 1)
                new_csv = scoped.export()
                self.assertNotEqual(new_csv, old_csv)
                self.assertEqual(old_csv.read_bytes(), original)
            finally:
                scoped.close()
            # 1.2 旧版本仍能以12列无列名INSERT写入原表。
            with closing(sqlite3.connect(Path(folder) / 'reviews.sqlite3')) as db:
                self.assertEqual(len(db.execute('PRAGMA table_info(reviews)').fetchall()), 12)
                db.execute('INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           ('legacy-key-2', 'hotel', '同名酒店', '乙', '2026', 'stay_month', '5.0', '第二条', 0, '[]', 'old.xml', 'now'))
                db.commit()
                self.assertEqual(db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 2)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM reviews_scoped').fetchone()[0], 1)
        logger.info('旧版兼容测试完成')

    def test_scope_separates_same_name_and_survives_name_change(self):
        # /* ===== 步骤1：核对隔离和去重；数据源：两个人工声明ID；操作：1) 同名隔离 2) 同ID改名续跑 ===== */
        logger.info('开始作用域隔离测试')
        row = {'author': '甲', 'date': '2026', 'date_kind': 'stay_month', 'rating': '5.0',
               'content': '相同评论', 'has_expand': False, 'raw_texts': ['相同评论']}
        with TemporaryDirectory() as folder:
            first = MobileScopedStore(folder, 'hotel', '00100')
            second = MobileScopedStore(folder, 'hotel', '200')
            try:
                # 1.1 不同ID相同名称各有独立数据；同一ID去重不依赖名称。
                self.assertEqual(first.save('同名酒店', [row], 'first.xml'), 1)
                self.assertEqual(second.count(), 0)
                self.assertEqual(second.save('同名酒店', [row], 'second.xml'), 1)
                self.assertEqual(first.save('酒店更名', [row], 'third.xml'), 0)
                self.assertEqual(first.count(), 1)
                self.assertEqual(second.count(), 1)
                self.assertNotEqual(first.export(), second.export())
                with closing(sqlite3.connect(Path(folder) / 'reviews.sqlite3')) as db:
                    self.assertEqual({r[0] for r in db.execute('SELECT identity_source FROM reviews_scoped')}, {'operator_declared_unverified'})
            finally:
                first.close()
                second.close()
        logger.info('作用域隔离测试完成')

    def test_export_escapes_formulas_and_invalid_ids_are_rejected(self):
        # /* ===== 步骤1：核对输入与导出；数据源：用户声明和评论；操作：1) 拒绝无效ID 2) 转义公式 ===== */
        logger.info('开始作用域导出测试')
        row = {'author': '@user', 'date': '2026', 'date_kind': 'review_date', 'rating': '5.0',
               'content': '=1+1\n第二行', 'has_expand': False, 'raw_texts': ['=1+1']}
        with TemporaryDirectory() as folder:
            # 1.1 ID只能是ASCII正整数字符串，不能以不可信名称拼接路径。
            for bad in ('', '0', '-1', 'abc', '１２３', '9' * 21, 123, True):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    MobileScopedStore(folder, 'sight', bad)
            store = MobileScopedStore(folder, 'sight', '321')
            try:
                store.save('测试景点', [row], 'sample.xml')
                path = store.export()
                self.assertRegex(path.name, r'^sight-v2-[0-9a-f]{24}\.csv$')
                with path.open(encoding='utf-8-sig', newline='') as stream:
                    exported = list(csv.DictReader(stream))
                self.assertEqual(exported[0]['content'], "'=1+1\n第二行")
                self.assertEqual(exported[0]['author'], "'@user")
                self.assertEqual(exported[0]['object_id'], '321')
                self.assertEqual(exported[0]['identity_source'], 'operator_declared_unverified')
            finally:
                store.close()
        logger.info('作用域导出测试完成')
