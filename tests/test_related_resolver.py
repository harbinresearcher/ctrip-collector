"""餐饮推荐 POI 解析测试。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from related_resolver import resolve_related_restaurants

SOURCE = 'https://you.ctrip.com/yougourmet/restdetail/datong275/108786.html'
REPORT_NAME = 'related-restaurant-' + 'a' * 32 + '.json'


def create_report(directory):
    report = {'source_method': 'public_restaurant_page_recommendations', 'reason': 'recommendations_read',
              'source': {'kind': 'restaurant', 'url': SOURCE, 'poi_id': 108786, 'resource_id': 8639, 'resource_type': 12},
              'objects': [{'kind': 'restaurant', 'poi_id': 108712},
                          {'kind': 'hotel', 'hotel_id': 1},
                          {'kind': 'restaurant', 'poi_id': 15811413}]}
    Path(directory, REPORT_NAME).write_text(json.dumps(report), encoding='utf-8')


def target(url, poi_id, resource_id):
    return {'url': url, 'kind': 'restaurant', 'poi_id': poi_id, 'resource_id': resource_id,
            'resource_type': 12, 'name': '餐厅', 'display_total': 5}


class FakeStop:
    def __init__(self):
        self.stopped = False
        self.waited = []

    def is_set(self):
        return self.stopped

    def wait(self, duration):
        self.waited.append(duration)
        return self.stopped


class RelatedResolverTests(unittest.TestCase):
    def test_only_identity_checked_targets_enter_objects_and_rate_limited(self):
        # 1.1 每个候选必须由详情页确认 POI 和独立点评 ID。
        with TemporaryDirectory() as directory:
            create_report(directory)
            stop = FakeStop()
            def fetch(url):
                identity = int(url.rsplit('/', 1)[1].split('.')[0])
                return target(url, identity, identity + 100)
            with patch('related_resolver.load_target', side_effect=fetch) as loader:
                report = resolve_related_restaurants(REPORT_NAME, directory, stop=stop)
            self.assertEqual(loader.call_count, 2)
            self.assertEqual(stop.waited, [1.5])
            self.assertFalse(report['complete'])
            self.assertEqual(report['reason'], 'candidates_processed')
            self.assertEqual([x['resource_id'] for x in report['objects']], [108812, 15811513])
            self.assertEqual([x['status'] for x in report['results']], ['verified', 'verified'])
            self.assertEqual(json.loads(Path(report['report_file']).read_text(encoding='utf-8')), report)

    def test_identity_failure_stops_and_never_publishes_candidate(self):
        # 1.2 即便请求成功，POI 不匹配也不能当成可采链接。
        with TemporaryDirectory() as directory:
            create_report(directory)
            stop = FakeStop()
            def fetch(url):
                identity = int(url.rsplit('/', 1)[1].split('.')[0])
                return target(url, identity if identity == 108712 else 999, 1)
            with patch('related_resolver.load_target', side_effect=fetch):
                report = resolve_related_restaurants(REPORT_NAME, directory, stop=stop)
            self.assertEqual(report['reason'], 'error')
            self.assertEqual(len(report['objects']), 1)
            self.assertEqual(report['results'][1]['status'], 'error')
            self.assertNotIn('url', report['results'][1])

    def test_cancel_during_fetch_never_publishes_inflight_result(self):
        with TemporaryDirectory() as directory:
            create_report(directory)
            stop = Event()
            def fetch(url):
                stop.set()
                return target(url, 108712, 1)
            with patch('related_resolver.load_target', side_effect=fetch):
                report = resolve_related_restaurants(REPORT_NAME, directory, stop=stop)
            self.assertEqual(report['reason'], 'cancelled')
            self.assertEqual(report['objects'], [])
            self.assertEqual(report['results'][0]['status'], 'cancelled')

    def test_rejects_paths_or_wrong_source_before_request(self):
        with TemporaryDirectory() as directory, patch('related_resolver.load_target') as loader:
            create_report(directory)
            with self.assertRaises(ValueError):
                resolve_related_restaurants('../' + REPORT_NAME, directory)
            report = json.loads(Path(directory, REPORT_NAME).read_text(encoding='utf-8'))
            report['source']['poi_id'] = 1
            Path(directory, REPORT_NAME).write_text(json.dumps(report), encoding='utf-8')
            with self.assertRaises(ValueError):
                resolve_related_restaurants(REPORT_NAME, directory)
            loader.assert_not_called()

    def test_partial_report_requires_restaurant_list_available(self):
        with TemporaryDirectory() as directory, patch('related_resolver.load_target') as loader:
            create_report(directory)
            path = Path(directory, REPORT_NAME)
            original = json.loads(path.read_text(encoding='utf-8'))
            original['reason'] = 'recommendations_partial'
            original['list_status'] = {'nearbyHotelsList': 'missing', 'moreRestaurantList': 'available'}
            path.write_text(json.dumps(original), encoding='utf-8')
            def fetch(url):
                return target(url, int(url.rsplit('/', 1)[1].split('.')[0]), 1)
            loader.side_effect = fetch
            report = resolve_related_restaurants(REPORT_NAME, directory, stop=FakeStop())
            self.assertEqual(report['reason'], 'candidates_processed')
            original['list_status']['moreRestaurantList'] = 'missing'
            path.write_text(json.dumps(original), encoding='utf-8')
            with self.assertRaises(ValueError):
                resolve_related_restaurants(REPORT_NAME, directory)

    def test_budget_and_failure_leave_later_candidates_unrequested(self):
        # 1.3 预算和失败不应让未访问的 POI 混入可采清单。
        with TemporaryDirectory() as directory:
            create_report(directory)
            path = Path(directory, REPORT_NAME)
            original = json.loads(path.read_text(encoding='utf-8'))
            original['objects'].append({'kind': 'restaurant', 'poi_id': 25796255})
            path.write_text(json.dumps(original), encoding='utf-8')
            def fetch(url):
                identity = int(url.rsplit('/', 1)[1].split('.')[0])
                if identity == 15811413:
                    raise OSError('request failed')
                return target(url, identity, 1)
            with patch('related_resolver.load_target', side_effect=fetch) as loader:
                report = resolve_related_restaurants(REPORT_NAME, directory, stop=FakeStop())
            self.assertEqual(loader.call_count, 2)
            self.assertEqual([x['status'] for x in report['results']], ['verified', 'error', 'not_started'])
            self.assertEqual([x['poi_id'] for x in report['objects']], [108712])
            with patch('related_resolver.load_target', side_effect=fetch) as loader:
                report = resolve_related_restaurants(REPORT_NAME, directory, stop=FakeStop(), max_objects=1)
            self.assertEqual(loader.call_count, 1)
            self.assertEqual(report['reason'], 'candidate_limit_reached')
            self.assertEqual(report['remaining_candidates'], 2)


if __name__ == '__main__':
    unittest.main()
