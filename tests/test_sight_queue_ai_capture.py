import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from sight_ai import capture_queue_sight_ai


FIXTURES = Path(__file__).parent / 'fixtures'
NAME = '长白山西景区'
OBJECT = '136039'
QUEUE = '1' * 32
REPORT = '2' * 32


class FastStop(Event):
    def wait(self, timeout=None):
        return self.is_set()


def expanded_frame(preview):
    root = ET.fromstring(preview)
    control = next(node for node in root.iter('node') if node.get('text') == '查看全部总结')
    control.set('text', '收起')
    return ET.tostring(root, encoding='unicode')


class SightQueueAiCaptureTests(unittest.TestCase):
    def setUp(self):
        self.preview = (FIXTURES / 'ctrip-comments.xml').read_text('utf-8')
        self.scrolled = (FIXTURES / 'ctrip-comments2.xml').read_text('utf-8')
        self.device = Mock()
        self.stop = FastStop()

    def capture(self, output):
        return capture_queue_sight_ai(self.device, output, NAME, OBJECT, QUEUE, self.stop, report_id=REPORT)

    def test_recovers_preview_expands_persists_bound_report_and_restores_reviews(self):
        self.device.snapshot.side_effect = [self.scrolled, self.preview, expanded_frame(self.preview), self.preview]
        with TemporaryDirectory() as output:
            summary = self.capture(output)
            path = Path(output) / f'sight-ai-{REPORT}.json'
            data = json.loads(path.read_text('utf-8'))
            self.assertEqual(summary, {'status': 'saved', 'report_file': path.name,
                                       'section_count': 2, 'summary_state': 'expanded'})
            self.assertEqual((data['kind'], data['name'], data['queue_id'], data['object_id']),
                             ('sight', NAME, QUEUE, OBJECT))
            self.assertFalse(data['complete'])
            self.assertFalse(data['identity_id_verified'])
            commands = [call.args for call in self.device.run.call_args_list]
            self.assertEqual([args[2] for args in commands], ['swipe', 'tap', 'tap'])
            self.assertLess(int(commands[0][4]), int(commands[0][6]))
            self.assertEqual(commands[1][3:], ('691', '1491'))
            self.assertEqual(commands[2][3:], ('691', '1491'))

    def test_bounded_search_does_not_treat_no_visible_summary_as_empty_or_complete(self):
        self.device.snapshot.return_value = self.scrolled
        with TemporaryDirectory() as output:
            self.assertEqual(self.capture(output), {'status': 'not_visible'})
            self.assertEqual(list(Path(output).iterdir()), [])
            commands = [call.args for call in self.device.run.call_args_list]
            self.assertGreaterEqual(len(commands), 1)
            self.assertLessEqual(len(commands), 4)
            self.assertTrue(all(args[2] == 'swipe' for args in commands))

    def test_failed_restore_stops_input_and_retains_the_valid_ai_result(self):
        expanded = expanded_frame(self.preview)
        other_page = self.preview.replace(NAME, '白云山')
        self.device.snapshot.side_effect = [self.preview, expanded, other_page]
        with TemporaryDirectory() as output, self.assertRaisesRegex(RuntimeError, '景点.*点评'):
            try:
                self.capture(output)
            finally:
                path = Path(output) / f'sight-ai-{REPORT}.json'
                self.assertEqual(json.loads(path.read_text('utf-8'))['summary_state'], 'expanded')
                self.assertEqual(self.device.run.call_count, 2)

    def test_cannot_collect_from_wrong_object_or_after_confirmation_prompt(self):
        for bad in (self.preview.replace(NAME, '白云山'), self.preview.replace('好评', '请先登录')):
            with self.subTest(), TemporaryDirectory() as output:
                self.device.snapshot.return_value = bad
                with self.assertRaises(RuntimeError):
                    self.capture(output)
                self.device.run.assert_not_called()
                self.assertEqual(list(Path(output).iterdir()), [])

    def test_preview_is_preserved_if_tapping_expand_does_not_reveal_more(self):
        self.device.snapshot.return_value = self.preview
        with TemporaryDirectory() as output:
            summary = self.capture(output)
            data = json.loads((Path(output) / summary['report_file']).read_text('utf-8'))
            self.assertEqual(data['summary_state'], 'preview')
            self.assertNotIn('expanded_raw_hash', data)
            self.assertEqual(self.device.run.call_count, 2)

    def test_rechecks_visible_preview_button_before_retrying_a_missed_expansion(self):
        self.device.snapshot.side_effect = [self.preview, self.preview, self.preview,
                                           expanded_frame(self.preview), self.preview]
        with TemporaryDirectory() as output:
            summary = self.capture(output)
            self.assertEqual(summary['summary_state'], 'expanded')
            self.assertEqual([command.args[2] for command in self.device.run.call_args_list],
                             ['tap', 'tap', 'tap'])

    def test_failed_report_write_is_independent_of_review_collection(self):
        self.device.snapshot.return_value = self.preview
        with TemporaryDirectory() as output, patch.object(Path, 'replace', side_effect=OSError('磁盘不可写')):
            summary = self.capture(output)
            self.assertEqual(summary['status'], 'error')
            self.assertEqual(list(Path(output).iterdir()), [])

    def test_stop_during_report_write_preserves_cancellation(self):
        self.device.snapshot.return_value = self.preview

        def cancel_before_publish(temporary, path, result, stop):
            temporary.write_text('{}', encoding='utf-8')
            stop.set()
            return False

        with TemporaryDirectory() as output, patch('sight_ai.save_summary_report', side_effect=cancel_before_publish):
            with self.assertRaises(InterruptedError):
                self.capture(output)
            self.assertEqual(list(Path(output).iterdir()), [])
            self.assertEqual(self.device.run.call_count, 2)

    def test_rejects_malformed_report_id_without_reading_the_device(self):
        with TemporaryDirectory() as output, self.assertRaises(ValueError):
            capture_queue_sight_ai(self.device, output, NAME, OBJECT, QUEUE, self.stop, report_id='../other')
        self.device.snapshot.assert_not_called()
