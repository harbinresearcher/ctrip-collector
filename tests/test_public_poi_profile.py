import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from workbench_jobs import make_job
from test_shopping_snapshot import URL, responses


FIXTURES = Path(__file__).parent / 'fixtures'
RESTAURANT_FILE = FIXTURES / 'public-restaurant-profile.json'
SIGHT_FILE = FIXTURES / 'public-sight-profile.json'


class PublicPoiProfileTests(unittest.TestCase):
    def test_sight_guidance_and_review_topics_remain_separate_from_full_ai_summary(self):
        sight = json.loads(SIGHT_FILE.read_text('utf-8'))
        with TemporaryDirectory() as folder, patch('workbench_jobs.load_target', return_value=sight):
            job = make_job({'mode': 'public_poi_profile', 'urls': [sight['url']]}, folder)
            report = job(Event(), lambda _: None)
        item = report['profiles'][0]
        self.assertEqual((item['kind'], item['poi_id'], item['resource_id']), ('sight', 10559411, 112796))
        self.assertEqual(item['opening_time_rules'], [{'period_text': '全年', 'hours_texts': ['10:00-21:00开放']}])
        self.assertEqual(item['visit_duration_text'], '1-2小时')
        self.assertEqual(len(item['admission_policies']), 8)
        self.assertEqual(item['score_dimensions'][1], {'label': '趣味', 'score': 4.7})
        self.assertEqual(item['review_topic_tags'][0], {'label': '领略海河两岸的美景', 'display_count': 23})
        self.assertEqual(item['review_topic_scope'], 'public_page_tag_labels_only')
        self.assertNotIn('ai_summary', item)
        self.assertNotIn('aiTagId', json.dumps(item, ensure_ascii=False))
        self.assertFalse(report['complete'])

    def test_review_batch_keeps_projected_profiles_on_first_run_and_verified_reuse(self):
        restaurant = json.loads(RESTAURANT_FILE.read_text('utf-8'))
        from network import load_target
        overview, comments = responses()
        overview['PoiOverviewInfo'].update(PoiName='样例商场', CommentCount=38)
        with patch('network.post_json', side_effect=[overview, comments]):
            shop = load_target(URL)
        with TemporaryDirectory() as folder, patch('workbench_jobs.load_target', side_effect=[restaurant, shop]) as load, \
             patch('network.fetch_page') as fetch:
            fetch.side_effect = lambda target, page: {'code': 200, 'result': {
                'items': [{'commentId': '1', 'resourceId': target['resource_id'], 'content': '点评正文'}],
                'totalCount': target['display_total']}}
            job = make_job({'mode': 'reviews', 'urls': [restaurant['url'], URL], 'limit': 1}, folder)
            first = job(Event(), lambda _: None)
            self.assertEqual((first['processed'], first['skipped']), (2, 0))
            self.assertEqual([item['public_profile']['kind'] for item in first['results']], ['restaurant', 'shopping'])
            self.assertEqual(first['results'][1]['public_profile']['comment_samples'][0]['commentId'], '123')
            self.assertEqual([item['read_unique'] for item in first['results']], [1, 1])
            self.assertNotIn('private-name', str(first['results'][1]['public_profile']))
            self.assertFalse(first['complete'])
            second = job(Event(), lambda _: None)
            self.assertEqual((second['processed'], second['skipped']), (0, 2))
            self.assertEqual([item['public_profile'] for item in second['reused_results']],
                             [item['public_profile'] for item in first['results']])
            self.assertEqual((load.call_count, fetch.call_count), (2, 2))
            self.assertEqual(json.loads((Path(folder) / 'batch-report.json').read_text('utf-8')), second)

    def test_restaurant_profile_preserves_distinct_ids_and_known_public_details(self):
        restaurant = json.loads(RESTAURANT_FILE.read_text('utf-8'))
        with TemporaryDirectory() as folder, patch('workbench_jobs.load_target', return_value=restaurant) as load:
            job = make_job({'mode': 'public_poi_profile', 'urls': [restaurant['url']]}, folder)
            result = job(Event(), lambda _: None)
            load.assert_called_once_with(restaurant['url'])
            item = result['profiles'][0]
            self.assertEqual((item['poi_id'], item['resource_id']), (108786, 8639))
            self.assertEqual(item['cuisines'], ['山西菜'])
            self.assertIn('浑源凉粉', item['featured_dishes'])
            self.assertEqual(item['score_dimensions'][0], {'label': '口味', 'score': 4.4})
            self.assertTrue(item['opening_text'])
            self.assertNotIn('phones', item)
            self.assertNotIn('comment_info', item)
            self.assertFalse(item['complete'])
            self.assertEqual(json.loads(Path(result['report_file']).read_text('utf-8')), result)

    def test_shopping_preview_is_saved_without_paginating_or_retaining_sample_author(self):
        overview, comments = responses()
        overview['PoiOverviewInfo'].update(PoiName='样例商场', CommentCount=38)
        with TemporaryDirectory() as folder, patch('network.post_json', side_effect=[overview, comments]) as post:
            job = make_job({'mode': 'public_poi_profile', 'urls': [URL, URL]}, folder)
            result = job(Event(), lambda _: None)
            self.assertEqual(post.call_count, 2)
            profile = result['profiles'][0]
            self.assertEqual(profile['display_total'], 38)
            self.assertEqual(profile['comment_samples'][0]['commentId'], '123')
            self.assertNotIn('private-name', json.dumps(profile, ensure_ascii=False))
            self.assertFalse(result['complete'])

    def test_invalid_link_and_failed_fetch_do_not_hide_other_results(self):
        restaurant = json.loads(RESTAURANT_FILE.read_text('utf-8'))
        with TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                make_job({'mode': 'public_poi_profile', 'urls': ['https://hotels.ctrip.com/hotels/1.html']}, folder)
            with patch('workbench_jobs.load_target', side_effect=[ValueError('页面解析失败'), restaurant]):
                job = make_job({'mode': 'public_poi_profile', 'urls': [URL, restaurant['url']]}, folder)
                result = job(Event(), lambda _: None)
            self.assertEqual(len(result['profiles']), 1)
            self.assertEqual(result['failed'][0]['url'], URL)
            self.assertEqual(result['reason'], 'partial')
