import csv
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core import parse_reviews
from mobile_scoped_store import MobileScopedStore
from workbench_preview import read_scoped_mobile_preview

logger = logging.getLogger(__name__)
FIXTURES = Path(__file__).parent / 'fixtures'
HOTEL_MEDIA_FOOTER = FIXTURES / 'hotel-review-media-footer.xml'


class MobileReviewFieldsTests(unittest.TestCase):
    def test_review_media_grid_counts_full_tiles_without_borrowing_page_gallery(self):
        no_tiles = parse_reviews((FIXTURES / 'hotel-comments.xml').read_text('utf-8'), 'hotel')[0]
        clipped = parse_reviews((FIXTURES / 'hotel-comments2.xml').read_text('utf-8'), 'hotel')[0]
        complete, next_review = parse_reviews(HOTEL_MEDIA_FOOTER.read_text('utf-8'), 'hotel')
        self.assertNotIn('media_tiles_visible', no_tiles['card_fields'])
        self.assertNotIn('media_tiles_visible', next_review['card_fields'])
        self.assertEqual(clipped['card_fields']['media_tiles_visible'], 3)
        self.assertEqual(complete['card_fields']['media_tiles_visible'], 5)
        two_columns = FIXTURES / 'hotel-review-media-two-columns.xml'
        cards = parse_reviews(two_columns.read_text('utf-8'), 'hotel')
        self.assertEqual(cards[0]['card_fields']['media_tiles_visible'], 4)
        # Later crops with fewer tiles must not be summed or erase the best one-screen evidence.
        less = {**complete, 'card_fields': {'media_tiles_visible': 2}}
        with TemporaryDirectory() as folder:
            store = MobileScopedStore(folder, 'hotel', '42')
            try:
                store.save('样例', [complete, next_review], 'first.xml')
                store.save('样例', [less], 'second.xml')
                with store.export().open(encoding='utf-8-sig', newline='') as stream:
                    self.assertEqual([row['media_tiles_visible'] for row in csv.DictReader(stream)], ['5', ''])
                preview = read_scoped_mobile_preview(folder)['items']
                self.assertEqual(next(item for item in preview if item['content'] == complete['content'])
                                 ['card_fields']['media_tiles_visible'], 5)
            finally:
                store.close()

    def test_historical_helpful_count_is_attached_to_the_correct_review_and_survives_clipping(self):
        matches = parse_reviews(HOTEL_MEDIA_FOOTER.read_text('utf-8'), 'hotel')
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0]['card_fields']['helpful_count_display'], 17)
        self.assertNotIn('helpful_count_display', matches[1]['card_fields'])
        first = matches[0]
        hidden = {**first, 'raw_texts': [text for text in first['raw_texts'] if text != '有用17']}
        changed = {**first, 'raw_texts': ['有用18' if text == '有用17' else text for text in first['raw_texts']]}
        with TemporaryDirectory() as folder:
            store = MobileScopedStore(folder, 'hotel', '42')
            try:
                store.save('酒店样例', [first, matches[1]], 'first.xml')
                store.save('酒店样例', [changed], 'later.xml')
                store.save('酒店样例', [hidden], 'clipped.xml')
                with store.export().open(encoding='utf-8-sig', newline='') as stream:
                    exported = list(csv.DictReader(stream))
                self.assertEqual([row['helpful_count_display'] for row in exported], ['18', ''])
                preview = read_scoped_mobile_preview(folder)['items']
                self.assertEqual(next(item for item in preview if item['content'] == first['content'])
                                 ['card_fields']['helpful_count_display'], 18)
            finally:
                store.close()

    def test_unlabeled_body_text_and_conflicting_controls_are_not_helpful_counts(self):
        from mobile_review_fields import extract_card_fields
        icon = chr(0xF0C85)
        for texts, body in ((['4.5', '正常正文', '有用99'], '正常正文'),
                            (['4.5', '有用99'], '有用99'),
                            (['4.5', '正常正文', icon, '有用'], '正常正文'),
                            (['4.5', '正常正文', icon, '有用8', icon, '有用9'], '正常正文')):
            with self.subTest(texts=texts):
                self.assertNotIn('helpful_count_display', extract_card_fields('hotel', texts, body, '4.5'))
        self.assertEqual(extract_card_fields('hotel', ['4.5', '正文', icon, '有用0'],
                                             '正文', '4.5')['helpful_count_display'], 0)

    def test_real_card_fields_and_reply_from_later_observation(self):
        logger.info('开始验证真实卡片的结构化字段与后续回复')
        hotel = parse_reviews((FIXTURES / 'hotel-comments2.xml').read_text('utf-8'), 'hotel')[0]
        sight = parse_reviews((FIXTURES / 'ctrip-comments3.xml').read_text('utf-8'), 'sight')[0]
        self.assertEqual(hotel['card_fields'], {'room_type': '锦华高级房-双床', 'travel_type': '家庭亲子',
                                               'media_tiles_visible': 3})
        self.assertEqual(sight['card_fields'], {'travel_type': '朋友出游', 'visit_duration': '5小时', 'verified_purchase_visible': True})
        reply = '感谢入住，欢迎再来。'
        texts = hotel['raw_texts'].copy()
        texts.insert(texts.index(hotel['content']) + 1, '酒店回复：' + reply)
        later = {**hotel, 'raw_texts': texts}
        with TemporaryDirectory() as folder:
            store = MobileScopedStore(folder, 'hotel', '42')
            try:
                store.save('酒店样例', [hotel], 'first.xml')
                store.save('酒店样例', [later], 'later.xml')
                store.save('酒店样例', [hotel], 'third.xml')
                self.assertEqual(store.count(), 1)
                with store.export().open(encoding='utf-8-sig', newline='') as stream:
                    exported = next(csv.DictReader(stream))
                self.assertEqual(exported['hotel_reply'], reply)
                self.assertEqual(exported['room_type'], '锦华高级房-双床')
                self.assertEqual(exported['content'], hotel['content'])
            finally:
                store.close()
            preview = read_scoped_mobile_preview(folder)['items'][0]
            self.assertEqual(preview['card_fields']['hotel_reply'], reply)
            self.assertNotIn('author', preview)
        logger.info('结构化字段与后续回复验证完成')

    def test_body_prefix_and_post_body_trip_labels_cannot_become_metadata(self):
        logger.info('开始验证卡片字段边界')
        from mobile_review_fields import extract_card_fields
        body = '酒店回复：这是评论者自己写的正文'
        self.assertEqual(extract_card_fields('hotel', [body], body, '5.0'), {})
        self.assertEqual(extract_card_fields('sight', ['5.0', '正文', '家庭亲子', '消费后评价'], '正文', '5.0'), {})
        logger.info('卡片字段边界验证完成')
