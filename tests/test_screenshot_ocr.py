import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from PIL import Image
from screenshot_ocr import recognize_screenshot


def screenshot(fmt='PNG'):
    stream = io.BytesIO()
    Image.new('RGB', (128, 64), 'white').save(stream, format=fmt)
    return stream.getvalue()


class ScreenshotOcrTests(unittest.TestCase):
    def test_result_retains_engine_text_and_only_persists_json(self):
        with TemporaryDirectory() as tmp, patch('screenshot_ocr.read_windows_ocr') as native:
            def recognize(image):
                with Image.open(image) as decoded:
                    self.assertEqual(decoded.size, (128, 64))
                return ['天 池 海 拔 2178 · 1 米', '时 间 07:30']
            native.side_effect = recognize
            report = recognize_screenshot(screenshot(), tmp, Event(), lambda _: None)
            self.assertEqual(report['ocr_language'], 'zh-Hans-CN')
            self.assertEqual(report['lines'], ['天 池 海 拔 2178 · 1 米', '时 间 07:30'])
            self.assertEqual(report['text'], '天池海拔 2178 · 1 米\n时间 07:30')
            self.assertEqual(report['image_size'], [128, 64])
            self.assertEqual(json.loads(Path(report['report_file']).read_text('utf-8')), report)
            self.assertEqual([p.suffix for p in Path(tmp).iterdir()], ['.json'])
            self.assertFalse(report['complete'])

    def test_bad_images_and_cancellation_do_not_create_files(self):
        with TemporaryDirectory() as tmp, patch('screenshot_ocr.read_windows_ocr') as native:
            for data in (b'not an image', screenshot('GIF')):
                with self.assertRaisesRegex(ValueError, 'PNG|JPEG'):
                    recognize_screenshot(data, tmp, Event(), lambda _: None)
            stopped = Event()
            stopped.set()
            self.assertEqual(recognize_screenshot(screenshot(), tmp, stopped, lambda _: None)['reason'], 'cancelled')
            native.assert_not_called()
            self.assertEqual(list(Path(tmp).iterdir()), [])
