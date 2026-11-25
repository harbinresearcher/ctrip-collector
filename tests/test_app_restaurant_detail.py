import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from uiautomator2.exceptions import HTTPTimeoutError
from xml.etree import ElementTree as ET

from app_restaurant_detail import capture_restaurant_detail, parse_restaurant_detail


XML = (Path(__file__).parent / 'fixtures' / 'app-restaurant-detail.xml').read_text('utf-8')
NAME = 'Mercato by Jean-Georges意大利餐厅'
FRAMES = [ET.tostring(frame, encoding='unicode') for frame in
          ET.parse(Path(__file__).parent / 'fixtures' / 'app-restaurant-recommendations.xml').getroot()]


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


    def test_scan_merges_adjacent_carousel_screens_to_the_displayed_count(self):
        device = Mock()
        device.snapshot.side_effect = (FRAMES[0].replace('查看全部', '隐去入口'), *FRAMES[1:])
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        with TemporaryDirectory() as output:
            report = capture_restaurant_detail(device, output, NAME, recommendation_stop=stop)
            self.assertEqual(json.loads(Path(report['file']).read_text('utf-8')), report)
        self.assertEqual(report['recommended_dishes'], [
            '黑松露披萨', '脆烤章鱼', '木炭烤加拿大龙虾', '带骨M6+和牛肉眼牛排',
            '木炭烤鲜活石斑鱼', '自制乳清芝士配时令果酱', '温海鲜色拉配牛油果', '牛肝菌裹三文鱼',
        ])
        self.assertEqual(report['recommendation_scan'], {
            'reason': 'expected_count_reached', 'screens': 6, 'complete': True,
        })
        self.assertFalse(report['complete'])
        self.assertEqual(device.run.call_count, 5)
        _, _, _, left, y, right, same_y, _ = device.run.call_args.args
        self.assertTrue(35 < int(right) < int(left) < 1080)
        self.assertTrue(1867 < int(y) < 2221)
        self.assertEqual(y, same_y)

    def test_scan_saves_verified_dishes_if_next_screen_does_not_match(self):
        for reason, second in (
            ('unverified_transition', FRAMES[-1]),
            ('context_changed', FRAMES[1].replace(NAME, '另一家餐厅')),
            ('count_changed', FRAMES[1].replace('推荐菜(8)', '推荐菜(9)')),
        ):
            with self.subTest(reason=reason), TemporaryDirectory() as output:
                device = Mock()
                device.snapshot.side_effect = (FRAMES[0].replace('查看全部', '隐去入口'), second)
                stop = Mock()
                stop.is_set.return_value = False
                stop.wait.return_value = False
                result = capture_restaurant_detail(device, output, NAME, recommendation_stop=stop)
                self.assertEqual(result['recommended_dishes'], ['黑松露披萨', '脆烤章鱼', '木炭烤加拿大龙虾'])
                self.assertEqual(result['recommendation_scan'], {'reason': reason, 'screens': 1, 'complete': False})
                self.assertTrue(Path(result['file']).is_file())

    def test_scan_stops_on_cancel_or_an_unchanged_carousel(self):
        for cancelled, reason, screens in ((True, 'cancelled', 1), (False, 'no_new_dishes', 3)):
            with self.subTest(reason=reason), TemporaryDirectory() as output:
                device = Mock()
                device.snapshot.return_value = FRAMES[0].replace('查看全部', '隐去入口')
                stop = Mock()
                stop.is_set.return_value = False
                stop.wait.return_value = cancelled
                result = capture_restaurant_detail(device, output, NAME, recommendation_stop=stop)
                self.assertEqual(result['recommendation_scan'], {'reason': reason, 'screens': screens, 'complete': False})
                self.assertEqual(result['status'], 'cancelled' if cancelled else 'partial')

    def test_scan_without_visible_carousel_does_not_gesture(self):
        with TemporaryDirectory() as output:
            device = Mock()
            device.snapshot.return_value = FRAMES[0].replace('查看全部', '隐去入口').replace('o_gs_tuijiancai', 'no_carousel')
            stop = Mock()
            stop.is_set.return_value = False
            result = capture_restaurant_detail(device, output, NAME, recommendation_stop=stop)
        self.assertEqual(result['recommendation_scan'], {'reason': 'missing_carousel', 'screens': 0, 'complete': False})
        device.run.assert_not_called()

    def test_device_timeout_saves_the_already_verified_carousel(self):
        device = Mock()
        device.snapshot.side_effect = (FRAMES[0].replace('查看全部', '隐去入口'), HTTPTimeoutError('connection timeout'))
        stop = Mock()
        stop.is_set.return_value = stop.wait.return_value = False
        with TemporaryDirectory() as output:
            result = capture_restaurant_detail(device, output, NAME, recommendation_stop=stop)
            self.assertEqual(json.loads(Path(result['file']).read_text('utf-8')), result)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['recommendation_scan']['reason'], 'device_error')
        self.assertEqual(len(result['recommended_dishes']), 3)

    def test_scan_prefers_the_dedicated_menu_and_checks_its_prefix(self):
        menu = ET.parse(Path(__file__).parent / 'fixtures' / 'app-restaurant-menu-pages.xml').getroot()
        menu_screens = [ET.tostring(frame, encoding='unicode') for frame in menu]
        for first_menu, reason, screens in (
            (menu_screens[0], 'expected_count_reached', 2),
            (menu_screens[0].replace('脆烤章鱼', '不同菜名'), 'unverified_transition', 1),
        ):
            with self.subTest(reason=reason), TemporaryDirectory() as output:
                device = Mock()
                device.snapshot.side_effect = (FRAMES[0], first_menu, menu_screens[1])
                stop = Mock()
                stop.is_set.return_value = stop.wait.return_value = False
                report = capture_restaurant_detail(device, output, NAME, recommendation_stop=stop)
                scan = report['recommendation_scan']
                self.assertEqual((scan['reason'], scan['screens']), (reason, screens))
                device.run.assert_any_call('shell', 'input', 'tap', '940', '1553')
                self.assertEqual(len(report['recommended_dishes']), 8 if scan['complete'] else 3)
                if scan['complete']:
                    self.assertEqual(scan['method'], 'menu')

    def test_scan_falls_back_when_opening_the_menu_leaves_the_same_detail(self):
        device = Mock()
        device.snapshot.side_effect = (FRAMES[0], FRAMES[0], *FRAMES[1:])
        stop = Mock()
        stop.is_set.return_value = stop.wait.return_value = False
        with TemporaryDirectory() as output:
            report = capture_restaurant_detail(device, output, NAME, recommendation_stop=stop)
        self.assertEqual(report['recommendation_scan'], {
            'reason': 'expected_count_reached', 'screens': 6, 'complete': True,
        })
        self.assertEqual(device.run.call_count, 6)
