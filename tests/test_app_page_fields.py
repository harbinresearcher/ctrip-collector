import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

from workbench_jobs import make_job


FIXTURES = Path(__file__).parent / 'fixtures'
COMMENTS = (FIXTURES / 'ctrip-comments.xml').read_text('utf-8')
GUIDE = (FIXTURES / 'sight-guide-overview.xml').read_text('utf-8')
NAME = '长白山西景区'
RUN = '20261122T214455-012abc'


class AppPageFieldsTests(unittest.TestCase):
    def test_current_screen_extracts_selected_fields_and_writes_a_report(self):
        with TemporaryDirectory() as tmp, patch('workbench_jobs.Adb') as factory:
            device = factory.return_value.__enter__.return_value
            device.snapshot.return_value = COMMENTS
            request = {'mode': 'app_page_fields', 'name': NAME, 'field_labels': ['游玩建议', '营业时间']}
            result = make_job(request, tmp, 'test-adb')(Event(), lambda _: None)
            self.assertEqual(result['input_source'], 'live_android')
            fields = {field['label']: field for field in result['screens'][0]['fields']}
            self.assertTrue(fields['游玩建议']['values'][0].startswith('提前关注天气'))
            self.assertEqual(fields['游玩建议']['status'], 'matched')
            self.assertEqual(fields['营业时间']['status'], 'not_found')
            self.assertNotIn('游玩体验', {field['label'] for field in result['screens'][0]['fields']})
            self.assertEqual(result['matched_screens'], 1)
            self.assertFalse(result['complete'])
            device.snapshot.assert_called_once_with()
            device.run.assert_not_called()
            self.assertEqual(json.loads(Path(result['report_file']).read_text('utf-8')), result)

    def test_saved_run_uses_two_real_layouts_and_keeps_unmatched_pages_out(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            archive = folder / 'raw' / RUN
            archive.mkdir(parents=True)
            (archive / '00000.xml').write_text(COMMENTS, encoding='utf-8')
            (archive / '00001.xml').write_text(GUIDE, encoding='utf-8')
            (archive / '00002.xml').write_text(COMMENTS.replace(NAME, '另一景点', 1), encoding='utf-8')
            request = {'mode': 'app_page_fields', 'name': NAME, 'field_labels': ['游玩建议', '全年'],
                       'field_archive_run': RUN}
            with patch('workbench_jobs.Adb') as factory:
                result = make_job(request, folder)(Event(), lambda _: None)
                factory.assert_not_called()
            self.assertEqual(result['input_source'], 'saved_android_pages')
            self.assertEqual(result['matched_screens'], 2)
            self.assertEqual(result['skipped_screens'], [{'screen': 3, 'reason': 'title_mismatch'}])
            self.assertEqual(result['screens'][1]['fields'][1]['values'], ['07:30-17:00开放（13:30停止入园）'])
            self.assertNotIn('observed_at', result['screens'][1])
            self.assertEqual((archive / '00001.xml').read_text('utf-8'), GUIDE)

    def test_multiple_explicit_values_are_visible_and_foreign_windows_are_ignored(self):
        root = ET.fromstring(COMMENTS)
        node = next(node for node in root.iter('node') if node.get('text') == '天气晴朗')
        node.set('text', '游玩体验：精选二')
        foreign = ET.SubElement(root, 'node', {'package':'other.app'})
        ET.SubElement(foreign, 'node', {'package':'ctrip.android.view', 'text':'游玩体验：伪值',
                                       'bounds':'[100,300][500,400]'})
        with TemporaryDirectory() as tmp, patch('workbench_jobs.Adb') as factory:
            factory.return_value.__enter__.return_value.snapshot.return_value = ET.tostring(root, encoding='unicode')
            request = {'mode':'app_page_fields', 'name': NAME, 'field_labels': ['游玩体验']}
            result = make_job(request, tmp, 'adb')(Event(), lambda _: None)
            field = result['screens'][0]['fields'][0]
            self.assertEqual(field['status'], 'multiple')
            self.assertEqual(len(field['values']), 2)
            self.assertIn('精选二', field['values'])
            self.assertNotIn('伪值', field['values'])

    def test_invalid_labels_and_archive_id_rejected_before_io(self):
        with TemporaryDirectory() as tmp:
            request = {'mode':'app_page_fields', 'name': NAME, 'field_labels': ['游玩建议']}
            with self.assertRaisesRegex(ValueError, '标签'):
                make_job(dict(request, field_labels=['游玩建议', ' 游玩建议 ']), tmp, 'adb')
            with self.assertRaisesRegex(ValueError, '编号'):
                make_job(dict(request, field_archive_run='../another'), tmp)
