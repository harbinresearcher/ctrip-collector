import tempfile
import unittest
import csv
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

from runner import collect

FIXTURE = Path(__file__).parent / 'fixtures' / 'hotel-comments.xml'


def mark_filter(xml, label):
    root = ET.fromstring(xml)
    group = next(node for node in root.iter('node') if node.get('content-desc', '').startswith(label + ', '))
    group.set('selected', 'true')
    return ET.tostring(root, encoding='unicode')


class MobileReviewContextTests(unittest.TestCase):
    def test_run_csv_excludes_previous_unseen_rows_and_follows_replay_order(self):
        from core import parse_reviews
        from workbench_result_files import open_task_csv
        original = FIXTURE.read_text('utf-8')
        body = parse_reviews(original, 'hotel')[0]['content']
        first = original.replace(body, '这次首先读到的点评')
        with tempfile.TemporaryDirectory() as folder:
            def load_report(frames, target=500, refresh=False):
                stop = Mock(is_set=lambda: False, wait=lambda _: False)
                device = Mock()
                device.snapshot.side_effect = frames
                return collect(device, 'hotel', '测试酒店', folder, limit=target, max_swipes=len(frames)-1,
                               object_id='123', refresh=refresh, stop=stop)

            old = original.replace(body, '上次才看到的点评')
            load_report([old, original])
            report = load_report([first, original, first], refresh=True)
            self.assertEqual((report['new_count'], report['count']), (1, 3))
            self.assertEqual(report['sample_csv_count'], 2)
            with open_task_csv(folder, {'status': 'finished', 'result': report}, sample=True) as stream:
                rows = list(csv.DictReader(stream.read().decode('utf-8-sig').splitlines()))
            self.assertEqual([row['content'] for row in rows], ['这次首先读到的点评', body])
            self.assertEqual([row['run_order'] for row in rows], ['1', '2'])
            self.assertTrue(rows[1]['run_first_seen_source'].endswith('/00001.xml')
                            or rows[1]['run_first_seen_source'].endswith('\\00001.xml'))
            with Path(report['csv']).open(encoding='utf-8-sig', newline='') as source:
                self.assertIn('上次才看到的点评', [row['content'] for row in csv.DictReader(source)])
            skipped = load_report([old], target=1)
            self.assertIsNone(skipped['sample_csv'])
            self.assertEqual(skipped['sample_csv_count'], 0)
            self.assertTrue(Path(report['sample_csv']).is_file())

    def test_new_and_seen_counts_are_separate_from_the_selected_filter_total(self):
        xml = mark_filter(FIXTURE.read_text('utf-8'), '全部')
        with tempfile.TemporaryDirectory() as folder:
            first = collect(Mock(**{'snapshot.return_value': xml}), 'hotel', '测试酒店', folder,
                            max_swipes=0, object_id='123')
            second = collect(Mock(**{'snapshot.return_value': xml}), 'hotel', '测试酒店', folder,
                             max_swipes=0, object_id='123')
            self.assertEqual(first['new_count'], 1)
            self.assertEqual(second['new_count'], 0)
            counts = second['review_count_check']
            self.assertEqual(counts['current_run_unique'], 1)
            self.assertEqual(counts['display_total'], 5722)
            self.assertEqual(counts['difference'], 5721)
            self.assertEqual(counts['state'], 'compared_display_only')
            self.assertEqual(second['reason'], 'screen_budget_reached')
            self.assertFalse(second['complete'])

    def test_unknown_approximate_or_changed_total_is_not_used_for_a_difference(self):
        base = FIXTURE.read_text('utf-8')
        explicit = mark_filter(base, '全部')
        cases = (('selection_unconfirmed', [base]), ('selection_unconfirmed', [explicit,base]),
                 ('approximate_count', [explicit.replace('5722', '5千+')]),
                 ('count_not_stable', [explicit,explicit.replace('5722', '5723')]))
        for expected, screens in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as folder:
                device = Mock()
                device.snapshot.side_effect = screens
                stop = Mock(is_set=lambda: False, wait=lambda _: False)
                report = collect(device, 'hotel', '测试酒店', folder, max_swipes=len(screens)-1,
                                 object_id='123', stop=stop)
                check = report['review_count_check']
                self.assertEqual(check['state'], expected)
                self.assertIsNone(check['display_total'])
                self.assertIsNone(check['difference'])
                self.assertFalse(report['complete'])

    def test_historical_filter_counts_are_not_proof_of_active_filter_or_sort(self):
        from mobile_review_context import read_review_context
        xml = FIXTURE.read_text('utf-8')
        result = read_review_context(ET.fromstring(xml), 'hotel')
        self.assertEqual(result['visible_filters'], [
            {'label': '全部', 'display_count': '5722'},
            {'label': '有图·视频', 'display_count': '2753'},
            {'label': '差评', 'display_count': '63'},
        ])
        self.assertIsNone(result['selected_filter'])
        self.assertTrue(result['latest_control_visible'])
        self.assertIsNone(result['sort_order'])
        sight_xml = (FIXTURE.parent / 'ctrip-comments2.xml').read_text('utf-8')
        result = read_review_context(ET.fromstring(sight_xml), 'sight')
        self.assertIn({'label': '消费后评价', 'display_count': '551'}, result['visible_filters'])
        self.assertIsNone(result['selected_filter'])

    def test_explicit_filter_change_stops_before_storing_new_screen(self):
        from core import parse_reviews
        xml = FIXTURE.read_text('utf-8')
        original = parse_reviews(xml, 'hotel')[0]['content']
        changed = xml.replace(original, '第二个筛选组的额外点评')
        adb = Mock()
        adb.snapshot.side_effect = [mark_filter(xml, '全部'), mark_filter(changed, '差评')]
        stop = Mock(is_set=lambda: False, wait=lambda _: False)
        with tempfile.TemporaryDirectory() as folder:
            report = collect(adb, 'hotel', '测试酒店', folder, max_swipes=5, object_id='123', stop=stop)
            self.assertEqual(report['reason'], 'review_filter_changed')
            self.assertEqual(report['count'], 1)
            self.assertEqual(report['sampling_context']['selected_filter'], '全部')
            self.assertEqual(report['sampling_context']['rejected_context']['selected_filter'], '差评')
            self.assertNotIn('第二个筛选组', Path(report['csv']).read_text(encoding='utf-8-sig'))
            self.assertEqual(adb.run.call_count, 1)
