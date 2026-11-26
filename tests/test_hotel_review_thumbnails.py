import csv
import io
import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from zipfile import ZipFile

from PIL import Image, ImageDraw

from core import parse_reviews
from runner import collect
from test_restaurant_app_reviews import NAME, scene


SOURCE = Path(__file__).parent / 'fixtures' / 'hotel-review-media-footer.xml'


class HotelReviewThumbnailTests(unittest.TestCase):
    def frame(self):
        image = Image.new('RGB', (1440, 3200), 'red')
        draw = ImageDraw.Draw(image)
        for column in range(0, image.width, 40):
            draw.rectangle((column, 350, column + 19, 2000), fill='navy')
        draw.rectangle((0, 1300, image.width, 1340), fill='green')
        stream = io.BytesIO()
        image.save(stream, format='PNG')
        return stream.getvalue()

    def test_capture_checks_same_review_and_exports_only_crops(self):
        xml = SOURCE.read_text('utf-8')
        fake = Mock()
        fake.snapshot.side_effect = [xml, xml]
        with TemporaryDirectory() as folder, patch('hotel_review_thumbnails.read_gallery_screenshot', return_value=self.frame()):
            result = collect(fake, 'hotel', '样例', folder, limit=2, max_swipes=0,
                             object_id='123', save_review_thumbnails=True)
            self.assertEqual(result['sample_csv_count'], 2)
            self.assertEqual(result['review_thumbnails']['saved_count'], 5)
            self.assertEqual(fake.snapshot.call_count, 2)
            fake.run.assert_not_called()
            with ZipFile(Path(folder) / result['review_thumbnails']['archive']) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual([row['sample_order'] for row in manifest['thumbnails']], [1] * 5)
                self.assertFalse(manifest['original_files'])
                for image in manifest['thumbnails']:
                    with Image.open(io.BytesIO(archive.read(image['file']))) as crop:
                        self.assertEqual(crop.size, (420, 420))
                        self.assertGreater(len(crop.getcolors()), 1)
                self.assertEqual({item['sha256'] for item in manifest['thumbnails']},
                                 {sha256(archive.read(item['file'])).hexdigest() for item in manifest['thumbnails']})

    def test_changed_page_drops_images_without_dropping_verified_reviews(self):
        xml = SOURCE.read_text('utf-8')
        fake = Mock()
        fake.snapshot.side_effect = [xml, xml.replace('[75,716][495,1136]', '[76,716][496,1136]')]
        with TemporaryDirectory() as folder, patch('hotel_review_thumbnails.read_gallery_screenshot', return_value=self.frame()):
            result = collect(fake, 'hotel', '样例', folder, limit=2, max_swipes=0,
                             object_id='123', save_review_thumbnails=True)
            self.assertEqual(result['sample_csv_count'], 2)
            self.assertEqual(result['reason'], 'review_screen_changed')
            self.assertEqual(result['review_thumbnails']['saved_count'], 0)
            self.assertEqual(result['review_thumbnails']['skipped_screens'], 1)
            self.assertIsNone(result['review_thumbnails']['archive'])

    def test_screenshot_with_different_dimensions_is_not_used(self):
        xml = SOURCE.read_text('utf-8')
        fake = Mock()
        fake.snapshot.return_value = xml
        with Image.open(io.BytesIO(self.frame())) as full:
            frame = io.BytesIO()
            full.crop((0, 0, 1440, 3199)).save(frame, format='PNG')
        with TemporaryDirectory() as folder, patch('hotel_review_thumbnails.read_gallery_screenshot',
                                                   return_value=frame.getvalue()):
            result = collect(fake, 'hotel', '样例', folder, limit=2, max_swipes=0,
                             object_id='123', save_review_thumbnails=True)
            self.assertEqual(result['sample_csv_count'], 2)
            self.assertEqual(result['review_thumbnails']['skipped_screens'], 1)
            self.assertIsNone(result['review_thumbnails']['archive'])


class RestaurantReviewThumbnailTests(unittest.TestCase):
    def test_scoped_review_crops_match_the_run_csv_and_deduplicate_overlapping_screens(self):
        xml = scene('2025-06-08', '测试餐厅点评。')
        wrong_restaurant = scene('2025-06-09', '另一条点评。', title='另一家餐厅')
        device = Mock()
        device.snapshot.side_effect = [xml, xml, xml, xml, wrong_restaurant]
        stop = Mock()
        stop.is_set.return_value = stop.wait.return_value = False
        boxes = [(34, 1396, 534, 1896), (546, 1396, 1046, 1896)]

        def parse_with_crops(review_xml, kind, *, locate_media=False):
            self.assertTrue(locate_media)
            self.assertEqual(kind, 'restaurant')
            rows = parse_reviews(review_xml, kind)
            self.assertEqual(len(rows), 1)
            rows[0]['media_tile_bounds'] = boxes
            return rows

        frame = Image.new('RGB', (1080, 2400), 'red')
        draw = ImageDraw.Draw(frame)
        draw.rectangle((34, 1400, 534, 1546), fill='navy')
        draw.rectangle((546, 1400, 1046, 1546), fill='green')
        stream = io.BytesIO()
        frame.save(stream, format='PNG')
        with TemporaryDirectory() as folder, patch('runner.parse_reviews', side_effect=parse_with_crops), \
                patch('hotel_review_thumbnails.read_gallery_screenshot', return_value=stream.getvalue()) as screenshot:
            result = collect(device, 'restaurant', NAME, folder, limit=20, max_swipes=2, stop=stop,
                             object_id='2501', refresh=True, save_review_thumbnails=True)
            self.assertEqual(result['reason'], 'identity_unverified')
            self.assertEqual(result['identity_checked_screens'], 2)
            self.assertEqual(screenshot.call_count, 2)
            self.assertEqual(result['review_thumbnails']['saved_count'], 2)
            self.assertEqual(result['review_thumbnails']['archive'],
                             f"restaurant-review-thumbnails-{result['run_id']}.zip")
            with Path(result['sample_csv']).open(encoding='utf-8-sig', newline='') as sample:
                rows = list(csv.DictReader(sample))
            self.assertEqual(len(rows), 1)
            with ZipFile(Path(folder) / result['review_thumbnails']['archive']) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(manifest['source'], 'restaurant_review_display_crops')
                self.assertEqual(manifest['identity_verification'], 'screen_title_name_only')
                self.assertEqual(manifest['sample_csv'], Path(result['sample_csv']).name)
                self.assertFalse(manifest['original_files'])
                self.assertEqual([item['crop_bounds'] for item in manifest['thumbnails']], list(map(list, boxes)))
                for item in manifest['thumbnails']:
                    self.assertEqual(item['sample_order'], int(rows[0]['run_order']))
                    self.assertEqual(item['review_key'], rows[0]['key'])
                    self.assertEqual(item['screen'], 1)
                    self.assertEqual(item['sha256'], sha256(archive.read(item['file'])).hexdigest())
