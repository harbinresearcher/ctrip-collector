import json
import logging
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from workbench_jobs import make_job

logger = logging.getLogger(__name__)
HISTORICAL = Path(__file__).parent / 'fixtures' / 'app-city-recommendations.xml'


class AppCityTests(unittest.TestCase):
    def test_historical_city_candidates_through_workbench_and_coverage(self):
        logger.info('开始历史城市推荐发现测试')
        with TemporaryDirectory() as folder, patch('workbench_jobs.Adb') as adb:
            device = adb.return_value.__enter__.return_value
            device.snapshot.return_value = HISTORICAL.read_text('utf-8')
            report = make_job({'mode': 'app_city', 'name': '抚松'}, folder, 'fake-adb')(Event(), logger.info)
            self.assertEqual(report['city'], '抚松')
            self.assertEqual([row['name'] for row in report['objects']], ['长白山西景区', '讷殷古城', '长白山景区'])
            self.assertEqual([row['nearby_label_visible'] for row in report['objects']], [False, False, True])
            self.assertEqual([row['visible_order'] for row in report['objects']], [1, 2, 3])
            self.assertTrue(all('resource_id' not in row and 'url' not in row for row in report['objects']))
            self.assertNotIn('问AI或按住说话', json.dumps(report, ensure_ascii=False))
            self.assertFalse(report['complete'])
            self.assertEqual(json.loads(Path(report['report_file']).read_text('utf-8')), report)
            device.run.assert_not_called()
            coverage = make_job({'mode': 'coverage'}, folder)(Event(), logger.info)
            self.assertEqual(coverage['android']['city_recommendations'][0]['candidate_count'], 3)
        logger.info('历史城市推荐发现测试完成')

    def test_wrong_city_and_foreign_card_evidence_are_rejected(self):
        logger.info('开始城市与卡片身份边界测试')
        from app_city_discovery import parse_city_recommendations
        xml = HISTORICAL.read_text('utf-8')
        with self.assertRaisesRegex(ValueError, '城市'):
            parse_city_recommendations(xml, '长白山西景区')
        root = ET.fromstring(xml)
        card = next(node for node in root.iter('node') if node.get('resource-id') == 'gs_searchlist_view_mustsees_item')
        for child in card:
            if child.get('text') == card.get('content-desc'):
                child.set('package', 'other.app')
        modified = parse_city_recommendations(ET.tostring(root, encoding='unicode'), '抚松')
        self.assertEqual(modified['skipped_cards'], 1)
        self.assertEqual([row['name'] for row in modified['objects']], ['讷殷古城', '长白山景区'])
        logger.info('城市与卡片身份边界测试完成')
