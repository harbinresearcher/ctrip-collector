import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from core import parse_reviews


FRAMES = {frame.get('case'): ET.tostring(frame, encoding='unicode') for frame in
          ET.parse(Path(__file__).parent / 'fixtures' / 'restaurant-review-image-rows.xml').getroot()}


class RestaurantReviewTilesTests(unittest.TestCase):
    def test_full_tiles_stay_attached_to_their_visible_review(self):
        rows = parse_reviews(FRAMES['confirmed'], 'restaurant', locate_media=True)
        with_media = [row for row in rows if row.get('media_tile_bounds')]
        self.assertEqual(len(with_media), 1)
        self.assertEqual(with_media[0]['media_tile_bounds'], [(34, 1436, 535, 1937), (546, 1436, 1046, 1937)])
        self.assertEqual(with_media[0]['card_fields']['media_tiles_visible'], 2)

    def test_count_is_visible_without_collecting_crop_coordinates(self):
        rows = parse_reviews(FRAMES['confirmed'], 'restaurant')
        self.assertTrue(any(row['card_fields'].get('media_tiles_visible') == 2 for row in rows))
        self.assertTrue(all('media_tile_bounds' not in row for row in rows))

    def test_clipped_or_unassociated_images_are_not_cropped(self):
        for name in ('clipped', 'unassociated', 'badge_clipped'):
            with self.subTest(name=name):
                rows = parse_reviews(FRAMES[name], 'restaurant', locate_media=True)
                self.assertTrue(rows)
                self.assertTrue(all('media_tile_bounds' not in row for row in rows))


    def test_badged_deck_keeps_the_display_count_and_unobstructed_crops(self):
        for name, count, top in (('badge', 15, 1235), ('badge_adjacent', 11, 1416)):
            with self.subTest(name=name):
                rows = parse_reviews(FRAMES[name], 'restaurant', locate_media=True)
                crops = [row for row in rows if row.get('media_tile_bounds')]
                self.assertEqual(len(crops), 1)
                self.assertEqual(crops[0]['media_tile_bounds'], [
                    (34, top, 364, top + 331), (375, top, 706, top + 331),
                ])
                self.assertEqual(crops[0]['card_fields']['photo_count_display'], count)
                self.assertEqual(crops[0]['card_fields']['media_tiles_visible'], 2)

    def test_badge_with_an_unverified_overlay_does_not_identify_photos(self):
        frame = ET.fromstring(FRAMES['badge'])
        parents = {child: parent for parent in frame.iter() for child in parent}
        badge = frame.find(".//node[@text='15图']")
        parents[badge][3].set('content-desc', '视频')
        rows = parse_reviews(ET.tostring(frame, 'unicode'), 'restaurant', locate_media=True)
        self.assertTrue(rows)
        self.assertTrue(all('media_tile_bounds' not in row for row in rows))
        self.assertTrue(all('photo_count_display' not in row['card_fields'] for row in rows))
