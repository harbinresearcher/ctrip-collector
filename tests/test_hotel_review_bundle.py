import csv
import io
import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock
from unittest.mock import patch

from ai_capture import capture_ai
from hotel_review_bundle import open_hotel_review_bundle
from runner import collect
from workbench_app_jobs import prepare_app_job


FIXTURES = Path(__file__).parent / 'fixtures'
NAME = '示例酒店'


class HotelReviewBundleTests(unittest.TestCase):
    def test_combined_switch_reuses_first_review_screen_and_pins_its_own_summary(self):
        with TemporaryDirectory() as folder, patch('workbench_app_jobs.Adb') as factory:
            mock = factory.return_value.__enter__.return_value
            mock.snapshot.return_value = (FIXTURES / 'hotel-comments.xml').read_text('utf-8')
            request = {'mode': 'app_reviews', 'kind': 'hotel', 'name': NAME, 'resource_id': '123',
                       'limit': 1, 'max_swipes': 1, 'save_visible_ai': True}
            report = prepare_app_job(request, folder, 'fake-adb')(Event(), lambda _: None)
            mock.snapshot.assert_called_once()
            mock.run.assert_not_called()
            captured = report['first_screen_ai']
            self.assertEqual(captured['status'], 'saved')
            self.assertEqual(captured['summary_status'], 'partial')
            archive = Path(folder) / 'ai' / captured['source_file']
            self.assertTrue(archive.is_file())
            self.assertEqual(archive.with_suffix('.xml').read_bytes(),
                             (FIXTURES / 'hotel-comments.xml').read_bytes())

            newer = Mock(**{'snapshot.return_value': (FIXTURES / 'hotel-ai-expanded.xml').read_text('utf-8')})
            capture_ai(newer, 'hotel', NAME, '123', folder, stop=Event(), include_gallery=False)
            with open_hotel_review_bundle(folder, {'status': 'finished', 'result': report}) as stream:
                with zipfile.ZipFile(stream) as saved:
                    ai = json.loads(saved.read('ai-summary.json'))
                    self.assertEqual(ai['source_file'], archive.name)
                    self.assertEqual(len(ai['sections']), 1)
            archive.unlink()
            with self.assertRaises(LookupError):
                open_hotel_review_bundle(folder, {'status': 'finished', 'result': report})

    def test_missing_summary_keeps_review_sample_but_does_not_substitute_history(self):
        with TemporaryDirectory() as folder, patch('workbench_app_jobs.Adb') as factory:
            mock = factory.return_value.__enter__.return_value
            mock.snapshot.return_value = (FIXTURES / 'hotel-comments2.xml').read_text('utf-8')
            request = {'mode': 'app_reviews', 'kind': 'hotel', 'name': NAME, 'resource_id': '123',
                       'limit': 1, 'max_swipes': 1, 'save_visible_ai': True}
            report = prepare_app_job(request, folder, 'fake-adb')(Event(), lambda _: None)
            self.assertEqual(report['sample_csv_count'], 1)
            self.assertEqual(report['first_screen_ai']['status'], 'not_visible')
            older = Mock(**{'snapshot.return_value': (FIXTURES / 'hotel-ai-expanded.xml').read_text('utf-8')})
            capture_ai(older, 'hotel', NAME, '123', folder, stop=Event(), include_gallery=False)
            with self.assertRaises(LookupError):
                open_hotel_review_bundle(folder, {'status': 'finished', 'result': report})

    def setup_run(self, folder):
        page = (FIXTURES / 'hotel-comments.xml').read_text('utf-8')
        device = Mock(**{'snapshot.return_value': page})
        result = collect(device, 'hotel', NAME, folder, limit=1, max_swipes=0, object_id='123')
        return {'status': 'finished', 'result': result}

    def test_bundle_uses_only_the_same_declared_name_and_id_and_leaves_out_raw_evidence(self):
        with TemporaryDirectory() as folder:
            snapshot = self.setup_run(folder)
            ai = Mock(**{'snapshot.return_value': (FIXTURES / 'hotel-ai-expanded.xml').read_text('utf-8')})
            capture_ai(ai, 'hotel', NAME, '00123', folder, stop=Event(), include_gallery=False)
            capture_ai(ai, 'hotel', '同号不同名', '123', folder, stop=Event(), include_gallery=False)
            capture_ai(ai, 'hotel', NAME, '456', folder, stop=Event(), include_gallery=False)
            with open_hotel_review_bundle(folder, snapshot) as body, zipfile.ZipFile(body) as saved:
                self.assertEqual(set(saved.namelist()), {'manifest.json', 'ai-summary.json', 'review-sample.csv'})
                manifest = json.loads(saved.read('manifest.json'))
                summary = json.loads(saved.read('ai-summary.json'))
                rows = list(csv.DictReader(io.StringIO(saved.read('review-sample.csv').decode('utf-8-sig'))))
                self.assertEqual((summary['name'], summary['resource_id']), (NAME, 123))
                self.assertTrue(summary['sections'])
                self.assertNotIn('header_texts', summary)
                self.assertNotIn('evidence', summary)
                self.assertEqual(len(rows), manifest['review_sample']['row_count'])
                self.assertEqual(manifest['identity_verification'], 'operator_declared_id_and_name_only')
                self.assertFalse(manifest['complete'])

    def test_no_current_sample_or_no_matching_summary_is_an_explicit_failure(self):
        with TemporaryDirectory() as folder:
            run = self.setup_run(folder)
            with self.assertRaisesRegex(LookupError, '同名'):
                open_hotel_review_bundle(folder, run)
            run['status'] = 'running'
            with self.assertRaises(ValueError):
                open_hotel_review_bundle(folder, run)
