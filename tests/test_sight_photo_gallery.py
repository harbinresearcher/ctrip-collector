import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch
from zipfile import ZipFile

from PIL import Image, ImageDraw

from sight_photo_gallery import capture_sight_photo_gallery, read_checkpoint, read_viewer


APP = 'ctrip.android.view'
NAME = '长白山西景区'
PREVIEW = (Path(__file__).parent / 'fixtures' / 'ctrip-comments.xml').read_text('utf-8')


def node(box, children='', *, class_name='ViewGroup', label='', resource_id=''):
    return (f'<node package="{APP}" class="android.widget.{class_name}" bounds="{box}"'
            f' text="{label}" resource-id="{resource_id}">{children}</node>')


def screen(children):
    return '<hierarchy>' + node('[0,0][400,900]', children, class_name='FrameLayout') + '</hierarchy>'


GRID = screen(
    node('[0,35][400,90]', node('[50,50][350,85]', label='用户实拍照片', class_name='TextView'))
    + node('[0,90][400,870]', node('[0,90][400,870]',
           node('[10,120][55,140]', label='全部', class_name='TextView')
           + node('[10,190][190,370]', node('[10,190][190,370]', class_name='ImageView', resource_id='image'),
                  resource_id='o_cc_travelSleuthPicture_picture')), class_name='ScrollView')
)


def viewer(index, total=896):
    image_box = '[0,250][400,650]'
    return screen(node('[0,180][400,750]',
                       node('[0,180][400,750]', node(image_box, resource_id='o_cc_picture_show')
                            + node(image_box, class_name='ImageView', resource_id='image')),
                       class_name='HorizontalScrollView')
                  + node('[0,780][400,875]',
                         node('[349,799][360,824]', label=str(index), class_name='TextView')
                         + node('[360,800][389,823]', label=f'/{total}', class_name='TextView')))


def frame(color='red'):
    image = Image.new('RGB', (400, 900), 'black')
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 250, 199, 649), fill='skyblue')
    draw.rectangle((200, 250, 399, 649), fill=color)
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


