import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from workbench_jobs import make_job


XML = (Path(__file__).parent / 'fixtures' / 'hotel-search.xml').read_text('utf-8')
LOCATION = '抚松(白山)'


class AppHotelListTests(unittest.TestCase):
    def run_search(self, folder, screens, *, location=LOCATION, pages=1):
        with patch('workbench_jobs.Adb') as factory:
            device = factory.return_value.__enter__.return_value
            device.snapshot.side_effect = screens
            job = make_job({'mode': 'app_hotel_list', 'name': location, 'hotel_max_pages': pages}, folder, 'adb')
            result = job(Event(), lambda _: None)
            return result, device.run.call_args_list

    def test_historical_search_saves_matching_hotels_and_query(self):
        with TemporaryDirectory() as folder:
            actual, gestures = self.run_search(folder, [XML])
            self.assertEqual(actual['query']['location'], '抚松(白山).')
            self.assertEqual(actual['query']['stay_dates'], ['10-18', '10-19'])
            self.assertEqual(actual['query']['room_guest_label'], '1间1人')
            self.assertEqual(actual['query']['visible_filters'][0], '欢迎度排序')
            self.assertEqual(len(actual['hotels']), 2)
            self.assertEqual(actual['screens'][0]['advertised_cards'], 1)
            first, second = actual['hotels']
            self.assertEqual(first['name'], '长白山万达锦华度假酒店')
            self.assertEqual(first['starting_price_display'], '¥489起')
            self.assertEqual(second['starting_price_display'], '¥592起')
            self.assertEqual(first['review_count_display'], '5722点评')
            self.assertEqual(first['favorites_count_display'], '4.3万收藏')
            self.assertEqual(first['discount_display'], '优惠37')
            self.assertEqual(first['screens_seen'], [1])
            self.assertFalse(actual['complete'])
            self.assertFalse(gestures)
            self.assertEqual(json.loads(Path(actual['report_file']).read_text('utf-8')), actual)
            coverage = make_job({'mode': 'coverage'}, folder)(Event(), lambda _: None)['android']
            self.assertEqual(coverage['hotel_lists'][0]['display_record_count'], 2)

    def test_different_query_stops_and_preserves_first_screen(self):
        with TemporaryDirectory() as folder:
            next_date = XML.replace('text="10-19"', 'text="10-20"', 1)
            actual, gestures = self.run_search(folder, [XML, next_date], pages=3)
            self.assertEqual(actual['reason'], 'query_changed')
            self.assertEqual(len(actual['screens']), 1)
            self.assertEqual(len(gestures), 1)
            self.assertEqual(len(json.loads(Path(actual['report_file']).read_text('utf-8'))['screens']), 1)

    def test_overlapping_cards_merge_only_identical_observations(self):
        with TemporaryDirectory() as folder:
            next_screen = XML.replace('text="592"', 'text="593"', 1)
            actual, gestures = self.run_search(folder, [XML, next_screen], pages=2)
            self.assertEqual(len(gestures), 1)
            self.assertEqual(len(actual['screens']), 2)
            self.assertEqual([row['starting_price_display'] for row in actual['hotels']], ['¥489起', '¥592起', '¥593起'])
            self.assertEqual(actual['hotels'][0]['screens_seen'], [1, 2])

    def test_requires_matching_location_and_hotel_list_marker(self):
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, '地区'):
                self.run_search(folder, [XML], location='别的城市')
            with self.assertRaisesRegex(ValueError, '列表'):
                self.run_search(folder, [XML.replace('hotel_list_page_view_root', 'another_page')])
            self.assertFalse(list(Path(folder).iterdir()))
