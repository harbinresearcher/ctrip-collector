import csv
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

from core import parse_reviews
from workbench_app_jobs import prepare_app_job
from workbench_preview import read_scoped_mobile_preview
from workbench_result_files import open_task_csv


NAME = '比斯特上海购物村'


def review_screen(review_date, body, *, title=NAME, first=False, review_footer=True):
    hierarchy = ET.Element('hierarchy')

    def node(parent, cls, bounds, text='', **fields):
        return ET.SubElement(parent, 'node', {'class': cls, 'bounds': bounds, 'text': text,
                                               'package': 'ctrip.android.view', **fields})

    window = node(hierarchy, 'android.widget.FrameLayout', '[0,0][1080,2400]')
    page = node(window, 'android.view.ViewGroup', '[0,128][1080,2337]')
    node(page, 'android.view.ViewGroup', '[0,128][1080,2337]')
    toolbar = node(page, 'android.view.ViewGroup', '[0,128][1080,259]')
    node(toolbar, 'android.view.ViewGroup', '[0,136][116,251]', **{'content-desc': '\ue943'})
    node(toolbar, 'android.widget.TextView', '[495,142][585,203]', '点评')
    node(toolbar, 'android.widget.TextView', '[412,203][668,246]', title)
    node(toolbar, 'android.view.ViewGroup', '[965,163][1080,224]', **{'content-desc': '\ue939'})
    node(page, 'android.view.ViewGroup', '[0,259][1080,2208]')
    scroll = node(page, 'android.widget.ScrollView', '[0,259][1080,2208]', scrollable='true')
    layer = node(scroll, 'android.view.ViewGroup', '[0,259][1080,2208]')
    footer = node(page, 'android.view.ViewGroup', '[0,2208][1080,2337]')
    node(footer, 'android.widget.TextView', '[460,2230][620,2275]', '写点评' if review_footer else '其他页')
    if first:
        overview = node(layer, 'android.view.ViewGroup', '[0,259][1080,598]')
        node(overview, 'android.widget.TextView', '[42,295][177,356]', '总评分')
        node(overview, 'android.widget.TextView', '[187,290][262,361]', '4.7')
        for left, label in ((325, '商品 4.7'), (492, '环境 4.6'), (658, '服务 4.7')):
            node(overview, 'android.widget.TextView', f'[{left},301][{left+135},352]', label)
        for left, label in ((42, '全部 703'), (230, '好评 481'), (425, '消费后评价 246')):
            group = node(overview, 'android.view.ViewGroup', f'[{left},394][{left+180},468]',
                         **{'content-desc': label})
            node(group, 'android.widget.TextView', f'[{left+20},409][{left+160},452]', label)
    node(layer, 'android.view.ViewGroup', '[0,706][1080,1131]')
    avatar = node(layer, 'android.view.ViewGroup', '[42,738][137,841]')
    node(avatar, 'android.widget.ImageView', '[42,738][137,833]')
    node(layer, 'android.widget.TextView', '[158,738][343,791]', '匿名测试')
    node(layer, 'android.widget.TextView', '[213,796][258,842]', '4分')
    for left, label in ((363, '商品4'), (458, '环境5'), (553, '服务4')):
        node(layer, 'android.widget.TextView', f'[{left},804][{left+74},833]', label)
    body_group = node(layer, 'android.view.ViewGroup', '[0,840][1080,933]')
    node(body_group, 'android.widget.TextView', '[42,872][1038,933]', body)
    node(layer, 'android.widget.TextView', '[42,1037][225,1080]', review_date)
    votes = node(layer, 'android.view.ViewGroup', '[771,1022][915,1095]')
    node(votes, 'android.widget.TextView', '[793,1023][845,1095]', '\ue956 ')
    node(votes, 'android.widget.TextView', '[854,1023][872,1095]', '9')
    node(layer, 'android.view.ViewGroup', '[915,1022][1017,1095]', **{'content-desc': '\ue947'})
    return ET.tostring(hierarchy, encoding='unicode')


