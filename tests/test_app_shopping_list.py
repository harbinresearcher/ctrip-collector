import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from app_shopping_list import capture_shopping_list


XML = (Path(__file__).parent / 'fixtures' / 'app-shopping-list.xml').read_text('utf-8')
SCROLLED_XML = (Path(__file__).parent / 'fixtures' / 'app-shopping-list-scrolled.xml').read_text('utf-8')
NAME = '比斯特上海购物村'


class AppShoppingListTests(unittest.TestCase):
    def capture(self, folder, screens, *, city='上海', max_pages=1, stop=None):
        device = Mock()
        device.snapshot.side_effect = screens
        stop = stop or Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False))
        return capture_shopping_list(device, folder, city, stop, max_pages=max_pages), device

    def test_live_layout_preserves_visible_cards_and_paired_offers(self):
        with TemporaryDirectory() as folder:
            result, device = self.capture(folder, [XML])
            self.assertEqual(result['kind'], 'shopping')
            self.assertEqual(result['source_method'], 'android_shopping_list_snapshot')
            self.assertEqual(result['city'], '上海')
            self.assertIn('商户类型', result['query']['visible_filters'])
            self.assertFalse(result['complete'])
            self.assertEqual(result['screens'][0]['shop_cards'], 4)
            self.assertEqual(result['shops'][0], {
                'name': NAME, 'english_name': 'Shanghai Village',
                'tagline_display': '全年享折扣优惠，4A级景区', 'category_display': '奥特莱斯·香水·时装',
                'score_display': '4.7', 'review_count_display': '683条点评',
                'location_display': '距市中心22.0km·迪士尼度假区',
                'offers': ['100元现金券等多重权益'], 'screens_seen': [1],
            })
            self.assertEqual(result['shops'][2]['offers'], ['【零售卡】50元无门槛券',
                             '【餐饮卡】满59减10元券+满99减20元券+消费赠礼'])
            self.assertEqual(result['shops'][3], {'name': '百联奥特莱斯广场（上海·青浦）', 'screens_seen': [1]})
            self.assertEqual(json.loads(Path(result['report_file']).read_text('utf-8')), result)
            device.run.assert_not_called()

    def test_only_exact_city_and_shopping_filter_anchor_can_start_a_report(self):
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, '城市'):
                self.capture(folder, [XML], city='杭州')
            with self.assertRaisesRegex(ValueError, '购物列表'):
                self.capture(folder, [XML.replace('resource-id="261342"', 'resource-id="wrong"')])
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_later_filter_change_preserves_first_two_screens_and_overlap(self):
        second = XML.replace('683条点评', '684条点评')
        third = second.replace('智能排序', '点评最多')
        with TemporaryDirectory() as folder:
            result, device = self.capture(folder, [XML, second, third], max_pages=3)
            self.assertEqual(result['reason'], 'query_changed')
            self.assertEqual(len(result['screens']), 2)
            self.assertEqual([card['review_count_display'] for card in result['shops'] if card['name'] == NAME],
                             ['683条点评', '684条点评'])
            self.assertEqual(result['shops'][1]['screens_seen'], [1, 2])
            self.assertEqual([call.args[2] for call in device.run.call_args_list], ['swipe', 'swipe'])

    def test_sticky_filters_keep_new_shops_under_the_same_verified_query(self):
        with TemporaryDirectory() as folder:
            result, device = self.capture(folder, [XML, SCROLLED_XML], max_pages=2)
            self.assertEqual(result['reason'], 'page_limit')
            self.assertEqual(result['screens'][1]['shop_cards'], 4)
            self.assertEqual(result['screens'][1]['unrecognized_cards'], 1)
            second = {shop['name']: shop for shop in result['shops'] if 2 in shop['screens_seen']}
            self.assertEqual(set(second), {'百联奥特莱斯广场（上海·青浦）', '虹桥南丰城', '美罗城', '新世界城'})
            self.assertEqual(second['虹桥南丰城']['review_count_display'], '296条点评')
            self.assertEqual(second['虹桥南丰城']['category_display'], '综合商场')
            self.assertEqual(second['新世界城']['english_name'], 'New World City')
            self.assertEqual(json.loads(Path(result['report_file']).read_text('utf-8')), result)
            device.run.assert_called_once()

    def test_cancel_after_first_saved_screen_does_not_read_second_screen(self):
        stop = Mock(is_set=Mock(return_value=False), wait=Mock(return_value=True))
        with TemporaryDirectory() as folder:
            result, device = self.capture(folder, [XML], max_pages=2, stop=stop)
            self.assertEqual(result['reason'], 'cancelled')
            self.assertEqual(len(result['screens']), 1)
            self.assertEqual(json.loads(Path(result['report_file']).read_text('utf-8')), result)
            device.snapshot.assert_called_once_with()
