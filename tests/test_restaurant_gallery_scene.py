import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from restaurant_gallery_scene import read_photo_viewer, read_review_note


FIXTURE = Path(__file__).parent / 'fixtures' / 'restaurant-review-gallery-scenes.xml'
FRAMES = {frame.get('frame'): ET.tostring(frame, 'unicode') for frame in ET.parse(FIXTURE).getroot()}
NAME = '示例餐厅'
AUTHOR = '示例点评者'
BODY = '示例点评正文。 包含换行。'


class ReviewGallerySceneTests(unittest.TestCase):
    def test_note_confirms_the_review_and_avoids_the_adjacent_preview(self):
        self.assertEqual(read_review_note(FRAMES['viewer-page1'], NAME, AUTHOR, BODY),
                         (1, 2, (60, 274, 542, 1715)))
        self.assertEqual(read_review_note(FRAMES['viewer-page2'], NAME, AUTHOR, BODY, 2),
                         (2, 2, (0, 274, 1080, 1715)))

    def test_note_rejects_different_identity_partial_body_and_total(self):
        frame = FRAMES['viewer-page1']
        for identity in (
            ('另一家餐厅', AUTHOR, BODY, None),
            (NAME, '另一位点评者', BODY, None),
            (NAME, AUTHOR, '示例点评正文。', None),
            (NAME, AUTHOR, BODY, 3),
        ):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                read_review_note(frame, *identity)

    def test_note_needs_the_bound_title_and_safe_photo(self):
        for old, new in (
            ('text="1/2"', 'text="3/2"'),
            ('[242,1749][923,1805]', '[242,600][923,999]'),
            ('[542,274][1080,1715]', '[100,274][980,1715]'),
        ):
            with self.subTest(change=new), self.assertRaises(ValueError):
                read_review_note(FRAMES['viewer-page1'].replace(old, new), NAME, AUTHOR, BODY)

    def test_native_photo_view_has_the_verified_save_button_on_both_pages(self):
        for page in (1, 2):
            with self.subTest(page=page):
                self.assertEqual(read_photo_viewer(FRAMES[f'immersive-page{page}'], 2),
                                 (page, 2, (0, 0, 1080, 2337), (970, 154, 1054, 238)))

    def test_native_photo_view_opens_with_controls_before_the_pager(self):
        self.assertEqual(read_photo_viewer(FRAMES['immersive-opened-page1'], 2),
                         (1, 2, (0, 0, 1080, 2337), (970, 154, 1054, 238)))

    def test_native_view_rejects_wrong_counter_wrong_control_or_non_app_window(self):
        frame = FRAMES['immersive-page1']
        for changed in (
            FRAMES['viewer-page1'],
            frame.replace('text="1/2"', 'text="2/2"'),
            frame.replace('content-desc="关闭"', 'content-desc="返回"'),
            frame.replace('content-desc="下载"', 'content-desc="保存到相册"'),
            frame.replace('package="ctrip.android.view"', 'package="other.app"'),
        ):
            with self.subTest(change=changed[:120]), self.assertRaises(ValueError):
                read_photo_viewer(changed, 2)
        with self.assertRaises(ValueError):
            read_photo_viewer(frame, 3)
