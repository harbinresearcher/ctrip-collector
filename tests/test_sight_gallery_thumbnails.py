import hashlib
import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
from zipfile import ZipFile

from PIL import Image, ImageDraw

from workbench_jobs import make_job


XML = (Path(__file__).parent / 'fixtures' / 'ctrip-comments.xml').read_text('utf-8')
NAME = '长白山西景区'


def sample_screen(size=(1440, 3200)):
    frame = Image.new('RGB', size, 'white')
    draw = ImageDraw.Draw(frame)
    for left, color in zip((95, 455, 815, 1175), ('blue', 'red', 'purple', 'yellow')):
        draw.rectangle((left, 1759, left + 80, 1840), fill=color)
        draw.rectangle((left + 80, 1890, left + 150, 2000), fill='green')
    target = io.BytesIO()
    frame.save(target, format='PNG')
    return target.getvalue()


class SightGalleryThumbnailsTests(unittest.TestCase):
    def run_capture(self, folder, *, next_screen=XML, image=None):
        with patch('workbench_jobs.Adb') as factory, \
             patch('gallery_capture.subprocess.run') as screenshot:
            screenshot.return_value.stdout = sample_screen() if image is None else image
            screenshot.return_value.returncode = 0
            device = factory.return_value.__enter__.return_value
            device.executable = 'test-adb'
            device.serial = 'test-device'
            device.snapshot.side_effect = [XML, next_screen]
            request = {'mode': 'sight_ai', 'name': NAME, 'save_sight_gallery_thumbnails': True}
            report = make_job(request, folder, 'test-adb')(Event(), lambda _: None)
            screenshot.assert_called_once()
            self.assertEqual(device.snapshot.call_count, 2)
            device.run.assert_not_called()
            return report

    def test_sight_gallery_saves_only_three_complete_tiles_with_report_link(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            report = self.run_capture(folder)
            thumbs = report['gallery_thumbnails']
            self.assertEqual(thumbs['saved_count'], 3)
            self.assertEqual(thumbs['visible_tile_candidates'], 3)
            self.assertFalse(thumbs['original_files'])
            self.assertEqual(json.loads(Path(report['file']).read_text('utf-8')), report)
            files = list(folder.iterdir())
            self.assertEqual({path.suffix for path in files}, {'.json', '.zip'})
            with ZipFile(folder / thumbs['archive']) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(manifest['report_file'], Path(report['file']).name)
                self.assertEqual([item['label'] for item in manifest['thumbnails']],
                                 ['天气晴朗', '自然景观远景', '山川'])
                self.assertEqual(len(archive.namelist()), 4)
                for item, color in zip(manifest['thumbnails'], ((0, 0, 255), (255, 0, 0), (128, 0, 128))):
                    data = archive.read(item['file'])
                    self.assertEqual(hashlib.sha256(data).hexdigest(), item['sha256'])
                    with Image.open(io.BytesIO(data)) as image:
                        self.assertEqual(image.size, (330, 329))
                        self.assertEqual(image.getpixel((0, 0)), color)
            def covered_thumbnails():
                report = make_job({'mode': 'coverage'}, folder)(Event(), lambda _: None)
                return report['android']['sight_ai_summaries'][0]['display_thumbnails_with_archive']
            self.assertEqual(covered_thumbnails(), 3)
            (folder / thumbs['archive']).unlink()
            self.assertEqual(covered_thumbnails(), 0)

    def test_unmatched_layout_or_screen_dimensions_keep_only_summary(self):
        for change, image, expected in (
            (XML.replace('长白山西景区', '另一处景区', 1), None, 'screen_changed'),
            (XML, sample_screen((1440, 3140)), 'dimension_mismatch'),
            (XML, b'not a PNG', 'capture_unavailable'),
        ):
            with self.subTest(expected=expected), TemporaryDirectory() as tmp:
                folder = Path(tmp)
                report = self.run_capture(folder, next_screen=change, image=image)
                self.assertEqual(report['gallery_thumbnails']['status'], expected)
                self.assertEqual(report['gallery_thumbnails']['saved_count'], 0)
                self.assertEqual(len(report['sections']), 2)
                self.assertEqual([path.suffix for path in folder.iterdir()], ['.json'])

    def test_option_requires_boolean(self):
        with TemporaryDirectory() as tmp, self.assertRaisesRegex(ValueError, '缩略图'):
            make_job({'mode': 'sight_ai', 'name': NAME, 'save_sight_gallery_thumbnails': 'no'}, tmp, 'adb')