class SightPhotoGalleryTests(unittest.TestCase):
    def checkpoint(self, tmp):
        device = Mock()
        device.snapshot.side_effect = [PREVIEW, GRID, viewer(1), viewer(1)]
        stop = Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False))
        with patch('sight_photo_gallery.read_gallery_screenshot', return_value=frame()):
            first = capture_sight_photo_gallery(device, tmp, NAME, stop, max_pages=1)
        return first, stop

    def test_resume_uses_previous_photo_as_anchor_and_writes_only_next_pages(self):
        with TemporaryDirectory() as tmp:
            first, stop = self.checkpoint(tmp)
            previous_report = Path(first['file'])
            previous_archive = Path(tmp) / first['archive']
            before = (previous_report.read_bytes(), previous_archive.read_bytes())
            device = Mock()
            device.snapshot.side_effect = [viewer(1), viewer(1), viewer(2), viewer(2), viewer(3), viewer(3)]
            with patch('sight_photo_gallery.read_gallery_screenshot', return_value=frame()):
                result = capture_sight_photo_gallery(device, tmp, NAME, stop, max_pages=2, resume_run=first['run_id'])
            self.assertEqual(result['previous_run_id'], first['run_id'])
            self.assertEqual((result['first_page'], result['last_page'], result['page_count']), (2, 3, 2))
            self.assertEqual([row['page'] for row in result['pages']], [2, 3])
            self.assertEqual(device.run.call_count, 2)
            for call in device.run.call_args_list:
                self.assertEqual(call.args[:3], ('shell', 'input', 'swipe'))
            self.assertEqual((previous_report.read_bytes(), previous_archive.read_bytes()), before)

    def test_slow_grid_and_viewer_transitions_wait_without_repeating_input(self):
        device = Mock()
        device.snapshot.side_effect = [PREVIEW, PREVIEW, GRID, GRID, viewer(1), viewer(1),
                                       viewer(1), viewer(2), viewer(2)]
        stop = Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False))
        with TemporaryDirectory() as tmp, patch('sight_photo_gallery.read_gallery_screenshot', return_value=frame()):
            result = capture_sight_photo_gallery(device, tmp, NAME, stop, max_pages=2)
        self.assertEqual([row['page'] for row in result['pages']], [1, 2])
        self.assertEqual([call.args[2] for call in device.run.call_args_list], ['tap', 'tap', 'swipe'])

    def test_cancellation_during_initial_load_does_not_create_a_failed_batch(self):
        device = Mock()
        device.snapshot.return_value = PREVIEW
        stop = Mock(is_set=Mock(return_value=False), wait=Mock(return_value=True))
        with TemporaryDirectory() as tmp:
            result = capture_sight_photo_gallery(device, tmp, NAME, stop, max_pages=2)
            self.assertEqual(result, {'reason': 'cancelled', 'complete': False})
            self.assertEqual(list(Path(tmp).iterdir()), [])
        device.run.assert_called_once()

    def test_resume_rejects_other_picture_at_same_page_without_input_or_new_files(self):
        with TemporaryDirectory() as tmp:
            first, stop = self.checkpoint(tmp)
            before = set(Path(tmp).iterdir())
            device = Mock()
            device.snapshot.return_value = viewer(1)
            with patch('sight_photo_gallery.read_gallery_screenshot', return_value=frame('green')):
                with self.assertRaisesRegex(ValueError, '图片.*不一致'):
                    capture_sight_photo_gallery(device, tmp, NAME, stop, max_pages=2, resume_run=first['run_id'])
            device.run.assert_not_called()
            self.assertEqual(set(Path(tmp).iterdir()), before)

    def test_old_bundle_can_resume_and_changed_archive_photo_is_rejected(self):
        with TemporaryDirectory() as tmp:
            first, _ = self.checkpoint(tmp)
            folder = Path(tmp).resolve()
            path = folder / first['archive']
            asset = first['pages'][0]['file']
            with ZipFile(path) as archive:
                photo = archive.read(asset)
            old_report = {key: value for key, value in first.items()
                          if key not in {'first_page', 'last_page', 'previous_run_id'}}
            Path(first['file']).write_text(json.dumps(old_report), encoding='utf-8')
            manifest = json.dumps({key: value for key, value in old_report.items() if key != 'file'})
            with ZipFile(path, 'w') as archive:
                archive.writestr('manifest.json', manifest)
                archive.writestr(asset, photo)
            self.assertEqual(read_checkpoint(folder, NAME, first['run_id'])[:2], (896, 1))
            with ZipFile(path, 'w') as archive:
                archive.writestr('manifest.json', manifest)
                archive.writestr(asset, b'changed photo')
            with self.assertRaisesRegex(ValueError, 'ZIP 与报告无法核对'):
                read_checkpoint(folder, NAME, first['run_id'])

    def test_reads_one_counter_next_to_total_and_rejects_missing_image(self):
        self.assertEqual(read_viewer(viewer(1), 896)[0], 1)
        with self.assertRaises(ValueError):
            read_viewer(viewer(1, 500), 896)
        with self.assertRaises(ValueError):
            read_viewer(viewer(1).replace('o_cc_picture_show', 'unknown'), 896)

    def test_consecutive_pages_reference_one_cropped_png_if_pixels_match(self):
        device = Mock()
        device.snapshot.side_effect = [PREVIEW, GRID, viewer(1), viewer(1), viewer(2), viewer(2)]
        with TemporaryDirectory() as tmp, patch('sight_photo_gallery.read_gallery_screenshot', return_value=frame()):
            result = capture_sight_photo_gallery(device, tmp, NAME, Event(), max_pages=2)
            self.assertEqual(result['reason'], 'page_budget_reached')
            self.assertEqual((result['page_count'], result['unique_image_count'], result['gallery_total']), (2, 1, 896))
            self.assertFalse(result['complete'])
            self.assertEqual(json.loads(Path(result['file']).read_text('utf-8')), result)
            with ZipFile(Path(tmp) / result['archive']) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                pages = manifest['pages']
                self.assertEqual([row['page'] for row in pages], [1, 2])
                self.assertEqual(pages[0]['file'], pages[1]['file'])
                self.assertEqual(len(archive.namelist()), 2)
                with Image.open(io.BytesIO(archive.read(pages[0]['file']))) as crop:
                    self.assertEqual(crop.size, (400, 400))
                    self.assertEqual(crop.getpixel((0, 0)), (135, 206, 235))

    def test_skipped_page_keeps_previous_page_and_stops(self):
        device = Mock()
        device.snapshot.side_effect = [PREVIEW, GRID, viewer(1), viewer(1), viewer(3)]
        with TemporaryDirectory() as tmp, patch('sight_photo_gallery.read_gallery_screenshot', return_value=frame()):
            result = capture_sight_photo_gallery(device, tmp, NAME, Event(), max_pages=3)
            self.assertEqual((result['page_count'], result['reason']), (1, 'screen_changed'))
            self.assertEqual(device.run.call_count, 3)
