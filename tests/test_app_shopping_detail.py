import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from app_shopping_detail import capture_shopping_detail, parse_shopping_detail


XML = (Path(__file__).parent / 'fixtures' / 'app-shopping-detail.xml').read_text('utf-8')
NAME = '比斯特上海购物村'


class ShoppingDetailTests(unittest.TestCase):
    def test_fields_stay_under_the_confirmed_public_shop_header(self):
        result = parse_shopping_detail(XML, NAME)
        self.assertEqual(result['score_display'], '4.7')
        self.assertEqual(result['review_count_display'], '683条点评')
        self.assertEqual(result['opening_status_display'], '营业中')
        self.assertEqual(result['opening_hours_display'], '10:00-22:00开放')
        self.assertEqual(result['category_labels'], ['奥特莱斯', '香水', '时装'])
        self.assertEqual(result['address_display'], '上海市浦东新区申迪东路88号')
        self.assertEqual(result['nearby_display'], '距国际旅游度假区1路区间购物村东站369m')
        self.assertIn('优享购', result['badges'])
        self.assertIn('100元现金券等多重权益', result['offers'])
        self.assertFalse(result['complete'])

    def test_rejects_other_shop_and_missing_detail_markers(self):
        with self.assertRaisesRegex(ValueError, '名称'):
            parse_shopping_detail(XML, '另一家商场')
        with self.assertRaisesRegex(ValueError, '详情'):
            parse_shopping_detail(XML.replace('shopPOI_headPictureModule_exp', 'other_app_hero'), NAME)

    def test_job_saves_json_and_leaves_device_untouched(self):
        device = Mock()
        device.snapshot.return_value = XML
        with TemporaryDirectory() as folder:
            result = capture_shopping_detail(device, folder, NAME)
            self.assertEqual(json.loads(Path(result['file']).read_text('utf-8')), result)
        self.assertEqual(device.ensure_foreground.call_count, 2)
        device.run.assert_not_called()