class ShoppingAppReviewsTests(unittest.TestCase):
    def test_card_missing_its_date_cannot_borrow_a_date_from_another_card(self):
        first = ET.fromstring(review_screen('2024-04-21', '日期不可见。'))
        first_layer = next(node[0] for node in first.iter('node') if node.get('class') == 'android.widget.ScrollView')
        first_layer.remove(next(node for node in first_layer if node.get('text') == '2024-04-21'))
        second = ET.fromstring(review_screen('2023-05-13', '第二张卡片。'))
        second_layer = next(node[0] for node in second.iter('node') if node.get('class') == 'android.widget.ScrollView')
        first_layer.extend(list(second_layer))
        rows = parse_reviews(ET.tostring(first, encoding='unicode'), 'shopping')
        self.assertEqual([(row['date'], row['content']) for row in rows], [('2023-05-13', '第二张卡片。')])

    def test_scoped_shopping_csv_exports_cards_from_first_and_scrolled_pages(self):
        with TemporaryDirectory() as folder, patch('workbench_app_jobs.Adb') as factory:
            device = factory.return_value.__enter__.return_value
            device.snapshot.side_effect = [
                review_screen('2024-04-21', '购物测试点评一。', first=True),
                review_screen('2023-05-13', '购物测试点评二。'),
                review_screen('2023-05-14', '其他地点的点评。', title='其他地点'),
            ]
            stop = MagicMock()
            stop.is_set.return_value = stop.wait.return_value = False
            job = prepare_app_job({'mode': 'app_reviews', 'kind': 'shopping', 'name': NAME,
                                   'resource_id': '4444', 'limit': 20, 'max_swipes': 2}, folder, 'adb')
            report = job(stop, lambda _: None)
            self.assertEqual(report['reason'], 'identity_unverified')
            self.assertEqual(report['identity_checked_screens'], 2)
            self.assertEqual(report['count'], 2)
            self.assertEqual(report['kind'], 'shopping')
            self.assertFalse(report['identity_id_verified'])
            self.assertFalse(report['complete'])
            self.assertEqual(device.run.call_count, 2)
            self.assertEqual(len(list((Path(folder) / 'raw' / report['run_id']).glob('*.xml'))), 2)
            self.assertIn({'label': '全部', 'display_count': '703'},
                          report['sampling_context']['observations'][0]['visible_filters'])
            self.assertEqual(report['sampling_context']['observations'][0]['shopping_review_overview'], {
                'overall_score': 4.7, 'rating_dimensions': {'商品': 4.7, '环境': 4.6, '服务': 4.7},
            })
            for sample in (False, True):
                with open_task_csv(folder, {'status': 'error', 'result': report}, sample=sample) as stream:
                    rows = list(csv.DictReader(io.StringIO(stream.read().decode('utf-8-sig'))))
                self.assertEqual([row['date'] for row in rows], ['2024-04-21', '2023-05-13'])
                self.assertEqual([row['rating'] for row in rows], ['4.0', '4.0'])
                self.assertTrue(all(row['shopping_environment_score'] == '5' for row in rows))
                self.assertTrue(all(row['shopping_reaction_count_display'] == '9' for row in rows))
                self.assertTrue(all(not row['helpful_count_display'] for row in rows))
            items = read_scoped_mobile_preview(folder)['items']
            self.assertEqual([item['kind'] for item in items], ['shopping', 'shopping'])
            self.assertEqual(items[0]['card_fields']['shopping_product_score'], '4')

    def test_same_title_without_review_footer_does_not_move_or_save(self):
        with TemporaryDirectory() as folder, patch('workbench_app_jobs.Adb') as factory:
            device = factory.return_value.__enter__.return_value
            device.snapshot.return_value = review_screen('2024-04-21', '正文相似。', review_footer=False)
            stop = MagicMock()
            stop.is_set.return_value = stop.wait.return_value = False
            job = prepare_app_job({'mode': 'app_reviews', 'kind': 'shopping', 'name': NAME,
                                   'resource_id': '4444', 'limit': 20, 'max_swipes': 2}, folder, 'adb')
            report = job(stop, lambda _: None)
            self.assertEqual(report['reason'], 'identity_unverified')
            self.assertEqual(report['count'], 0)
            self.assertEqual(report['identity_checked_screens'], 0)
            device.run.assert_not_called()
