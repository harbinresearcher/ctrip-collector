import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
from xml.etree import ElementTree as ET

from workbench_jobs import make_job


FIXTURES = Path(__file__).parent / 'fixtures'
NAME = '长白山西景区'


class SightAiTests(unittest.TestCase):
    def test_requested_expansion_reads_all_visible_sections_from_the_same_card(self):
        preview = (FIXTURES / 'ctrip-comments.xml').read_text('utf-8').replace('1095条点评', '18.0w条点评')
        root = ET.fromstring(preview)
        parents = {child: parent for parent in root.iter() for child in parent}
        control = next(node for node in root.iter('node') if node.get('text') == '查看全部总结')
        control.set('text', '收起')
        row = parents[control]
        panel = parents[row]
        for value in ('设施服务：有游客中心。', '注意事项：出行前查看天气。'):
            panel.insert(list(panel).index(row), ET.Element('node', {
                'package': 'ctrip.android.view', 'text': value, 'bounds': '[155,1300][1348,1415]',
            }))
        expanded = ET.tostring(root, encoding='unicode')
        with TemporaryDirectory() as output, patch('workbench_jobs.Adb') as adb:
            device = adb.return_value.__enter__.return_value
            device.snapshot.side_effect = [preview, expanded]
            job = make_job({'mode': 'sight_ai', 'name': NAME, 'expand_sight_ai': True}, output, 'adb')
            result = job(Event(), lambda _: None)
            device.run.assert_called_once_with('shell', 'input', 'tap', '691', '1491')
            self.assertEqual([row['topic'] for row in result['sections']],
                             ['游玩体验', '游玩建议', '设施服务', '注意事项'])
            self.assertEqual(result['summary_state'], 'expanded')
            self.assertEqual(result['review_display']['count_text'], '18.0w条点评')
            self.assertEqual(result['raw_hash'], hashlib.sha256(preview.encode('utf-8')).hexdigest())
            self.assertEqual(result['expanded_raw_hash'], hashlib.sha256(expanded.encode('utf-8')).hexdigest())
            self.assertEqual(result['ai_selected_images']['display_count'], 896)
            self.assertFalse(result['complete'])
            self.assertEqual(json.loads(Path(result['file']).read_text('utf-8')), result)

    def test_unverified_expansion_retains_original_preview(self):
        preview = (FIXTURES / 'ctrip-comments.xml').read_text('utf-8')
        for response in (preview, preview.replace(NAME, '另一处景点').replace('查看全部总结', '收起')):
            with self.subTest(changed=response != preview), TemporaryDirectory() as output, \
                 patch('workbench_jobs.Adb') as adb, patch('sight_ai.capture_thumbnails', return_value=('no_visible_tiles', [])):
                device = adb.return_value.__enter__.return_value
                device.snapshot.side_effect = [preview, preview, response]
                job = make_job({'mode': 'sight_ai', 'name': NAME, 'expand_sight_ai': True,
                                'save_sight_gallery_thumbnails': True}, output, 'adb')
                result = job(Event(), lambda _: None)
                self.assertEqual(device.snapshot.call_count, 3)
                device.run.assert_called_once()
                self.assertEqual(result['summary_state'], 'preview')
                self.assertNotIn('expanded_raw_hash', result)
                self.assertEqual(len(result['sections']), 2)

    def run_job(self, output, xml, name=NAME):
        with patch('workbench_jobs.Adb') as adb:
            device = adb.return_value.__enter__.return_value
            device.snapshot.return_value = xml
            job = make_job({'mode': 'sight_ai', 'name': name}, output, 'adb', adb_serial='test-device')
            result = job(Event(), lambda _: None)
            device.snapshot.assert_called_once_with()
            self.assertEqual(device.ensure_foreground.call_count, 2)
            device.run.assert_not_called()
            return result

    def test_sight_summary_record_preview_input_and_coverage(self):
        xml = (FIXTURES / 'ctrip-comments.xml').read_text('utf-8')
        with TemporaryDirectory() as output:
            result = self.run_job(output, xml)
            self.assertEqual(result['raw_hash'], hashlib.sha256(xml.encode('utf-8')).hexdigest())
            self.assertEqual(result['name'], NAME)
            self.assertEqual(result['identity_verification'], 'operator_name_exact_match')
            self.assertNotIn('resource_id', result)
            self.assertEqual([row['topic'] for row in result['sections']], ['游玩体验', '游玩建议'])
            self.assertTrue(all(row['body'] for row in result['sections']))
            self.assertTrue(result['expand_entry_visible'])
            self.assertEqual(result['review_display'], {'score_text': '4.4/5', 'count_text': '1095条点评'})
            self.assertEqual(result['rating_dimensions'], [
                {'label': '景色', 'score_text': '4.6'}, {'label': '趣味', 'score_text': '4.4'},
                {'label': '性价比', 'score_text': '4.3'}])
            self.assertEqual(result['ai_selected_images'], {
                'display_count': 896, 'visible_labels': ['天气晴朗', '自然景观远景', '山川', '自然景观近景']})
            self.assertFalse(result['complete'])
            self.assertEqual(json.loads(Path(result['file']).read_text('utf-8')), result)
            self.assertEqual(list(Path(output).glob('*.xml')), [])
            coverage = make_job({'mode': 'coverage'}, output)(Event(), lambda _: None)['android']
            self.assertEqual(coverage['ai_summaries'], [])
            self.assertEqual(coverage['sight_ai_summaries'][0]['section_count'], 2)

    def test_rejects_scrolled_screen_wrong_identity_and_missing_ai_marker(self):
        root = ET.parse(FIXTURES / 'ctrip-comments.xml').getroot()
        marker = next(node for node in root.iter('node') if node.get('text') == '由AI生成')
        marker.set('package', 'another.app')
        with TemporaryDirectory() as output:
            cases = (
                ((FIXTURES / 'ctrip-comments.xml').read_text('utf-8'), '另一处景点', '标题'),
                ((FIXTURES / 'ctrip-comments2.xml').read_text('utf-8'), NAME, 'AI 总结'),
                (ET.tostring(root, encoding='unicode'), NAME, '由AI生成'),
            )
            for xml, name, message in cases:
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    self.run_job(output, xml, name)
            self.assertEqual(list(Path(output).iterdir()), [])
