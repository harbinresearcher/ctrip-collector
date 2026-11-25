import csv
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

from workbench_app_jobs import prepare_app_job
from workbench_preview import read_scoped_mobile_preview
from workbench_result_files import open_task_csv


NAME = 'Mercato by Jean-Georges意大利餐厅'


def scene(review_date, body, *, title=NAME, review_filters=True, scrolled=False):
    hierarchy = ET.Element('hierarchy')

    def node(parent, clazz, bounds, text='', **attributes):
        return ET.SubElement(parent, 'node', {
            'class': clazz, 'bounds': bounds, 'text': text, 'package': 'ctrip.android.view', **attributes,
        })

    window = node(hierarchy, 'android.widget.FrameLayout', '[0,0][1080,2400]')
    page = node(window, 'android.view.ViewGroup', '[0,128][1080,2337]')
    toolbar = node(page, 'android.view.ViewGroup', '[0,128][1080,259]')
    node(toolbar, 'android.view.ViewGroup', '[0,136][116,251]', **{'content-desc': '\ue943'})
    node(toolbar, 'android.widget.TextView', '[172,163][909,224]', title)
    node(toolbar, 'android.view.ViewGroup', '[965,163][1080,224]', **{'content-desc': '\ue939'})
    node(page, 'android.view.ViewGroup', '[0,259][1080,2208]')
    scroll = node(page, 'android.widget.ScrollView', '[0,259][1080,2208]', scrollable='true')
    node(page, 'android.view.ViewGroup', '[0,2208][1080,2337]')
    if review_filters:
        top = 315 if scrolled else 661
        for left, label, count in ((32, '全部', '470'), (528, '好评', '28'), (716, '差评', '3')):
            group = node(scroll, 'android.view.ViewGroup', f'[{left},{top}][{left+152},{top+74}]',
                         **{'resource-id': 'o_gs_comments_newlist_filter1'})
            node(group, 'android.widget.TextView', f'[{left},{top}][{left+84},{top+54}]', label)
            node(group, 'android.widget.TextView', f'[{left+95},{top+6}][{left+152},{top+48}]', count)
        if not scrolled:
            sort = node(scroll, 'android.view.ViewGroup', '[32,765][167,840]',
                        **{'resource-id': 'o_gs_comments_newlist_filter2'})
            node(sort, 'android.widget.TextView', '[65,781][133,823]', '最新')
    card = node(scroll, 'android.view.ViewGroup', '[0,894][1080,1550]')
    header = node(card, 'android.view.ViewGroup', '[135,894][1046,999]')
    node(header, 'android.widget.TextView', '[135,894][295,948]', '匿名测试读者')
    node(header, 'android.widget.TextView', '[135,954][305,999]', review_date)
    node(card, 'android.widget.TextView', '[39,1022][89,1064]', '5.0')
    node(card, 'android.widget.TextView', '[34,1086][1048,1391]', body)
    return ET.tostring(hierarchy, encoding='unicode')


class RestaurantAppReviewsTests(unittest.TestCase):
    def test_review_job_exports_two_verified_screens_and_stops_at_wrong_title(self):
        with TemporaryDirectory() as folder, patch('workbench_app_jobs.Adb') as factory:
            device = factory.return_value.__enter__.return_value
            device.snapshot.side_effect = [
                scene('2025-06-08', '第一条测试餐厅点评。'),
                scene('2025-10-15', '第二条测试餐厅点评。', scrolled=True),
                scene('2025-10-16', '其他餐厅点评。', title='另一家餐厅', scrolled=True),
            ]
            stop = MagicMock()
            stop.is_set.return_value = stop.wait.return_value = False
            job = prepare_app_job({'mode': 'app_reviews', 'kind': 'restaurant', 'name': NAME,
                                   'resource_id': '2501', 'limit': 20, 'max_swipes': 2}, folder, 'adb')
            report = job(stop, lambda _: None)
            self.assertEqual(report['reason'], 'identity_unverified')
            self.assertEqual(report['identity_checked_screens'], 2)
            self.assertEqual(report['kind'], 'restaurant')
            self.assertEqual(report['count'], 2)
            self.assertEqual(report['object_id'], '2501')
            self.assertFalse(report['identity_id_verified'])
            self.assertFalse(report['complete'])
            self.assertEqual(device.run.call_count, 2)
            self.assertEqual(len(list((Path(folder) / 'raw' / report['run_id']).glob('*.xml'))), 2)
            controls = report['sampling_context']['observations'][0]
            self.assertIn({'label': '全部', 'display_count': '470'}, controls['visible_filters'])
            self.assertTrue(controls['latest_control_visible'])
            for sample in (False, True):
                with open_task_csv(folder, {'status': 'error', 'result': report}, sample=sample) as stream:
                    rows = list(csv.DictReader(io.StringIO(stream.read().decode('utf-8-sig'))))
                self.assertEqual([row['date_kind'] for row in rows], ['review_date', 'review_date'])
                self.assertEqual([row['date'] for row in rows], ['2025-06-08', '2025-10-15'])
            self.assertEqual([row['kind'] for row in read_scoped_mobile_preview(folder)['items']],
                             ['restaurant', 'restaurant'])

    def test_matching_title_and_review_like_body_without_filters_does_not_save(self):
        with TemporaryDirectory() as folder, patch('workbench_app_jobs.Adb') as factory:
            device = factory.return_value.__enter__.return_value
            device.snapshot.return_value = scene('2025-06-08', '正文也出现全部、好评、差评。', review_filters=False)
            stop = MagicMock()
            stop.is_set.return_value = stop.wait.return_value = False
            job = prepare_app_job({'mode': 'app_reviews', 'kind': 'restaurant', 'name': NAME,
                                   'resource_id': '2501', 'limit': 20, 'max_swipes': 2}, folder, 'adb')
            report = job(stop, lambda _: None)
            self.assertEqual(report['reason'], 'identity_unverified')
            self.assertEqual(report['count'], 0)
            self.assertEqual(report['identity_checked_screens'], 0)
            device.run.assert_not_called()
