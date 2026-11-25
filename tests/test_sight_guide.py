import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
from xml.etree import ElementTree as ET

from sight_guide import parse_sight_guide
from workbench_jobs import make_job


FIXTURES = Path(__file__).parent / 'fixtures'
NAME = '长白山西景区'


class SightGuideTests(unittest.TestCase):
    def test_historical_guide_binds_opening_and_booking_fields(self):
        result = parse_sight_guide((FIXTURES / 'sight-guide-overview.xml').read_text('utf-8'), NAME)
        self.assertEqual(result['visit_duration_text'], '建议时长约0.5-1天')
        self.assertEqual(result['opening_status_text'], '今日已闭园')
        self.assertEqual(result['opening_periods'], [
            {'period_text': '全年', 'hours_text': '07:30-17:00开放（13:30停止入园）'}])
        self.assertTrue(any('现场公示' in text for text in result['opening_notes']))
        self.assertEqual(result['ticket_price_texts'], ['门票¥190起'])
        self.assertEqual(result['booking_notes'], ['补充说明：景区交通车另收费，详情以景区现场公示为准'])
        self.assertFalse(result['complete'])

    def test_scrolled_guide_does_not_borrow_opening_info_from_any_other_group(self):
        root = ET.parse(FIXTURES / 'sight-guide-scrolled.xml').getroot()
        price = next(node for node in root.iter('node') if node.get('text') == '门票¥190起')
        price.set('package', 'other.app')
        result = parse_sight_guide(ET.tostring(root, encoding='unicode'), NAME)
        self.assertIsNone(result['visit_duration_text'])
        self.assertEqual(result['opening_periods'], [])
        self.assertEqual(result['ticket_price_texts'], [])
        self.assertEqual(len(result['booking_notes']), 1)

    def test_lower_body_cannot_replace_the_toolbar_title(self):
        root = ET.parse(FIXTURES / 'sight-guide-overview.xml').getroot()
        title = next(node for node in root.iter('node') if node.get('text') == NAME)
        title.set('text', '另一处景点')
        nav = next(node for node in root.iter('node') if node.get('resource-id') == 'gs_sightmoredetail_view_opentime')
        ET.SubElement(nav, 'node', {'text': NAME, 'package': 'ctrip.android.view'})
        with self.assertRaisesRegex(ValueError, '标题'):
            parse_sight_guide(ET.tostring(root, encoding='unicode'), NAME)
        with self.assertRaisesRegex(ValueError, '必看指南'):
            parse_sight_guide((FIXTURES / 'sight-detail-ticket.xml').read_text('utf-8'), NAME)

    def test_workbench_persists_and_inventory_counts_standalone_guide(self):
        with TemporaryDirectory() as folder, patch('workbench_jobs.Adb') as adb:
            device = adb.return_value.__enter__.return_value
            device.snapshot.return_value = (FIXTURES / 'sight-guide-overview.xml').read_text('utf-8')
            job = make_job({'mode': 'sight_guide', 'name': NAME}, Path(folder), 'adb', adb_serial='emulator-5554')
            result = job(Event(), lambda _: None)
            self.assertEqual(device.ensure_foreground.call_count, 2)
            self.assertTrue(result['captured_at'].endswith('+08:00'))
            self.assertEqual(json.loads(Path(result['file']).read_text('utf-8')), result)
            summary = make_job({'mode': 'coverage'}, Path(folder))(Event(), lambda _: None)['android']
            self.assertEqual(len(summary['sight_guides']), 1)
            self.assertEqual(summary['sight_guides'][0]['opening_period_count'], 1)
            self.assertEqual(summary['sight_details'], [])
