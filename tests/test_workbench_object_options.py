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


def write_snapshot(folder, kind, object_id, name, **extra):
    path = Path(folder) / f'{kind}-{object_id}-object.json'
    path.write_text(json.dumps({'kind': kind, 'resource_id': object_id, 'name': name, **extra}), encoding='utf-8')
    return path


class WorkbenchObjectOptionsTests(unittest.TestCase):
    def test_auto_open_sight_uses_local_address_before_collecting(self):
        stop, emit, calls = Event(), Mock(), Mock()
        address = '白山市抚松县松江河镇松天线'
        with TemporaryDirectory() as folder, patch('workbench_app_jobs.Adb') as adb, \
                patch('workbench_app_jobs.open_sight_reviews', calls.open), \
                patch('workbench_app_jobs.collect', calls.collect):
            evidence = write_snapshot(folder, 'sight', '136039', '长白山西景区', details={'address': address})
            calls.open.return_value = 'opened'
            request = {'mode': 'app_reviews', 'object_ref': 'sight-136039', 'kind': 'sight',
                       'auto_open_sight': True, 'address': '用户请求中的地址不作为证据', 'limit': 2}
            job = make_job(request, folder, 'adb.exe', adb_serial='emulator-5556')
            job(stop, emit)
            device = adb.return_value.__enter__.return_value
            calls.open.assert_called_once_with(device, '长白山西景区', address, stop)
            calls.collect.assert_called_once_with(device, 'sight', '长白山西景区', Path(folder).resolve(),
                                                 2, 1500, stop, emit, object_id='136039',
                                                 identity_source='evidence_manifest',
                                                 identity_evidence=evidence.name, sight_navigation='opened')
            self.assertEqual([value[0] for value in calls.mock_calls], ['open', 'collect'])
            calls.open.side_effect = ValueError('场景不匹配')
            calls.collect.reset_mock()
            with self.assertRaisesRegex(ValueError, '场景不匹配'):
                job(stop, emit)
            calls.collect.assert_not_called()

    def test_auto_open_sight_rejects_missing_or_conflicting_local_address(self):
        base = {'mode': 'app_reviews', 'kind': 'sight', 'auto_open_sight': True}
        with TemporaryDirectory() as folder, patch('workbench_app_jobs.Adb') as adb:
            write_snapshot(folder, 'sight', '41', '缺少地址景点')
            write_snapshot(folder, 'sight', '42', '地址冲突景点', details={'address': '模拟大道100号'},
                           comment_info={'address': '模拟大道200号'})
            for fields in ({'object_ref': 'sight-41'}, {'object_ref': 'sight-42'},
                           {'name': '缺少地址景点', 'resource_id': '41'},
                           {'object_ref': 'sight-41', 'auto_open_sight': 'yes'}):
                with self.subTest(fields=fields), self.assertRaises(ValueError):
                    make_job({**base, **fields}, folder, 'adb.exe')
            adb.assert_not_called()

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
