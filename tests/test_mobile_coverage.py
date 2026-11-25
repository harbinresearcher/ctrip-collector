import json
import logging
import tempfile
import unittest
from pathlib import Path
from threading import Event

from mobile_scoped_store import MobileScopedStore
from workbench_jobs import make_job

logger = logging.getLogger(__name__)


class MobileCoverageTests(unittest.TestCase):
    def test_gallery_gaps_merge_runs_by_hotel_and_total_without_hiding_conflicts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def page(index, total, status, char=None, exists=True):
                result = {'status': status}
                if char:
                    digest = char * 64
                    filename = f'display-{digest}.png' if status == 'display_saved' else digest + '.jpg'
                    result.update(file=filename, sha256=digest, bytes=4, gallery_index=index, object_key='hotel-42')
                    if exists:
                        (root / filename).write_bytes(b'1234')
                return {'index': index, 'total': total, 'result': result}

            def run(char, pages):
                (root / f'gallery-run-{char * 32}.json').write_text(json.dumps({
                    'object_key': 'hotel-42', 'category': 'AI优选', 'status': 'page_limit', 'pages': pages
                }), encoding='utf-8')

            run('1', [page(1, 8, 'saved', 'a'), page(2, 8, 'no_match'), page(3, 8, 'saved', 'b', exists=False)])
            run('2', [page(1, 8, 'saved', 'c'), page(2, 8, 'display_saved', 'd'), page(5, 8, 'saved', 'e')])
            run('3', [page(1, 9, 'no_match')])
            run('4', [page(1, 9, 'no_match'), page(2, 10, 'no_match')])
            report = make_job({'mode': 'coverage'}, root)(Event(), logger.info)['android']
            self.assertEqual(report['invalid_artifacts'], 1)
            self.assertEqual([group['display_total'] for group in report['gallery_page_coverage']], [8, 9])
            group = report['gallery_page_coverage'][0]
            self.assertEqual(group['run_count'], 2)
            self.assertEqual(group['cache_pages'], [1, 5])
            self.assertEqual(group['display_only_pages'], [2])
            self.assertEqual(group['observed_without_file_ranges'], [[3, 3]])
            self.assertEqual(group['unseen_ranges'], [[4, 4], [6, 8]])
            self.assertEqual(group['missing_file_ranges'], [[3, 4], [6, 8]])
            self.assertEqual(group['conflicting_file_pages'], [1])
            self.assertFalse(group['complete'])

    def test_mobile_only_directory_reports_collected_sources_without_merging(self):
        logger.info('开始验证仅手机数据的覆盖统计')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ai = root / 'ai'
            ai.mkdir()
            summary = {'source': 'ctrip_android_ui', 'kind': 'hotel', 'resource_id': 42,
                       'identity_source': 'operator_declared_current_phone_object',
                       'status': 'expanded_visible', 'sections': [{'text': 'PRIVATE_SUMMARY'}],
                       'ai_selected_images': {'display_count': 300, 'selected_count': 30,
                                              'metadata_status': 'captured'}}
            (ai / 'hotel-0042-20261118T030405000000Z.json').write_text(json.dumps(summary), encoding='utf-8')
            (ai / 'hotel-43-20261118T030405000000Z.json').write_text(json.dumps(summary), encoding='utf-8')
            (root / ('sight-detail-' + 'a' * 32 + '.json')).write_text(json.dumps({
                'name': '测试景点', 'source_channel': 'android_ui_snapshot',
                'identity_verification': 'operator_name_exact_match', 'ticket_categories': ['门票'],
                'price_texts': ['PRIVATE_PRICE_TEXT']}), encoding='utf-8')
            (root / ('sight-policy-' + 'b' * 32 + '.json')).write_text(json.dumps({
                'name': '测试景点', 'source_channel': 'android_ui', 'status': 'page_limit',
                'rows': [{'text': 'PRIVATE_POLICY'}], 'pages': [{}]}), encoding='utf-8')
            filename = 'c' * 64 + '.jpg'
            (root / filename).write_bytes(b'1234')
            display_file = 'display-' + 'f' * 64 + '.png'
            (root / display_file).write_bytes(b'123456')
            (root / ('gallery-run-' + 'd' * 32 + '.json')).write_text(json.dumps({
                'object_key': 'hotel-42', 'category': 'AI优选', 'status': 'end_reached',
                'pages': [{'index': 1, 'total': 4, 'result': {'status': 'saved', 'object_key': 'hotel-42',
                    'gallery_index': 1, 'file': filename, 'sha256': 'c' * 64, 'bytes': 4}},
                    {'index': 2, 'total': 4, 'result': {'status': 'no_match'}},
                    {'index': 3, 'total': 4, 'result': {'status': 'saved', 'object_key': 'hotel-42',
                    'gallery_index': 3, 'file': 'e' * 64 + '.jpg', 'sha256': 'e' * 64, 'bytes': 4}},
                    {'index': 4, 'total': 4, 'result': {'status': 'display_saved', 'object_key': 'hotel-42',
                    'gallery_index': 4, 'file': display_file, 'sha256': 'f' * 64, 'bytes': 6}}]}),
                encoding='utf-8')
            store = MobileScopedStore(root, 'hotel', '42')
            try:
                store.save('测试酒店', [{'author': 'PRIVATE_AUTHOR', 'date': '2026-11-18', 'date_kind': 'exact',
                    'rating': '5', 'content': 'PRIVATE_REVIEW', 'has_expand': True, 'raw_texts': []}], 'unused')
            finally:
                store.close()
            database = root / 'reviews.sqlite3'
            before = database.read_bytes()
            report = make_job({'mode': 'coverage'}, root)(Event(), lambda _: None)
            self.assertEqual(database.read_bytes(), before)
            self.assertFalse((root / 'network.sqlite3').exists())
            self.assertEqual(report['scope'], 'local_network_and_android_artifacts')
            self.assertEqual(report['objects'], [])
            self.assertFalse(report['complete'])
            mobile = report['android']
            self.assertEqual(mobile['reviews'][0]['review_count'], 1)
            self.assertEqual(mobile['reviews'][0]['expand_marked_count'], 1)
            self.assertEqual(mobile['reviews'][0]['identity_verification'], 'operator_declared_unverified')
            self.assertEqual(mobile['ai_summaries'][0]['declared_object_key'], 'hotel-42')
            self.assertEqual(mobile['ai_summaries'][0]['ai_selected_count'], 30)
            self.assertEqual(mobile['invalid_artifacts'], 1)
            self.assertEqual(mobile['gallery_runs'][0]['page_statuses'], {'saved': 2, 'no_match': 1, 'display_saved': 1})
            self.assertEqual(mobile['gallery_runs'][0]['saved_assets_present'], 2)
            self.assertEqual(mobile['gallery_runs'][0]['display_assets_present'], 1)
            self.assertEqual(mobile['sight_details'][0]['fields_present'], ['price_texts', 'ticket_categories'])
            self.assertEqual(mobile['sight_policies'][0]['row_count'], 1)
            saved = Path(report['report_file']).read_text(encoding='utf-8')
            self.assertNotIn('PRIVATE_', saved)
        logger.info('仅手机数据的覆盖统计验证完成')
