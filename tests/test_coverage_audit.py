import io
import os
import json
import logging
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from coverage_audit import read_audit_history, write_coverage_audit
from gap_collect import collect_gaps, gap_candidates
from mobile_scoped_store import MobileScopedStore

logger = logging.getLogger(__name__)


def discovery_report(root, name, **fields):
    report = {'objects': [], 'pages': 1, 'listing_total': None, 'complete': False,
              'reason': 'page_budget_reached', 'error': '', 'links_file': ''}
    report.update(fields)
    (Path(root) / name).write_text(json.dumps(report), encoding='utf-8')


def save_json(root, name, **data):
    (root / name).write_text(json.dumps(data), encoding='utf-8')


def save_app_report(root, run_id, kind, identity, place, *, review_filter='全部',
                    state='compared_display_only', total=1, reason='target_reached'):
    save_json(root, f'report-{run_id}.json', run_id=run_id, kind=kind, object_id=identity, place=place,
              reason=reason, error='sample failure' if reason == 'error' else '',
              identity_id_verified=False,
              review_count_check={'scope': 'current_run_fingerprints_vs_app_filter_label',
                                  'filter': review_filter, 'state': state,
                                  'display_total': total, 'current_run_unique': total})


class CoverageAuditTests(unittest.TestCase):
    def test_web_gap_and_renewal_use_only_web_rows_and_reports(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            url = 'https://you.ctrip.com/sight/a1/40.html'
            save_json(root, 'sight-40-object.json', kind='sight', resource_id=40, display_total=2, url=url)
            save_json(root, 'sight-40-report-first.json', object_key='sight-40', reason='target_reached',
                      source_method='public_comment_api')
            save_app_report(root, '20261126T072300-abcdef', 'sight', '40', '测试景点', reason='error')
            with closing(sqlite3.connect(root / 'network.sqlite3')) as db, db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.execute('INSERT INTO network_reviews VALUES(?,?,?,?)', ('sight-40', 'web-1', '"同一条点评"', 't'))
            with closing(sqlite3.connect(root / 'reviews.sqlite3')) as db, db:
                db.execute('CREATE TABLE reviews_scoped (key TEXT PRIMARY KEY, kind TEXT, object_id TEXT, content TEXT)')
                db.execute('INSERT INTO reviews_scoped VALUES(?,?,?,?)', ('app-1', 'sight', '40', '同一条点评'))
            obj = write_coverage_audit(root)['objects'][0]
            self.assertEqual((obj['target'], obj['collected'], obj['missing'], obj['complete']), (2, 1, 1, False))
            self.assertEqual(obj['collected_by_source'], {'network_reviews': 1, 'reviews_scoped': 1, 'reviews': 0})
            self.assertEqual((obj['latest_reason'], obj['gap_reason']), ('target_reached', 'missing'))

            with patch('gap_collect.collect_batch', return_value={'results': []}) as batch:
                followup = collect_gaps(root, 1, collector=lambda *args: None)
            self.assertEqual(followup['selected'], ['sight-40'])
            urls, output, limit = batch.call_args.args[:3]
            self.assertEqual((urls, limit), ([url], 2))
            self.assertTrue(output.samefile(root))

    def test_app_count_requires_a_confirmed_all_filter_and_exact_run_membership(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            place, identity = '测试餐厅', '41'
            all_run = '20261126T072300-abcdef'
            filtered_run = '20261126T082300-aabbcc'
            row = {'author': '用户', 'date': '2025-01-01', 'date_kind': 'review_date', 'rating': '5.0',
                   'has_expand': False, 'raw_texts': [], 'card_fields': {}}
            with closing(MobileScopedStore(root, 'restaurant', identity)) as store:
                for run, bodies in ((all_run, ('第一条', '之后被删除的一条')), (filtered_run, ('仅筛选任务看到',))):
                    store.start_run(run, place)
                    store.save(place, [dict(row, content=body) for body in bodies], 'sample.xml',
                               observed_keys={}, run_id=run)
                    store.finish_run(run, 'target_reached')
                save_app_report(root, all_run, 'restaurant', identity, place, total=2)
                self.assertTrue(write_coverage_audit(root)['objects'][0]['app_all_run']['display_total_matched'])
                save_app_report(root, filtered_run, 'restaurant', identity, place, review_filter='图/视频', total=1)
                with store.db:
                    store.db.execute('DELETE FROM reviews_scoped WHERE content=?', ('之后被删除的一条',))
                report = write_coverage_audit(root)
                obj = report['objects'][0]
                self.assertEqual((obj['target'], obj['missing'], obj['complete']), (None, None, False))
                self.assertEqual(obj['collected_by_source']['reviews_scoped'], 2)
                check = obj['app_all_run']
                self.assertEqual((check['run_id'], check['display_total'], check['collected'], check['missing']),
                                 (all_run, 2, 1, 1))
                self.assertFalse(check['display_total_matched'])
                self.assertFalse(check['identity_id_verified'])
                self.assertEqual(report['summary']['unknown_target'], 1)
                for bad in ({'review_filter': '图/视频'}, {'state': 'approximate_count'}, {'state': 'selection_unconfirmed'}):
                    with self.subTest(bad=bad):
                        save_app_report(root, all_run, 'restaurant', identity, place, total=2, **bad)
                        obj = write_coverage_audit(root)['objects'][0]
                        self.assertIsNone(obj['app_all_run'])
                        self.assertTrue(obj['target_unknown'])
                save_app_report(root, all_run, 'restaurant', identity, '另一家店', total=2)
                self.assertIsNone(write_coverage_audit(root)['objects'][0]['app_all_run'])

    def test_saved_app_import_rows_and_totals_do_not_inflate_web_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_json(root, 'hotel-42-object.json', kind='hotel', resource_id=42,
                      display_total=3, source_method='hotel_browser_response')
            save_json(root, 'hotel-42-app-capture-object.json', kind='hotel', resource_id=42,
                      display_total=1, source_method='app_proxy_capture')
            save_json(root, 'hotel-42-report-import.json', object_key='hotel-42', display_total=1,
                      source_method='app_proxy_capture', reason='error', error='offline evidence failed')
            save_json(root, 'hotel-43-app-capture-object.json', kind='hotel', resource_id=43,
                      display_total=1, source_method='app_proxy_capture')
            with closing(sqlite3.connect(root / 'network.sqlite3')) as db, db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.executemany('INSERT INTO network_reviews VALUES(?,?,?,?)',
                               [('hotel-42', key, '{}', 't') for key in ('both', 'import', 'historical', 'legacy')])
                db.execute('INSERT INTO network_reviews VALUES(?,?,?,?)', ('hotel-43', 'unattributed', '{}', 't'))
                db.execute('INSERT INTO network_reviews VALUES(?,?,?,?)', ('hotel-44', 'legacy', '{}', 't'))
                db.executemany('INSERT INTO network_reviews VALUES(?,?,?,?)',
                               [('hotel-45', key, '{}', 't') for key in ('import', 'unattributed')])
                db.execute('CREATE TABLE network_review_observations (object_key TEXT, comment_id TEXT,'
                           ' source_method TEXT, data_hash TEXT, data TEXT, fetched_at TEXT)')
                db.executemany('INSERT INTO network_review_observations VALUES(?,?,?,?,?,?)',
                               [('hotel-42', key, source, stamp, '{}', 't') for key, source, stamp in (
                                   ('both', 'hotel_browser_response', '1'), ('both', 'hotel_browser_response', '2'),
                                   ('both', 'app_proxy_capture', '3'), ('import', 'app_proxy_capture', '4'),
                                   ('historical', 'historical_unknown', '5'))])
                db.execute('INSERT INTO network_review_observations VALUES(?,?,?,?,?,?)',
                           ('hotel-45', 'import', 'app_proxy_capture', '1', '{}', 't'))
            objects = {obj['object_key']: obj for obj in write_coverage_audit(root)['objects']}
            web, imported, legacy = (objects[f'hotel-{identity}'] for identity in (42, 43, 44))
            self.assertEqual((web['target'], web['collected'], web['missing'], web['complete']), (3, 1, 2, False))
            self.assertEqual(web['collected_by_source']['network_reviews'], 4)
            self.assertEqual((web['latest_reason'], web['gap_reason']), (None, 'missing'))
            self.assertEqual((imported['target'], imported['collected'], imported['missing']), (None, 0, None))
            self.assertEqual(imported['collected_by_source']['network_reviews'], 1)
            self.assertEqual(legacy['collected'], 1)
            self.assertEqual(objects['hotel-45']['collected'], 0)

            with closing(sqlite3.connect(root / 'network.sqlite3')) as db, db:
                db.execute('DROP TABLE network_review_observations')
            legacy_rows = {obj['object_key']: obj for obj in write_coverage_audit(root)['objects']}
            self.assertEqual(legacy_rows['hotel-42']['collected_by_source']['network_reviews'], 4)
            self.assertEqual(legacy_rows['hotel-42']['collected'], 0)
            self.assertEqual(legacy_rows['hotel-44']['collected'], 1)

    def test_known_target_and_no_gap_is_complete(self):
        # /* ===== 步骤1：核对完整对象；数据源：对象快照与数据库；操作：1) 目标来自接口回显 2) 缺口为零 ===== */
        logger.info('开始完整目标对账测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sight-1-object.json').write_text(json.dumps(
                {'kind': 'sight', 'resource_id': 1, 'name': '测试景点', 'display_total': 2}), encoding='utf-8')
            with closing(sqlite3.connect(root / 'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.executemany('INSERT INTO network_reviews VALUES(?,?,?,?)',
                               [('sight-1', str(index), '{}', 't') for index in (1, 2)])
                db.commit()
            report = write_coverage_audit(root)
            obj = report['objects'][0]
            self.assertEqual((obj['object_key'], obj['target'], obj['collected']), ('sight-1', 2, 2))
            self.assertEqual(obj['target_source'], 'sight-1-object.json:display_total')
            self.assertFalse(obj['target_unknown'])
            self.assertEqual((obj['missing'], obj['gap_reason'], obj['complete']), (0, None, True))
            self.assertEqual({field: report['summary'][field]
                              for field in ('target', 'collected', 'missing', 'reachable_missing',
                                            'capped_count', 'unknown_target', 'object_count',
                                            'directory_count', 'complete')},
                             {'target': 2, 'collected': 2, 'missing': 0, 'reachable_missing': 0,
                              'capped_count': 0, 'unknown_target': 0, 'object_count': 1,
                              'directory_count': 0, 'complete': False})
            # 1.1 报告文件写进同一目录，但下一次对账不能把它当成采集证据。
            saved = json.loads(Path(report['report_file']).read_text(encoding='utf-8'))
            self.assertEqual(saved['objects'], report['objects'])
            self.assertEqual(write_coverage_audit(root)['summary'], report['summary'])
        logger.info('完整目标对账测试完成')

    def test_known_target_with_gap_is_missing(self):
        # /* ===== 步骤2：核对缺口；数据源：接口总数与数据库行数；操作：1) 计算差值 2) 原因记为 missing ===== */
        logger.info('开始缺口对账测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sight-2-object.json').write_text(json.dumps(
                {'kind': 'sight', 'resource_id': 2, 'name': '测试景点', 'display_total': 100}), encoding='utf-8')
            with closing(sqlite3.connect(root / 'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.executemany('INSERT INTO network_reviews VALUES(?,?,?,?)',
                               [('sight-2', str(index), '{}', 't') for index in range(3)])
                db.commit()
            obj = write_coverage_audit(root)['objects'][0]
            self.assertEqual((obj['target'], obj['collected'], obj['missing']), (100, 3, 97))
            self.assertEqual(obj['gap_reason'], 'missing')
            self.assertFalse(obj['complete'])
        logger.info('缺口对账测试完成')

    def test_failed_report_reason_is_reported_as_failed(self):
        # /* ===== 步骤3：核对失败原因；数据源：运行报告；操作：1) 保留停止原因 2) 缺口记为 failed ===== */
        logger.info('开始失败原因对账测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sight-3-report-20261126T000000000000Z.json').write_text(json.dumps(
                {'object_key': 'sight-3', 'name': '测试景点', 'display_total': 10, 'reason': 'error',
                 'error': '接口返回错误、验证或未知结构，已停止；不是空数据。'}), encoding='utf-8')
            with closing(sqlite3.connect(root / 'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.execute('INSERT INTO network_reviews VALUES(?,?,?,?)', ('sight-3', '1', '{}', 't'))
                db.commit()
            obj = write_coverage_audit(root)['objects'][0]
            self.assertEqual(obj['latest_reason'], 'error')
            self.assertTrue(obj['latest_error'])
            self.assertEqual(obj['gap_reason'], 'failed')
        logger.info('失败原因对账测试完成')

    def test_unknown_target_is_never_replaced_by_collected_count(self):
        # /* ===== 步骤4：核对未知目标；数据源：目录卡片没回显点评数；操作：1) 标记 target_unknown 2) 不用已采数冒充 ===== */
        logger.info('开始未知目标对账测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            discovery_report(root, 'discovery-sight-2-20261126T000000000000Z.json', listing_total=50,
                             objects=[{'kind': 'sight', 'resource_id': 7, 'name': '无点评数'}])
            with closing(sqlite3.connect(root / 'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.execute('INSERT INTO network_reviews VALUES(?,?,?,?)', ('sight-7', '1', '{}', 't'))
                db.commit()
            report = write_coverage_audit(root)
            obj = report['objects'][0]
            self.assertIsNone(obj['target'])
            self.assertIsNone(obj['target_source'])
            self.assertTrue(obj['target_unknown'])
            self.assertIsNone(obj['missing'])
            self.assertEqual(obj['collected'], 1)
            self.assertFalse(obj['complete'])
            # 4.1 未知目标不计入平台目标，也不能算成 complete。
            self.assertEqual(report['summary']['target'], 0)
            self.assertEqual(report['summary']['unknown_target'], 1)
            self.assertEqual(report['summary']['collected'], 1)
            # 4.2 目录报告单独给出平台总数与已发现对象数。
            directory = report['directories'][0]
            self.assertEqual((directory['listing_total'], directory['discovered_objects']), (50, 1))
            self.assertFalse(directory['target_unknown'])
        logger.info('未知目标对账测试完成')

    def test_missing_database_and_missing_output_directory(self):
        # /* ===== 步骤5：核对缺库与缺目录；数据源：只有对象快照；操作：1) 缺库按零条对账 2) 缺目录明确报错 ===== */
        logger.info('开始缺库缺目录测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sight-4-public-object.json').write_text(json.dumps(
                {'kind': 'sight', 'resource_id': 4, 'name': '公开样本', 'display_total': 5}), encoding='utf-8')
            report = write_coverage_audit(root)
            self.assertEqual(report['network_database'], 'missing')
            self.assertEqual(report['mobile_database'], 'missing')
            obj = report['objects'][0]
            self.assertEqual((obj['target'], obj['collected'], obj['missing']), (5, 0, 5))
            self.assertEqual(obj['gap_reason'], 'missing')
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                write_coverage_audit(Path(folder) / '不存在')
        logger.info('缺库缺目录测试完成')

    def test_directory_and_mobile_reports_are_reconciled(self):
        # /* ===== 步骤6：核对目录与手机；数据源：目录报告、手机报告、手机库；操作：1) 分别取目标 2) 分开统计来源 ===== */
        logger.info('开始目录与手机对账测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            discovery_report(root, 'discovery-shopping-2-20261126T000000000000Z.json', listing_total=8,
                             objects=[{'kind': 'shopping', 'resource_id': 5, 'name': '测试商场', 'comment_count': 8},
                                      {'kind': 'shopping', 'resource_id': 6, 'name': '另一商场', 'comment_count': 3}])
            (root / 'shopping-5-object.json').write_text(json.dumps(
                {'kind': 'shopping', 'resource_id': 5, 'name': '测试商场', 'display_total': 8}), encoding='utf-8')
            (root / 'report-20261126T070000-abcdef.json').write_text(json.dumps(
                {'kind': 'shopping', 'place': '测试商场', 'object_id': '5', 'target': 500, 'count': 4,
                 'reason': 'target_reached', 'error': '',
                 'review_count_check': {'display_total': 8}}), encoding='utf-8')
            # 6.0 旧式手机报告没有对象 ID，只能按类型与名称保留原始计数。
            (root / 'report-20261126T070500-fedcba.json').write_text(json.dumps(
                {'kind': 'shopping', 'place': '测试商场', 'target': 500, 'count': 2,
                 'reason': 'target_reached', 'error': '',
                 'review_count_check': {'display_total': 8}}), encoding='utf-8')
            with closing(sqlite3.connect(root / 'reviews.sqlite3')) as db:
                db.execute('CREATE TABLE reviews (key TEXT PRIMARY KEY, kind TEXT, place TEXT)')
                db.executemany('INSERT INTO reviews VALUES(?,?,?)',
                               [('a', 'shopping', '测试商场'), ('b', 'shopping', '测试商场'), ('c', 'shopping', '另一商场')])
                db.execute('CREATE TABLE reviews_scoped (key TEXT PRIMARY KEY, kind TEXT, object_id TEXT)')
                db.execute('INSERT INTO reviews_scoped VALUES(?,?,?)', ('d', 'shopping', '5'))
                db.commit()
            report = write_coverage_audit(root)
            objects = {obj['object_key']: obj for obj in report['objects']}
            self.assertEqual(set(objects), {'shopping-5', 'shopping-6', 'mobile:shopping:测试商场', 'mobile:shopping:另一商场'})
            self.assertEqual(objects['shopping-5']['target'], 8)
            self.assertEqual(objects['shopping-5']['collected_by_source'],
                             {'network_reviews': 0, 'reviews_scoped': 1, 'reviews': 0})
            self.assertEqual(objects['shopping-5']['collected'], 0)
            self.assertEqual(objects['shopping-6']['target'], 3)
            self.assertEqual(objects['shopping-6']['gap_reason'], 'unadapted')
            self.assertIsNone(objects['mobile:shopping:测试商场']['target'])
            self.assertEqual(objects['mobile:shopping:测试商场']['collected'], 0)
            self.assertEqual(objects['mobile:shopping:测试商场']['collected_by_source']['reviews'], 2)
            self.assertIsNone(objects['mobile:shopping:测试商场']['app_all_run'])
            self.assertEqual(report['directories'][0]['discovered_objects'], 2)
        logger.info('目录与手机对账测试完成')

    def test_object_url_comes_from_landed_evidence(self):
        # /* ===== 步骤8：核对对象链接；数据源：对象快照、运行报告、目录卡片；操作：1) 只收 https 2) 取不到为 null ===== */
        logger.info('开始对象链接对账测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sight-10-object.json').write_text(json.dumps(
                {'kind': 'sight', 'resource_id': 10, 'name': '快照链接', 'display_total': 1,
                 'url': 'https://you.ctrip.com/sight/a1/10.html'}), encoding='utf-8')
            (root / 'sight-11-report-20261126T000000000000Z.json').write_text(json.dumps(
                {'object_key': 'sight-11', 'name': '报告链接', 'display_total': 1, 'reason': 'target_reached',
                 'source_url': 'https://you.ctrip.com/sight/a1/11.html'}), encoding='utf-8')
            discovery_report(root, 'discovery-sight-2-20261126T000000000000Z.json', listing_total=50,
                             objects=[{'kind': 'sight', 'resource_id': 12, 'name': '目录链接', 'comment_count': 5,
                                       'url': 'https://you.ctrip.com/sight/a1/12.html'},
                                      {'kind': 'sight', 'resource_id': 13, 'name': '无链接', 'comment_count': 5},
                                      {'kind': 'sight', 'resource_id': 14, 'name': '非https', 'comment_count': 5,
                                       'url': 'http://you.ctrip.com/sight/a1/14.html'}])
            objects = {obj['object_key']: obj for obj in write_coverage_audit(root)['objects']}
            self.assertEqual(objects['sight-10']['url'], 'https://you.ctrip.com/sight/a1/10.html')
            self.assertEqual(objects['sight-11']['url'], 'https://you.ctrip.com/sight/a1/11.html')
            self.assertEqual(objects['sight-12']['url'], 'https://you.ctrip.com/sight/a1/12.html')
            # 8.1 没有链接证据时保持 null，不拼地址也不猜链接。
            self.assertIsNone(objects['sight-13']['url'])
            self.assertIsNone(objects['sight-14']['url'])
        logger.info('对象链接对账测试完成')

    def test_cli_audit_reads_only_local_evidence(self):
        # /* ===== 步骤7：核对命令入口；数据源：本地目录；操作：1) 输出对账 JSON 2) 不启动网络采集 ===== */
        logger.info('开始覆盖对账命令测试')
        import app
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sight-9-object.json').write_text(json.dumps(
                {'kind': 'sight', 'resource_id': 9, 'name': '命令行景点', 'display_total': 1}), encoding='utf-8')
            with patch('sys.argv', ['app.py', '--audit', '--output', folder]), \
                    patch('sys.stdout', new_callable=io.StringIO) as stdout, \
                    patch('app.collect_batch', side_effect=AssertionError('network')):
                app.main()
                result = json.loads(stdout.getvalue())
                self.assertEqual(result['summary']['target'], 1)
                self.assertEqual(result['objects'][0]['object_key'], 'sight-9')
                self.assertTrue(Path(result['report_file']).is_file())
        logger.info('覆盖对账命令测试完成')

    def test_repeated_empty_pages_are_marked_channel_capped(self):
        # /* ===== 步骤7：通道到顶；数据源：连续两次空页；操作：1) 标 channel_capped 2) 缺口仍计入 ===== */
        logger.info('开始通道到顶分类测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sight-9-object.json').write_text(json.dumps(
                {'kind': 'sight', 'resource_id': 9, 'name': '景点9', 'display_total': 100,
                 'url': 'https://you.ctrip.com/sight/a1/9.html'}), encoding='utf-8')
            for index, reason in enumerate(('empty_page', 'queue_exhausted'), start=1):
                (root / f'sight-9-report-2026010{index}T000000000000Z.json').write_text(json.dumps(
                    {'object_key': 'sight-9', 'kind': 'sight', 'reason': reason, 'error': '',
                     'pages': index, 'count': 5}), encoding='utf-8')
            report = write_coverage_audit(root)
            obj = report['objects'][0]
            # 7.1 通道到顶不等于采完：缺口 100 保留，可达缺口不为 0，并提示还有其它通道。
            self.assertEqual((obj['missing'], obj['gap_reason'], obj['reachable_missing'], obj['capped']),
                             (100, 'channel_capped', 100, True))
            self.assertTrue(obj['needs_other_channel'])
            self.assertEqual(report['summary']['missing'], 100)
            self.assertEqual(report['summary']['reachable_missing'], 100)
            self.assertEqual(report['summary']['capped_count'], 1)
            # 7.2 只空过一次的对象仍算可采，避免把临时抖动当成到顶。
            (root / 'sight-9-report-20260103T000000000000Z.json').write_text(json.dumps(
                {'object_key': 'sight-9', 'kind': 'sight', 'reason': 'target_reached', 'error': '',
                 'pages': 3, 'count': 5}),
                encoding='utf-8')
            again = write_coverage_audit(root)['objects'][0]
            self.assertEqual((again['gap_reason'], again['reachable_missing'], again['capped']),
                             ('missing', 100, False))
        logger.info('通道到顶分类测试完成')

    def test_capped_row_keeps_real_gap_and_points_to_phone(self):
        # /* ===== 步骤7.1：到顶但没采完；数据源：真机同类证据（网页 577/1099、手机全部 1099）；
        #         操作：1) 缺口与可达缺口都保留 2) 标 capped 并提示换通道 ===== */
        logger.info('开始通道到顶缺口保留测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sight-136039-object.json').write_text(json.dumps(
                {'kind': 'sight', 'resource_id': 136039, 'name': '长白山西景区', 'display_total': 1099,
                 'url': 'https://you.ctrip.com/sight/a1/136039.html'}), encoding='utf-8')
            with closing(sqlite3.connect(root / 'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.executemany('INSERT INTO network_reviews VALUES(?,?,?,?)',
                               [('sight-136039', str(index), '{}', 't') for index in range(577)])
                db.commit()
            for index, reason in enumerate(('empty_page', 'queue_exhausted'), start=1):
                (root / f'sight-136039-report-2026112{index}T000000000000Z.json').write_text(json.dumps(
                    {'object_key': 'sight-136039', 'kind': 'sight', 'reason': reason, 'error': '',
                     'pages': index, 'count': index}), encoding='utf-8')
            mobile = {'run_id': '20261127T083202-567534', 'kind': 'sight', 'object_id': 136039,
                      'place': '长白山西景区', 'reason': 'screen_budget_reached', 'count': 13,
                      'review_count_check': {'scope': 'x', 'filter': None, 'state': 'selection_unconfirmed',
                                             'display_total': None},
                      'sampling_context': {'observations': [
                          {'visible_filters': [{'label': '全部', 'display_count': 1099}]}]}}
            (root / 'report-20261127T083202-567534.json').write_text(
                json.dumps(mobile, ensure_ascii=False), encoding='utf-8')
            report = write_coverage_audit(root)
            obj = next(row for row in report['objects'] if row['object_key'] == 'sight-136039')
            # 7.1.1 网页 577、目标 1099、缺口 522 与手机「全部」1099 都按原样保留。
            self.assertEqual((obj['target'], obj['collected'], obj['missing']), (1099, 577, 522))
            self.assertEqual((obj['gap_reason'], obj['capped'], obj['complete']), ('channel_capped', True, False))
            self.assertEqual(obj['reachable_missing'], 522)
            self.assertTrue(obj['needs_other_channel'])
            self.assertEqual((obj['phone_target'], obj['phone_collected']), (1099, 0))
            self.assertEqual((report['summary']['missing'], report['summary']['reachable_missing'],
                              report['summary']['capped_count']), (522, 522, 1))
        logger.info('通道到顶缺口保留测试完成')

    def test_missing_zero_is_the_only_complete_case(self):
        # /* ===== 步骤7.2：真采完；数据源：已采到目标数；操作：1) 可达缺口为 0 2) 不标 capped ===== */
        logger.info('开始采完判定测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sight-8-object.json').write_text(json.dumps(
                {'kind': 'sight', 'resource_id': 8, 'name': '景点8', 'display_total': 3,
                 'url': 'https://you.ctrip.com/sight/a1/8.html'}), encoding='utf-8')
            with closing(sqlite3.connect(root / 'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.executemany('INSERT INTO network_reviews VALUES(?,?,?,?)',
                               [('sight-8', str(index), '{}', 't') for index in range(3)])
                db.commit()
            # 7.2.1 即使最近两次都是空页，只要已采到目标数（missing==0）就是采完，不能说成「到顶未采完」。
            for index, reason in enumerate(('empty_page', 'queue_exhausted'), start=1):
                (root / f'sight-8-report-2026112{index}T000000000000Z.json').write_text(json.dumps(
                    {'object_key': 'sight-8', 'kind': 'sight', 'reason': reason, 'error': '',
                     'pages': index, 'count': 1}), encoding='utf-8')
            report = write_coverage_audit(root)
            obj = report['objects'][0]
            self.assertEqual((obj['missing'], obj['reachable_missing']), (0, 0))
            self.assertIsNone(obj['gap_reason'])
            self.assertFalse(obj['capped'])
            self.assertFalse(obj['needs_other_channel'])
            self.assertTrue(obj['complete'])
            self.assertEqual((report['summary']['reachable_missing'], report['summary']['capped_count']), (0, 0))
        logger.info('采完判定测试完成')

    def test_gap_queue_still_skips_capped_objects(self):
        # /* ===== 步骤7.3：续采队列；数据源：到顶对象与普通缺口对象；操作：1) 到顶对象不入队 2) 普通缺口仍入队 ===== */
        logger.info('开始到顶对象不入续采队列测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for identity, total in ((1, 100), (2, 50)):
                (root / f'sight-{identity}-object.json').write_text(json.dumps(
                    {'kind': 'sight', 'resource_id': identity, 'name': f'景点{identity}',
                     'display_total': total, 'url': f'https://you.ctrip.com/sight/a1/{identity}.html'}),
                    encoding='utf-8')
            for index, reason in enumerate(('empty_page', 'queue_exhausted'), start=1):
                (root / f'sight-1-report-2026112{index}T000000000000Z.json').write_text(json.dumps(
                    {'object_key': 'sight-1', 'kind': 'sight', 'reason': reason, 'error': ''}), encoding='utf-8')
            # 7.3.1 只空过一次的普通缺口对象仍在队列里，说明跳过只针对 channel_capped。
            (root / 'sight-2-report-20261121T000000000000Z.json').write_text(json.dumps(
                {'object_key': 'sight-2', 'kind': 'sight', 'reason': 'empty_page', 'error': ''}), encoding='utf-8')
            objects = write_coverage_audit(root)['objects']
            self.assertEqual({obj['object_key']: obj['gap_reason'] for obj in objects},
                             {'sight-1': 'channel_capped', 'sight-2': 'missing'})
            self.assertEqual([obj['object_key'] for obj in gap_candidates(objects)], ['sight-2'])
        logger.info('到顶对象不入续采队列测试完成')

    def test_each_audit_appends_one_history_row(self):
        # /* ===== 步骤9：核对历史追加；数据源：同一目录连跑两次对账；操作：1) 每次一行 2) 字段来自 summary ===== */
        logger.info('开始历史追加测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sight-1-object.json').write_text(json.dumps(
                {'kind': 'sight', 'resource_id': 1, 'name': '测试景点', 'display_total': 4}), encoding='utf-8')
            with closing(sqlite3.connect(root / 'network.sqlite3')) as db:
                db.execute('CREATE TABLE network_reviews (object_key TEXT, comment_id TEXT, data TEXT, fetched_at TEXT)')
                db.executemany('INSERT INTO network_reviews VALUES(?,?,?,?)',
                               [('sight-1', str(index), '{}', 't') for index in (1, 2)])
                db.commit()
            first = write_coverage_audit(root)
            second = write_coverage_audit(root)
            # 9.1 两次对账追加两行，不是覆盖：第一次那行仍在文件里。
            lines = (root / 'audit-history.jsonl').read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 2)
            rows = [json.loads(line) for line in lines]
            self.assertEqual([row['missing'] for row in rows], [2, 2])
            self.assertEqual(len(first['history']['rows']), 1)
            self.assertEqual(second['history']['rows'], rows)
            latest = rows[-1]
            self.assertEqual((latest['reachable_missing'], latest['target'], latest['collected']), (2, 4, 2))
            self.assertEqual((latest['object_count'], latest['unknown_target']), (1, 0))
            self.assertEqual((latest['failed_count'], latest['unadapted_count'],
                              latest['capped_count']), (0, 0, 0))
            self.assertRegex(latest['captured_at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        logger.info('历史追加测试完成')

    def test_history_skips_broken_lines(self):
        # /* ===== 步骤9.2：核对坏行容错；数据源：手工写坏的历史文件；操作：1) 跳过非 JSON 与非对象 2) 余下仍可读 ===== */
        logger.info('开始历史坏行容错测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'audit-history.jsonl').write_text(
                '{"captured_at": "2026-01-01T00:00:00Z", "missing": 9}\n'
                '这不是 JSON 的坏行\n'
                '[1, 2, 3]\n'
                '{"captured_at": "2026-01-02T00:00:00Z", "missing": 4}\n', encoding='utf-8')
            history = read_audit_history(root)
            self.assertEqual([row['missing'] for row in history['rows']], [9, 4])
            self.assertEqual(history['bad_rows'], 2)
            self.assertEqual(history['trend'], -5)
            self.assertIsNone(history['warning'])
            # 9.2.1 坏行不影响对账本身：仍正常算缺口，并把本次这行追加在坏行之后。
            (root / 'sight-2-object.json').write_text(json.dumps(
                {'kind': 'sight', 'resource_id': 2, 'name': '测试景点', 'display_total': 3}), encoding='utf-8')
            report = write_coverage_audit(root)
            self.assertEqual(report['summary']['missing'], 3)
            self.assertEqual([row['missing'] for row in report['history']['rows']], [9, 4, 3])
            self.assertEqual(report['history']['bad_rows'], 2)
        logger.info('历史坏行容错测试完成')

    def test_history_warns_when_missing_grows(self):
        # /* ===== 步骤9.3：核对变大告警；数据源：两次历史缺口；操作：1) trend 是差值 2) 变大给明确说明 ===== */
        logger.info('开始历史缺口变大告警测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'audit-history.jsonl').write_text(
                '{"captured_at": "2026-01-01T00:00:00Z", "missing": 10, "review_totals_scope": "web_reviews"}\n'
                '{"captured_at": "2026-01-02T00:00:00Z", "missing": 15, "review_totals_scope": "web_reviews"}\n',
                encoding='utf-8')
            history = read_audit_history(root)
            self.assertEqual(history['trend'], 5)
            self.assertIn('缺口比上次多了 5 条', history['warning'])
            # 9.3.1 history 字段随对账输出：本次 20 对上上次 15，trend 为 5。
            (root / 'sight-3-object.json').write_text(json.dumps(
                {'kind': 'sight', 'resource_id': 3, 'name': '测试景点', 'display_total': 20}), encoding='utf-8')
            report = write_coverage_audit(root)
            # 9.3.1 路径按同一文件比较：CI 上临时目录可能带 8.3 短名（RUNNER~1），
            #        直接比字符串会假红。
            self.assertTrue(os.path.samefile(report['history']['file'], root / 'audit-history.jsonl'))
            self.assertEqual(report['history']['trend'], 5)
            self.assertIn('缺口比上次多了 5 条', report['history']['warning'])
            self.assertEqual(report['summary']['missing'], 20)
            # 9.3.2 缺口为零时不制造告警。
            (root / 'sight-3-object.json').unlink()
            self.assertIsNone(write_coverage_audit(root)['history']['warning'])
        logger.info('历史缺口变大告警测试完成')

    def test_old_history_row_with_source_exhausted_count_still_reads(self):
        # /* ===== 步骤9.5：旧历史兼容；数据源：只含 source_exhausted_count 的旧行；操作：1) 不报错 2) 按同义读出 ===== */
        logger.info('开始旧历史字段兼容测试')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'audit-history.jsonl').write_text(
                '{"captured_at": "2026-11-27T00:00:00Z", "missing": 522, "reachable_missing": 0,'
                ' "review_totals_scope": "web_reviews", "source_exhausted_count": 1}\n', encoding='utf-8')
            history = read_audit_history(root)
            # 9.5.1 旧行没有 capped_count：按 source_exhausted_count 的同义值读出，坏行计数仍为 0。
            self.assertEqual(history['bad_rows'], 0)
            self.assertEqual(history['rows'][0]['capped_count'], 1)
            self.assertEqual(history['rows'][0]['missing'], 522)
            # 9.5.2 本次对账照常追加新字段，旧行不被改写，趋势仍按 missing 比较。
            (root / 'sight-1-object.json').write_text(json.dumps(
                {'kind': 'sight', 'resource_id': 1, 'name': '测试景点', 'display_total': 4}), encoding='utf-8')
            report = write_coverage_audit(root)
            self.assertEqual(report['summary']['missing'], 4)
            self.assertEqual(len(report['history']['rows']), 2)
            self.assertEqual((report['history']['rows'][-1]['capped_count'],
                              report['history']['rows'][-1]['missing']), (0, 4))
            self.assertEqual(report['history']['rows'][0]['source_exhausted_count'], 1)
        logger.info('旧历史字段兼容测试完成')

    def test_history_cannot_compare_old_combined_totals_to_web_totals(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'audit-history.jsonl').write_text('{"missing": 1}\n', encoding='utf-8')
            save_json(root, 'sight-1-object.json', kind='sight', resource_id=1, display_total=2)
            history = write_coverage_audit(root)['history']
            self.assertEqual([row['missing'] for row in history['rows']], [1, 2])
            self.assertIsNone(history['trend'])
            self.assertIsNone(history['warning'])
            self.assertEqual(write_coverage_audit(root)['history']['trend'], 0)

    def test_audit_succeeds_when_history_write_fails(self):
        # /* ===== 步骤9.4：核对写历史失败；数据源：打桩让追加打开抛 OSError；操作：1) 只记警告 2) 对账照常返回 ===== */
        logger.info('开始历史写入失败容错测试')
        import app
        real_open = Path.open

        def failing_open(self, mode='r', *args, **kwargs):
            if 'a' in mode:
                raise PermissionError('输出目录只读，无法追加历史')
            return real_open(self, mode, *args, **kwargs)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'sight-4-object.json').write_text(json.dumps(
                {'kind': 'sight', 'resource_id': 4, 'name': '命令行景点', 'display_total': 1}), encoding='utf-8')
            with patch.object(Path, 'open', failing_open), \
                    patch('sys.argv', ['app.py', '--audit', '--output', folder]), \
                    patch('sys.stdout', new_callable=io.StringIO) as stdout, \
                    self.assertLogs('coverage_audit', level='WARNING') as logs:
                app.main()
            result = json.loads(stdout.getvalue())
            # 9.4.1 --audit 仍然成功返回对账结果，历史只降级为空并带警告。
            self.assertEqual(result['summary']['target'], 1)
            self.assertEqual(result['objects'][0]['object_key'], 'sight-4')
            self.assertTrue(Path(result['report_file']).is_file())
            self.assertEqual(result['history']['rows'], [])
            self.assertIsNone(result['history']['trend'])
            self.assertFalse((root / 'audit-history.jsonl').exists())
            self.assertTrue(any('审计历史写入失败' in line for line in logs.output))
        logger.info('历史写入失败容错测试完成')
