import http.client
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from workbench_jobs import make_job
from workbench_server import create_server


def write_snapshot(folder, kind, object_id, name):
    path = Path(folder) / f'{kind}-{object_id}-object.json'
    path.write_text(json.dumps({'kind': kind, 'resource_id': object_id, 'name': name}), encoding='utf-8')
    return path


class WorkbenchObjectOptionsTests(unittest.TestCase):
    def test_object_options_reads_local_evidence_and_checks_origin_and_filters(self):
        with TemporaryDirectory() as folder, create_server(folder, port=0) as server:
            write_snapshot(folder, 'restaurant', '42', '样本餐厅')
            write_snapshot(folder, 'sight', '42', '样本景点')
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                query = urlencode({'kind': 'restaurant', 'q': '样本'})
                conn.request('GET', f'/api/object-options?{query}')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), {'objects': [{
                    'object_ref': 'restaurant-42', 'kind': 'restaurant', 'object_id': '42', 'name': '样本餐厅',
                    'identity_source': 'evidence_manifest', 'identity_evidence': 'restaurant-42-object.json',
                }], 'truncated': False})
                for query, headers, expected in (
                    ('kind=unknown', {}, 400),
                    ('q=a&q=b', {}, 400),
                    ('kind=hotel', {'Origin': 'https://other.example'}, 403),
                ):
                    with self.subTest(query=query, headers=headers):
                        conn.request('GET', f'/api/object-options?{query}', headers=headers)
                        response = conn.getresponse()
                        self.assertEqual(response.status, expected)
                        response.read()
            finally:
                conn.close()
                server.shutdown()
                worker.join(timeout=3)

    def test_invalid_object_references_never_access_android(self):
        with TemporaryDirectory() as folder, patch('workbench_app_jobs.Adb') as review_adb, \
                patch('workbench_jobs.Adb') as gallery_adb:
            write_snapshot(folder, 'restaurant', '42', '样本餐厅')
            write_snapshot(folder, 'hotel', '42', '样本酒店')
            for mode, fields in (
                ('app_reviews', {'object_ref': 'restaurant-43'}),
                ('app_reviews', {'object_ref': '../restaurant-42'}),
                ('app_reviews', {'object_ref': 42, 'name': '样本餐厅', 'resource_id': '42'}),
                ('app_reviews', {'object_ref': 'restaurant-42', 'name': '样本餐厅', 'resource_id': '42'}),
                ('app_reviews', {'object_ref': 'restaurant-42', 'kind': 'hotel'}),
                ('restaurant_review_gallery', {'object_ref': 'hotel-42'}),
                ('restaurant_review_gallery', {'object_ref': 'restaurant-42', 'kind': 'hotel'}),
                ('restaurant_review_gallery', {'object_ref': 'restaurant-42', 'name': '样本餐厅'}),
            ):
                with self.subTest(mode=mode, fields=fields), self.assertRaises(ValueError):
                    make_job({'mode': mode, **fields}, folder, 'adb.exe')
            review_adb.assert_not_called()
            gallery_adb.assert_not_called()

    def test_review_and_photo_jobs_use_the_saved_identity(self):
        stop, emit = Event(), Mock()
        with TemporaryDirectory() as folder, patch('workbench_app_jobs.Adb') as review_adb, \
                patch('workbench_jobs.Adb') as gallery_adb, patch('workbench_app_jobs.collect') as collect, \
                patch('restaurant_review_gallery.capture_restaurant_review_gallery') as gallery:
            evidence = write_snapshot(folder, 'restaurant', '42', '样本餐厅')
            saved_object = {'object_ref': 'restaurant-42', 'name': '', 'resource_id': '',
                            'identity_source': 'screen_verified_id', 'identity_evidence': 'client.json'}
            review_job = make_job({'mode': 'app_reviews', 'limit': 3, **saved_object}, folder, 'adb.exe')
            gallery_job = make_job({'mode': 'restaurant_review_gallery', 'kind': 'restaurant', 'max_pages': 2,
                                    **saved_object}, folder, 'adb.exe')
            review_job(stop, emit)
            gallery_job(stop, emit)
            local_identity = {'identity_source': 'evidence_manifest', 'identity_evidence': evidence.name}
            review_device = review_adb.return_value.__enter__.return_value
            gallery_device = gallery_adb.return_value.__enter__.return_value
            collect.assert_called_once_with(review_device, 'restaurant', '样本餐厅', Path(folder).resolve(),
                                            3, 1500, stop, emit, object_id='42', **local_identity)
            gallery.assert_called_once_with(gallery_device, Path(folder).resolve(), '样本餐厅', '42', stop,
                                            max_pages=2, emit=emit, **local_identity)
