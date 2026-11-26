import csv
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch
from zipfile import ZipFile

from restaurant_review_gallery import capture_restaurant_review_gallery, wait_scene


ROW = {
    'author': '样本作者', 'date': '2025-03-12', 'date_kind': 'review_date',
    'rating': '4.5', 'content': '样本正文', 'has_expand': False, 'raw_texts': [],
    'media_tile_bounds': [(0, 20, 200, 220)], 'card_fields': {'photo_count_display': 2},
}
FRAMES = ((1, 2, (0, 0, 1080, 2337), (970, 154, 1054, 238)),
          (2, 2, (0, 0, 1080, 2337), (970, 154, 1054, 238)))


class RestaurantGalleryCaptureTests(unittest.TestCase):
    def run_gallery(self, output, downloads, *, rows=None):
        device = Mock()
        device.snapshot.return_value = '<hierarchy />'
        stop = Mock(spec=Event)
        stop.is_set.return_value = False
        stop.wait.return_value = False
        with patch('restaurant_review_gallery.scoped_rows', side_effect=rows or ([ROW], [ROW])), \
                patch('restaurant_review_gallery.read_review_note', return_value=(1, 2, (0, 0, 530, 900))), \
                patch('restaurant_review_gallery.read_photo_viewer', side_effect=(FRAMES[0], *FRAMES)), \
                patch('restaurant_review_gallery.download_visible_photo', side_effect=downloads) as download:
            report = capture_restaurant_review_gallery(device, output, '样本餐厅', '42',
                                                      stop, max_pages=2, emit=Mock())
        return report, device, download

    def test_complete_files_are_bound_to_the_validated_review_csv(self):
        with TemporaryDirectory() as folder:
            report, device, download = self.run_gallery(
                folder, [(b'first App download', 'webp', (640, 960)), (b'second App download', 'webp', (640, 426))],
            )
            self.assertEqual(report['reason'], 'gallery_end_reached')
            self.assertTrue(report['gallery_complete'])
            self.assertEqual(report['page_count'], 2)
            self.assertEqual(download.call_count, 2)
            steps = device.run.call_args_list
            self.assertEqual([step.args[2] for step in steps], ['tap', 'tap', 'swipe'])
            self.assertEqual(steps[-1].args, ('shell', 'input', 'swipe', '886', '1145', '194', '1145', '150'))
            with Path(report['sample_csv']).open(encoding='utf-8-sig', newline='') as stream:
                sample, = list(csv.DictReader(stream))
            with ZipFile(Path(folder) / report['archive']) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(manifest['review_key'], sample['key'])
                self.assertEqual(manifest['sample_order'], 1)
                self.assertEqual([p['page'] for p in manifest['pages']], [1, 2])
                self.assertEqual(archive.read(manifest['pages'][1]['file']), b'second App download')
                self.assertFalse(manifest['original_files_verified'])

    def test_partial_download_keeps_pages_already_saved(self):
        with TemporaryDirectory() as folder:
            report, _, _ = self.run_gallery(
                folder, [(b'first App download', 'webp', (640, 960)), ValueError('未核验下载文件')],
            )
            self.assertEqual(report['reason'], 'download_unverified')
            self.assertFalse(report['gallery_complete'])
            self.assertEqual(report['page_count'], 1)
            with ZipFile(Path(folder) / report['archive']) as archive:
                self.assertEqual(len(json.loads(archive.read('manifest.json'))['pages']), 1)

    def test_entry_layout_change_prevents_tapping(self):
        moved = {**ROW, 'media_tile_bounds': [(0, 30, 200, 230)]}
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, '位置'):
                self.run_gallery(folder, [], rows=([ROW], [moved]))

    def test_waits_out_transient_window_focus_after_switching_to_the_viewer(self):
        device, reader, stop = Mock(), Mock(return_value=FRAMES[0]), Mock()
        device.snapshot.side_effect = (RuntimeError('window changing'), '<hierarchy/>')
        stop.wait.return_value = False
        self.assertEqual(wait_scene(device, stop, reader, 1), FRAMES[0])
        self.assertEqual(device.snapshot.call_count, 2)
