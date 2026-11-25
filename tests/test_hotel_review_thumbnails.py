import io
import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from zipfile import ZipFile

from PIL import Image, ImageDraw

from runner import collect


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
