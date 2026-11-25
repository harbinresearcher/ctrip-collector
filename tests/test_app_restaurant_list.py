import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from app_restaurant_list import capture_restaurant_list


XML = (Path(__file__).parent / 'fixtures' / 'restaurant-list.xml').read_text('utf-8')
NAME = 'Mercato by Jean-Georges意大利餐厅'


class AppRestaurantListTests(unittest.TestCase):
    def capture(self, folder, screens, *, city='上海', max_pages=1):
        device = Mock()
        device.snapshot.side_effect = screens
        stop = Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False))
        result = capture_restaurant_list(device, folder, city, stop, max_pages=max_pages)
        return result, device

    def test_live_layout_preserves_paired_list_fields_and_partial_result(self):
        with TemporaryDirectory() as folder:
            result, device = self.capture(folder, [XML])
            self.assertEqual(result['source_method'], 'android_restaurant_list_snapshot')
            self.assertEqual(result['query']['city'], '上海')
            self.assertEqual(result['status'], 'partial')
            self.assertFalse(result['complete'])
            self.assertEqual(len(result['restaurants']), 4)
            first = result['restaurants'][0]
            self.assertEqual(first, {'name': NAME, 'score_display': '4.3', 'review_count_display': '470条',
                                     'per_person_price_display': '¥600/人', 'opening_status_display': '17:30营业',
                                     'cuisine_display': '西餐', 'nearby_display': '近外滩', 'location_display': '外滩',
                                     'ranking_display': '上海美食必打卡榜 No.17', 'screens_seen': [1]})
            self.assertNotIn('opening_status_display', result['restaurants'][2])
            self.assertEqual(set(result['restaurants'][3]), {'name', 'screens_seen'})
            self.assertEqual(json.loads(Path(result['report_file']).read_text('utf-8')), result)
            device.run.assert_not_called()

    def test_city_must_match_the_toolbar_and_cards_must_be_in_the_list(self):
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, '城市'):
                self.capture(folder, [XML.replace('text="上海"', 'text="杭州"')])
            with self.assertRaisesRegex(ValueError, '餐厅列表'):
                self.capture(folder, [XML.replace('o_gs_gourmethome_card', 'other_card')])
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_multi_screen_deduplicates_overlaps_and_preserves_verified_screens(self):
        second = XML.replace('470条', '471条')
        changed = second.replace('智能排序', '人气排序')
        with TemporaryDirectory() as folder:
            result, device = self.capture(folder, [XML, second, changed], max_pages=3)
            self.assertEqual(result['reason'], 'query_changed')
            self.assertEqual(len(result['screens']), 2)
            self.assertEqual([card['review_count_display'] for card in result['restaurants'] if card['name'] == NAME],
                             ['470条', '471条'])
            self.assertEqual(result['restaurants'][1]['screens_seen'], [1, 2])
            self.assertEqual([call.args[2] for call in device.run.call_args_list], ['swipe', 'swipe'])

    def test_stop_during_scroll_preserves_the_first_screen(self):
        device = Mock()
        device.snapshot.return_value = XML
        stop = Mock(is_set=Mock(return_value=False), wait=Mock(return_value=True))
        with TemporaryDirectory() as folder:
            result = capture_restaurant_list(device, folder, '上海', stop, max_pages=2)
            self.assertEqual(result['reason'], 'cancelled')
            self.assertEqual(len(result['screens']), 1)
            self.assertEqual(json.loads(Path(result['report_file']).read_text('utf-8')), result)
            device.snapshot.assert_called_once_with()
