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

    def test_text_only_rows_keep_titles_between_the_fixed_header_and_add_button(self):
        xml = (FIXTURES / 'app-restaurant-menu-text-rows.xml').read_text('utf-8')
        total, names, area = parse_restaurant_menu(xml)
        self.assertEqual(total, 29)
        self.assertEqual(names, [
            'M5和牛腰肉', '草莓冰霜', '法芙娜巧克力蛋糕', '青瓜沙冰配青苹果', 'M7和牛',
            '福建多宝鱼', '莫纳生蚝', '生和牛塔塔150g', '甜品车',
        ])
        self.assertEqual(area, (0, 244, 1080, 2226))

    def test_duplicate_dish_names_remain_separate_rows(self):
        xml = FIRST.replace('脆烤章鱼', '黑松露披萨')
        self.assertEqual(parse_restaurant_menu(xml)[1], [DISHES[0], DISHES[0], *DISHES[2:7]])

    def test_zero_sized_sibling_inside_the_webview_keeps_the_layout(self):
        # 真机 dump 里标题 WebView 下多一个 [0,0][0,0] 的隐藏节点，不能当作布局变化。
        tree = ET.fromstring(FIRST)
        webview = tree.find(".//node[@class='android.webkit.WebView'][@text='推荐菜']")
        hidden = ET.SubElement(webview, 'node')
        hidden.set('package', 'ctrip.android.view')
        hidden.set('class', 'android.view.View')
        hidden.set('bounds', '[0,0][0,0]')
        ET.SubElement(hidden, 'node').set('bounds', '[0,0][0,0]')
        self.assertEqual(parse_restaurant_menu(ET.tostring(tree, 'unicode'))[0], 8)
        # 布局子节点全部不可见时仍然拒绝，不凭猜测选一个。
        for node in webview.findall("node[@class='android.view.View']"):
            node.set('bounds', '[0,0][0,0]')
        with self.assertRaises(ValueError):
            parse_restaurant_menu(ET.tostring(tree, 'unicode'))

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

