import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from app_restaurant_detail import capture_restaurant_detail, parse_restaurant_detail


XML = (Path(__file__).parent / 'fixtures' / 'app-restaurant-detail.xml').read_text('utf-8')
NAME = 'Mercato by Jean-Georges意大利餐厅'


class AppRestaurantDetailTests(unittest.TestCase):
    def test_visible_profile_matches_the_detail_and_public_booking_preview(self):
        report = parse_restaurant_detail(XML, NAME)
        self.assertEqual(report['score_display'], '4.3')
        self.assertEqual(report['review_count_display'], '470条点评')
        self.assertEqual(report['per_person_price_display'], '人均¥600')
        self.assertEqual(report['cuisine_display'], '西餐')
        self.assertEqual(report['address_display'], '中山东一路3号外滩三号6楼')
        self.assertEqual(report['opening_hours_display'], '17:30-22:00')
        self.assertEqual(report['features']['餐厅特色'], '江景')
        self.assertEqual(report['ranking_display'], '上海美食必打卡榜 No.17')
        self.assertEqual(report['dish_count_display'], 8)
        self.assertEqual(report['recommended_dishes'], ['黑松露披萨', '脆烤章鱼', '木炭烤加拿大龙虾'])
        self.assertEqual(report['booking_preview']['time_slots_display'], ['17:30', '18:00', '18:30'])
        self.assertEqual(report['booking_preview']['party_size_display'], '2 人用餐')
        self.assertFalse(report['complete'])

    def test_wrong_title_or_nonrestaurant_scene_is_rejected_before_export(self):
        with self.assertRaisesRegex(ValueError, '名称'):
            parse_restaurant_detail(XML, '另一家餐厅')
        changed = XML.replace('gm_food_detail_scroll', 'other_scroll')
        with self.assertRaisesRegex(ValueError, '详情'):
            parse_restaurant_detail(changed, NAME)

    def test_capture_is_read_only_and_json_roundtrips(self):
        device = Mock()
        device.snapshot.return_value = XML
        with TemporaryDirectory() as output:
            report = capture_restaurant_detail(device, output, NAME)
            self.assertEqual(json.loads(Path(report['file']).read_text('utf-8')), report)
        self.assertEqual(device.ensure_foreground.call_count, 2)
        device.run.assert_not_called()
