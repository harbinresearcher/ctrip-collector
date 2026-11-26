import io
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from restaurant_gallery_download import download_visible_photo


FOLDER = '/sdcard/Pictures/Trip'
NAME = 'image_name_1795659031394.webp'
OLD = 'image_name_1795659000000.webp'
BOX = (970, 154, 1054, 238)
TAP = ('shell', 'input', 'tap', '1012', '196')


def png_photo():
    output = io.BytesIO()
    Image.new('RGB', (9, 7), 'navy').save(output, 'PNG')
    return output.getvalue()


class AndroidSave:
    def __init__(self, payload, listings=None, sizes=None):
        self.payload = payload
        self.listings = iter(listings or [f'{OLD}\n{NAME[:-4]}png\n'])
        self.sizes = iter(sizes or [len(payload)])
        self.listing = None
        self.size = None
        self.commands = []
        self.before_tap = OLD + '\n'

    def run(self, *args, timeout=None):
        self.commands.append(args)
        if args == TAP:
            return ''
        if args[0] == 'pull':
            Path(args[2]).write_bytes(self.payload)
            return ''
        if args[0] == 'shell' and 'ls -1' in args[1]:
            if TAP not in self.commands:
                return self.before_tap
            self.listing = next(self.listings, self.listing)
            return self.listing
        if args[0] == 'shell' and 'stat -c' in args[1]:
            self.size = next(self.sizes, self.size)
            return '' if self.size is None else str(self.size) + '\n'
        raise AssertionError(args)


class GalleryDownloadTests(unittest.TestCase):
    def setUp(self):
        self.stop = Mock()
        self.stop.is_set.return_value = False
        self.stop.wait.return_value = False

    def test_keeps_app_bytes_and_checks_viewer_before_tap_and_after_pull(self):
        data = png_photo()
        device = AndroidSave(data, sizes=[0, 12, len(data), len(data)])
        checked_commands = []

        def verify_view():
            checked_commands.append(list(device.commands))

        payload, extension, dimensions = download_visible_photo(device, self.stop, BOX, verify_view)

        self.assertEqual((payload, extension, dimensions), (data, 'png', (9, 7)))
        self.assertEqual(device.commands.count(TAP), 1)
        self.assertNotIn(TAP, checked_commands[0])
        self.assertEqual(len(checked_commands), 2)
        activity = [command for command in device.commands if command[0] == 'pull']
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0][1], FOLDER + '/' + NAME[:-4] + 'png')
        self.assertIn(activity[0], checked_commands[1])
        self.assertFalse(Path(activity[0][2]).exists())

    def test_decodes_webp_as_written_by_the_app(self):
        output = io.BytesIO()
        Image.new('RGB', (9, 7), 'navy').save(output, 'WEBP')
        data = output.getvalue()
        device = AndroidSave(data, listings=[f'{OLD}\n{NAME}\n'])

        self.assertEqual(download_visible_photo(device, self.stop, BOX, Mock()), (data, 'webp', (9, 7)))

    def test_ignores_old_files_and_nonmatching_filenames(self):
        device = AndroidSave(png_photo(), listings=[OLD + '\nalbum.png\n../' + NAME])

        with self.assertRaisesRegex(ValueError, '新保存'):
            download_visible_photo(device, self.stop, BOX, Mock())

        self.assertEqual(device.commands.count(TAP), 1)
        self.assertFalse(any(command[0] == 'pull' for command in device.commands))

    def test_file_from_prior_download_appearing_during_verification_is_not_attributed_to_tap(self):
        name = NAME[:-4] + 'png'
        device = AndroidSave(png_photo(), listings=[f'{OLD}\n{name}\n'])

        def verify_view():
            device.before_tap = f'{OLD}\n{name}\n'

        with self.assertRaisesRegex(ValueError, '新保存'):
            download_visible_photo(device, self.stop, BOX, verify_view)

        self.assertEqual(device.commands.count(TAP), 1)
        self.assertFalse(any(command[0] == 'pull' for command in device.commands))

    def test_two_new_matching_paths_are_ambiguous(self):
        device = AndroidSave(png_photo(), listings=[
            f'{OLD}\n{NAME}\n', f'{OLD}\n{NAME}\nimage_name_1795659031395.webp\n',
        ])

        with self.assertRaisesRegex(ValueError, '多张'):
            download_visible_photo(device, self.stop, BOX, Mock())

        self.assertFalse(any(command[0] == 'pull' for command in device.commands))

    def test_rejects_oversize_file_before_copying(self):
        device = AndroidSave(png_photo(), sizes=[2049])

        with patch('restaurant_gallery_download.MAX_PHOTO_BYTES', 2048):
            with self.assertRaisesRegex(ValueError, '过大'):
                download_visible_photo(device, self.stop, BOX, Mock())

        self.assertFalse(any(command[0] == 'pull' for command in device.commands))

    def test_rejects_invalid_images_and_extension_mismatch(self):
        png = png_photo()
        for filename, payload in ((NAME, png), (NAME[:-4] + 'png', png[:50])):
            with self.subTest(filename=filename):
                device = AndroidSave(payload, listings=[f'{OLD}\n{filename}\n'])
                with self.assertRaisesRegex(ValueError, '图片'):
                    download_visible_photo(device, self.stop, BOX, Mock())

    def test_rejects_file_change_after_copy(self):
        data = png_photo()
        device = AndroidSave(data, sizes=[len(data), len(data), len(data) + 1])

        with self.assertRaisesRegex(ValueError, '变化'):
            download_visible_photo(device, self.stop, BOX, Mock())

    def test_viewer_check_failure_does_not_return_a_photo(self):
        for changes_before_tap in (True, False):
            with self.subTest(changes_before_tap=changes_before_tap):
                device = AndroidSave(png_photo())
                checks = [ValueError('页码变化')] if changes_before_tap else [None, ValueError('页码变化')]
                with self.assertRaisesRegex(ValueError, '页码变化'):
                    download_visible_photo(device, self.stop, BOX, Mock(side_effect=checks))
                self.assertEqual(device.commands.count(TAP), 0 if changes_before_tap else 1)

    def test_stop_before_tap_leaves_device_untouched(self):
        device = AndroidSave(png_photo())
        self.stop.is_set.return_value = True

        with self.assertRaises(InterruptedError):
            download_visible_photo(device, self.stop, BOX, Mock())

        self.assertEqual(device.commands, [])

    def test_stop_while_waiting_does_not_pull(self):
        device = AndroidSave(png_photo())
        self.stop.wait.return_value = True

        with self.assertRaises(InterruptedError):
            download_visible_photo(device, self.stop, BOX, Mock())

        self.assertEqual(device.commands.count(TAP), 1)
        self.assertFalse(any(command[0] == 'pull' for command in device.commands))
