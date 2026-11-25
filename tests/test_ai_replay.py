import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from ai_capture import parse_ai
from workbench_jobs import make_job


class AiReplayTests(unittest.TestCase):
    def test_replay_is_read_only_and_does_not_follow_paths_in_metadata(self):
        fixture = Path(__file__).parent / 'fixtures' / 'hotel-ai-expanded.xml'
        current = parse_ai(fixture.read_text(encoding='utf-8'))
        with TemporaryDirectory() as folder:
            root = Path(folder)
            ai = root / 'ai'
            ai.mkdir()
            stamp = '20261118T030405000000Z'
            record = {
                'source': 'ctrip_android_ui', 'kind': 'hotel', 'resource_id': 42, 'name': '测试酒店',
                'captured_at': '2026-11-18T03:04:05+00:00',
                'sections': [{'text': part['text'], 'linked_count': '999'} for part in current['sections']],
                'ai_selected_images': {'selected_count': 30, 'metadata_status': 'captured'},
                'evidence': 'PRIVATE_PATH_MUST_NOT_BE_USED',
            }
            original = ai / f'hotel-42-{stamp}.json'
            original.write_text(json.dumps(record), encoding='utf-8')
            xml = original.with_suffix('.xml')
            shutil.copyfile(fixture, xml)
            unrelated = ai / f'hotel-43-{stamp}.json'
            unrelated.write_text(json.dumps({**record, 'resource_id': 43, 'sections': [{'text': '另一份总结'}]}),
                                 encoding='utf-8')
            shutil.copyfile(fixture, unrelated.with_suffix('.xml'))
            before = {path.name: path.read_bytes() for path in ai.iterdir()}
            report = make_job({'mode': 'ai_replay'}, root)(Event(), lambda _: None)
            self.assertEqual(report['reason'], 'replayed')
            self.assertEqual(len(report['summaries']), 1)
            self.assertEqual(report['skipped'][0]['reason'], 'summary_text_mismatch')
            restored = report['summaries'][0]
            self.assertEqual(restored['captured_at'], record['captured_at'])
            self.assertEqual(restored['rating_dimensions'], current['rating_dimensions'])
            self.assertEqual(restored['sections'][0]['linked_count'], '3372')
            self.assertEqual(restored['ai_selected_images']['selected_count'], 30)
            self.assertFalse(report['complete'])
            output = Path(report['report_file']).read_text('utf-8')
            self.assertNotIn('PRIVATE_PATH', output)
            self.assertEqual(json.loads(output), report)
            self.assertEqual({path.name: path.read_bytes() for path in ai.iterdir()}, before)
