import json
import io
import logging
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from pathlib import Path

from coverage_report import write_coverage_report
from mobile_scoped_store import MobileScopedStore

logger = logging.getLogger(__name__)


class CoverageReportTests(unittest.TestCase):
    def test_shopping_list_and_detail_without_database_or_offer_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            common = {'kind': 'shopping', 'source': 'ctrip_android_ui', 'status': 'partial'}
            listing = dict(common, source_method='android_shopping_list_snapshot', city='上海',
                           screens=[{'index': 1}, {'index': 2}],
                           shops=[{'name': '测试商铺'}, {'name': '另外商铺'}])
            detail = dict(common, source_method='android_shopping_detail_snapshot', name='测试商铺',
                          score_display='4.6', address_display='TEST_SHOP_ADDRESS', category_labels=['购物中心'],
                          offers=['TEST_SHOP_OFFER'], review_entry_visible=True)
            (root / f"app-shopping-list-{'a' * 32}.json").write_text(json.dumps(listing), encoding='utf-8')
            (root / f"app-shopping-detail-{'b' * 32}.json").write_text(json.dumps(detail), encoding='utf-8')
            report = write_coverage_report(root, include_android=True)
            android = report['android']
            self.assertEqual(android['review_database'], 'missing')
            self.assertEqual(android['shopping_lists'][0]['display_record_count'], 2)
            self.assertEqual(android['shopping_lists'][0]['screen_count'], 2)
            item = android['shopping_details'][0]
            self.assertEqual(item['offer_count'], 1)
            self.assertEqual(item['category_count'], 1)
            self.assertTrue(item['review_entry_visible'])
            self.assertEqual(set(item['fields_present']), {'address_display', 'score_display', 'category_labels', 'offers'})
            self.assertFalse(item['complete'])
            saved = Path(report['report_file']).read_text(encoding='utf-8')
            self.assertNotIn('TEST_SHOP_ADDRESS', saved)
            self.assertNotIn('TEST_SHOP_OFFER', saved)

    def test_shopping_scoped_review_coverage_uses_only_confirmed_kind_and_number(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with closing(sqlite3.connect(root / 'reviews.sqlite3')) as db:
                db.execute('CREATE TABLE reviews_scoped(kind TEXT, object_id TEXT, has_expand INTEGER, content TEXT)')
                db.executemany('INSERT INTO reviews_scoped VALUES(?, ?, ?, ?)',
                               [('shopping', '1501', 0, 'PRIVATE_SHOP_REVIEW'),
                                ('shopping', '1501', 1, 'PRIVATE_SHOP_REVIEW'),
                                ('shopping', 'bad', 0, 'invalid'),
                                ('unknown', '1502', 0, 'invalid')])
                db.commit()
            report = write_coverage_report(root, include_android=True)
            self.assertEqual(report['android']['reviews'], [{
                'declared_object_key': 'shopping-1501', 'review_count': 2, 'expand_marked_count': 1,
                'identity_verification': 'operator_declared_unverified',
            }])
            self.assertNotIn('PRIVATE_SHOP_REVIEW', Path(report['report_file']).read_text('utf-8'))

    def test_restaurant_scoped_rows_are_reported_without_review_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = MobileScopedStore(root, 'restaurant', '2501')
            try:
                store.save('测试餐厅', [{'author': '匿名作者', 'date': '2025-10-15', 'date_kind': 'review_date',
                                      'rating': '5.0', 'content': 'PRIVATE_REVIEW', 'has_expand': True,
                                      'raw_texts': ['PRIVATE_REVIEW']}], 'raw/test/00000.xml')
            finally:
                store.close()
            report = write_coverage_report(root, include_android=True)
            self.assertEqual(report['objects'], [])
            self.assertEqual(report['android']['reviews'], [{
                'declared_object_key': 'restaurant-2501', 'review_count': 1, 'expand_marked_count': 1,
                'identity_verification': 'operator_declared_unverified',
            }])
            self.assertNotIn('PRIVATE_REVIEW', Path(report['report_file']).read_text(encoding='utf-8'))
            self.assertFalse(report['complete'])

    def test_restaurant_dish_scan_keeps_stop_reason_without_dish_names(self):
        # /* ===== 步骤1：核对推荐菜扫描结论；数据源：详情报告；操作：1) 保留原因与屏数 2) 不写菜名 ===== */
        logger.info('开始推荐菜扫描覆盖测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            common = {'kind': 'restaurant', 'source': 'ctrip_android_ui', 'status': 'partial',
                      'mode': 'app_restaurant_detail', 'source_method': 'android_restaurant_detail_snapshot',
                      'name': '测试餐厅', 'recommended_dishes': ['PRIVATE_DISH'], 'dish_count_display': 29}
            # 1.1 正常结束的扫描；另两份分别是没有选读、以及结构不可读。
            done = dict(common, recommendation_scan={'reason': 'expected_count_reached', 'method': 'menu',
                                                     'screens': 3, 'complete': True})
            absent = dict(common, name='没有选读')
            broken = dict(common, name='坏记录', recommendation_scan={'reason': 'something_else'})
            for letter, record in (('a', done), ('b', absent), ('c', broken)):
                (root / f"app-restaurant-detail-{letter * 32}.json").write_text(
                    json.dumps(record), encoding='utf-8')
            details = {item['name']: item for item in write_coverage_report(root, include_android=True)['android']['restaurant_details']}
            self.assertEqual(details['测试餐厅']['recommendation_scan'],
                             {'status': 'expected_count_reached', 'method': 'menu', 'screens': 3, 'complete': True})
            self.assertIsNone(details['没有选读']['recommendation_scan'])
            self.assertEqual(details['坏记录']['recommendation_scan'], {'status': 'unreadable'})
            # 1.2 覆盖报告只留计数与固定词表，菜名不能出现在报告文件里。
            serialized = Path(write_coverage_report(root, include_android=True)['report_file']).read_text(encoding='utf-8')
            self.assertNotIn('PRIVATE_DISH', serialized)
            self.assertIn('expected_count_reached', serialized)
        logger.info('推荐菜扫描覆盖测试完成')

    def test_restaurant_list_and_detail_are_counted_without_a_review_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            common = {'kind': 'restaurant', 'source': 'ctrip_android_ui', 'status': 'partial'}
            listing = dict(common, mode='app_restaurant_list', source_method='android_restaurant_list_snapshot',
                           city='上海', screens=[{'index': 1}],
                           restaurants=[{'name': '测试餐厅'}, {'name': '另一餐厅'}])
            detail = dict(common, mode='app_restaurant_detail', source_method='android_restaurant_detail_snapshot',
                          name='测试餐厅', address_display='TEST_ADDRESS', score_display='4.6',
                          recommended_dishes=['TEST_DISH'], dish_count_display=8, booking_preview={})
            (root / f"app-restaurant-list-{'a' * 32}.json").write_text(json.dumps(listing), encoding='utf-8')
            (root / f"app-restaurant-detail-{'b' * 32}.json").write_text(json.dumps(detail), encoding='utf-8')
            report = write_coverage_report(root, include_android=True)
            android = report['android']
            self.assertEqual(android['review_database'], 'missing')
            self.assertEqual(android['restaurant_lists'][0]['display_record_count'], 2)
            self.assertEqual(android['restaurant_lists'][0]['screen_count'], 1)
            self.assertEqual(android['restaurant_details'][0]['recommended_dish_count'], 1)
            self.assertEqual(android['restaurant_details'][0]['dish_count_display'], 8)
            self.assertEqual(set(android['restaurant_details'][0]['fields_present']), {'address_display', 'score_display'})
            self.assertEqual(android['restaurant_details'][0]['status'], 'partial')
            self.assertFalse(android['restaurant_details'][0]['complete'])
            serialized = Path(report['report_file']).read_text(encoding='utf-8')
            self.assertNotIn('TEST_DISH', serialized)
            self.assertNotIn('TEST_ADDRESS', serialized)

    def test_app_capture_history_is_visible_without_counting_reviews_twice(self):
        # /* ===== 步骤1：核验历史覆盖；数据源：当前对象和历史快照；操作：1) 暴露历史状态 2) 不重复计数 ===== */
        logger.info('开始AI历史覆盖测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            current = {'kind':'hotel','resource_id':123,'source_method':'app_proxy_capture',
                       'ai_summary':{'status':'not_captured'},'ai_photos':{'status':'not_captured'}}
            historic = {**current, 'ai_summary':{'status':'response_captured'},
                        'ai_photos':{'status':'response_captured'}}
            (root/'hotel-123-app-capture-object.json').write_text(json.dumps(current),encoding='utf-8')
            (root/('hotel-123-app-capture-history-'+'a'*32+'.json')).write_text(json.dumps(historic),encoding='utf-8')
            # 1.0 文件名与内容身份不同，不能把其他酒店的快照归到当前对象。
            (root/('hotel-456-app-capture-history-'+'b'*32+'.json')).write_text(json.dumps(historic),encoding='utf-8')
            with closing(sqlite3.connect(root/'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews(object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.execute('INSERT INTO network_reviews VALUES(?,?,?,?)',('hotel-123','1','{}','now'))
                db.commit()
            # 1.1 当前快照为空不代表历史未曾捕获；评论数仍只来自数据库。
            report = write_coverage_report(root)
            obj = report['objects'][0]
            self.assertEqual(obj['review_count'],1)
            self.assertEqual({x['snapshot_role'] for x in obj['sources']},{'current','capture_history'})
            self.assertTrue(any(x['snapshot_role']=='capture_history' and x['ai_summary_status']=='response_captured' for x in obj['sources']))
            self.assertFalse(obj['complete'])
            self.assertEqual(report['invalid_object_files'],1)
        logger.info('AI历史覆盖测试完成')

    def test_cli_generates_report_without_network_collection(self):
        # /* ===== 步骤1：核对命令入口；数据源：本地数据库；操作：1) 输出覆盖报告 2) 不启动网络采集 ===== */
        logger.info('开始覆盖报告命令测试')
        import app
        with tempfile.TemporaryDirectory() as folder:
            with closing(sqlite3.connect(Path(folder)/'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.execute('INSERT INTO network_reviews VALUES(?,?,?,?)',('sight-1','1','{"content":"PRIVATE_TEXT"}','t'))
                db.commit()
            # 1.1 只输出统计结果，不能把评论正文复制到覆盖报告。
            with patch('sys.argv',['app.py','--coverage','--output',folder]), patch('sys.stdout',new_callable=io.StringIO) as stdout, patch('app.collect_batch',side_effect=AssertionError('network')):
                app.main()
                report=json.loads(stdout.getvalue())
                self.assertEqual(report['objects'][0]['review_count'],1)
                self.assertNotIn('PRIVATE_TEXT',stdout.getvalue())
        logger.info('覆盖报告命令测试完成')

    def test_distinguishes_missing_null_and_populated_fields(self):
        # /* ===== 步骤1：核对字段覆盖；数据源：本地评论库；操作：1) 分开缺失与空值 2) 不推断全量 ===== */
        logger.info('开始字段覆盖统计测试')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'sight-1-object.json').write_text(json.dumps({'kind':'sight','resource_id':1,'name':'景区',
                'display_total':100,'details':{},'ai_summary':{'status':'not_captured','content':None}}),encoding='utf-8')
            with closing(sqlite3.connect(root/'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                rows=[{'content':'文字','images':[{'url':'public'}],'videos':None}, {'content':'','images':[]}, {'score':0}]
                db.executemany('INSERT INTO network_reviews VALUES(?,?,?,?)', [('sight-1',str(i),json.dumps(row),'t') for i,row in enumerate(rows)])
                db.commit()
            # 1.1 三条评论中的缺失字段不能写成平台没有该字段。
            report=write_coverage_report(root)
            obj=report['objects'][0]
            self.assertEqual(obj['review_count'],3)
            self.assertEqual(obj['fields']['images'],{'present':2,'nonempty':1,'missing':1})
            self.assertEqual(obj['fields']['videos'],{'present':1,'nonempty':0,'missing':2})
            self.assertEqual(obj['fields']['score']['nonempty'],1)
            self.assertFalse(obj['complete'])
            self.assertEqual(obj['sources'][0]['ai_summary_status'],'not_captured')
            self.assertEqual(json.loads(Path(report['report_file']).read_text(encoding='utf-8'))['objects'],report['objects'])
        logger.info('字段覆盖统计测试完成')

    def test_invalid_rows_are_reported_not_silently_discarded(self):
        # /* ===== 步骤2：核对坏数据；数据源：无法解码的历史行；操作：1) 记录异常数量 2) 保留其他对象 ===== */
        logger.info('开始坏评论统计测试')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with closing(sqlite3.connect(root/'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.execute('INSERT INTO network_reviews VALUES(?,?,?,?)',('sight-2','1','not json','t'))
                db.commit()
            result=write_coverage_report(root)
            self.assertEqual(result['objects'][0]['invalid_rows'],1)
            self.assertEqual(result['objects'][0]['review_count'],1)
        logger.info('坏评论统计测试完成')
