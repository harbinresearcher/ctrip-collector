import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from app_restaurant_menu import parse_restaurant_menu


FIXTURES = Path(__file__).parent / 'fixtures'
FIRST, LAST = [ET.tostring(frame, encoding='unicode') for frame in
               ET.parse(FIXTURES / 'app-restaurant-menu-pages.xml').getroot()]
DISHES = ['黑松露披萨', '脆烤章鱼', '木炭烤加拿大龙虾', '带骨M6+和牛肉眼牛排',
          '木炭烤鲜活石斑鱼', '自制乳清芝士配时令果酱', '温海鲜色拉配牛油果', '牛肝菌裹三文鱼']


class RestaurantMenuTests(unittest.TestCase):
    def test_first_and_last_frames_preserve_visible_dish_order(self):
        self.assertEqual(parse_restaurant_menu(FIRST), (8, DISHES[:7], (0, 244, 1080, 2226)))
        self.assertEqual(parse_restaurant_menu(LAST), (8, DISHES[2:], None))

    def test_hidden_card_name_does_not_promote_description(self):
        tree = ET.fromstring(FIRST)
        name = tree.find(".//node[@text='温海鲜色拉配牛油果']")
        name.set('bounds', '[325,2168][1050,2168]')
        self.assertEqual(parse_restaurant_menu(ET.tostring(tree, 'unicode'))[1], DISHES[:6])

    def test_duplicate_dish_names_remain_separate_rows(self):
        xml = FIRST.replace('脆烤章鱼', '黑松露披萨')
        self.assertEqual(parse_restaurant_menu(xml)[1], [DISHES[0], DISHES[0], *DISHES[2:7]])

    def test_malformed_xml_and_unproven_layout_are_rejected(self):
        for xml in (
            '<hierarchy',
            (FIXTURES / 'app-restaurant-detail.xml').read_text('utf-8'),
            FIRST.replace('text="(8)"', 'text="(0)"'),
            FIRST.replace('text="推荐菜"', 'text="美食"'),
            FIRST.replace('ctrip.android.view', 'other.app'),
        ):
            with self.subTest(frame=xml[:45]), self.assertRaises(ValueError):
                parse_restaurant_menu(xml)

