import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET

from core import parse_reviews
from mobile_scoped_store import MobileScopedStore
from workbench_preview import read_scoped_mobile_preview


FIXTURES = Path(__file__).parent / 'fixtures'
NAME = '长白山西景区'


class SightReviewFooterTests(unittest.TestCase):
    def test_match_accessibility_footer_to_its_own_clipped_card(self):
        first = parse_reviews((FIXTURES / 'ctrip-comments.xml').read_text('utf-8'), 'sight')[0]
        self.assertNotIn('4图', first['raw_texts'])
        self.assertEqual(first['card_fields']['photo_count_display'], 4)
        self.assertEqual(first['card_fields']['helpful_count_display'], 1)
        self.assertEqual(first['card_fields']['thread_comment_count_display'], 1)
        reviews = parse_reviews((FIXTURES / 'ctrip-comments3.xml').read_text('utf-8'), 'sight')
        self.assertNotIn('helpful_count_display', reviews[0]['card_fields'])
        self.assertEqual(reviews[1]['card_fields']['helpful_count_display'], 2)
        self.assertNotIn('thread_comment_count_display', reviews[1]['card_fields'])

    def test_reject_footer_with_different_body_or_appending_unanchored_numbers(self):
        xml = (FIXTURES / 'ctrip-comments.xml').read_text('utf-8')
        review = parse_reviews(xml, 'sight')[0]
        root = ET.fromstring(xml)
        card = next(node for node in root.iter('node') if '有用1' in node.get('content-desc', ''))
        for text in (card.get('content-desc').replace(review['content'], '另一条正文', 1),
                     card.get('content-desc').replace('4图, ', '附加文本, 4图, ', 1)):
            with self.subTest():
                card.set('content-desc', text)
                rows = parse_reviews(ET.tostring(root, encoding='unicode'), 'sight')
                self.assertNotIn('helpful_count_display', rows[0]['card_fields'])
                self.assertNotIn('photo_count_display', rows[0]['card_fields'])

    def test_saved_fields_are_specific_to_each_review_and_surface_in_both_csv_formats(self):
        xml = (FIXTURES / 'ctrip-comments.xml').read_text('utf-8')
        original = parse_reviews(xml, 'sight')[0]
        later = parse_reviews(xml.replace('有用1', '有用7'), 'sight')[0]
        clipped = {**original, 'card_fields': {'travel_type': '家庭亲子'}}
        other = parse_reviews((FIXTURES / 'ctrip-comments3.xml').read_text('utf-8'), 'sight')[0]
        with TemporaryDirectory() as folder:
            store = MobileScopedStore(folder, 'sight', '42')
            try:
                run_id = '20261122T210101-012abc'
                observed_keys = {}
                store.start_run(run_id, NAME)
                store.save(NAME, [original, other], 'first.xml', run_id=run_id, observed_keys=observed_keys)
                store.save(NAME, [later, clipped], 'later.xml', run_id=run_id, observed_keys=observed_keys)
                self.assertEqual(store.count(), 2)
                store.finish_run(run_id, 'page_limit')
                for export in (store.export(), store.export(run_id=run_id, observed_keys=observed_keys)):
                    with export.open(encoding='utf-8-sig', newline='') as stream:
                        rows = list(csv.DictReader(stream))
                    self.assertEqual(rows[0]['photo_count_display'], '4')
                    self.assertEqual(rows[0]['helpful_count_display'], '7')
                    self.assertEqual(rows[0]['thread_comment_count_display'], '1')
                    self.assertEqual(rows[1]['helpful_count_display'], '')
                    self.assertEqual(json.loads(rows[0]['raw_texts']), original['raw_texts'])
            finally:
                store.close()
            items = read_scoped_mobile_preview(folder)['items']
            fields = next(item['card_fields'] for item in items if item['content'] == original['content'])
            self.assertEqual((fields['photo_count_display'], fields['helpful_count_display']), (4, 7))


    def test_older_database_can_read_a_numbered_visible_footer_from_an_earlier_observation(self):
        original = parse_reviews((FIXTURES / 'ctrip-comments3.xml').read_text('utf-8'), 'sight')[0]
        footer = ['\U000f12c6', '有用3', '\ue951', '评论1']
        body_index = original['raw_texts'].index(original['content'])
        first = {**original, 'raw_texts': [*original['raw_texts'][:body_index + 1], *footer]}
        with TemporaryDirectory() as folder:
            store = MobileScopedStore(folder, 'sight', '42')
            try:
                store.save(NAME, [original], 'initial.xml')
                store.save(NAME, [first], 'visible-footer.xml')
                store.save(NAME, [original], 'clipped.xml')
                # Model a database from before numeric footer fields had their own table.
                with store.db:
                    store.db.execute('DROP TABLE reviews_scoped_sight_counts')
                fields = read_scoped_mobile_preview(folder)['items'][0]['card_fields']
                self.assertEqual((fields['helpful_count_display'], fields['thread_comment_count_display']), (3, 1))
            finally:
                store.close()
