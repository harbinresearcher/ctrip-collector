import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from coverage_audit import write_coverage_audit
from workbench_sight_gap_plan import build_sight_gap_plan


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')


def write_object(folder, identity, *, kind='sight', address='山西省大同市云冈区云冈镇1号'):
    write_json(folder / f'{kind}-{identity}-object.json', {
        'kind': kind, 'resource_id': identity, 'name': f'景点{identity}',
        'display_total': 999, 'details': {'address': address},
    })


def write_app_report(folder, identity, *, total, reason='target_reached'):
    run_id = f'20261128T000000-{identity:06x}'
    write_json(folder / f'report-{run_id}.json', {
        'kind': 'sight', 'object_id': str(identity), 'place': f'景点{identity}', 'reason': reason,
        'run_id': run_id, 'review_count_check': {'filter': '全部', 'state': 'compared_display_only',
                                                 'display_total': total},
    })


def snapshot_files(folder):
    return {item.name: item.read_bytes() for item in folder.iterdir()}


class WorkbenchSightGapPlanTests(unittest.TestCase):
    def test_uses_only_app_counts_and_returns_no_paths_commands_or_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for identity in (1, 2, 4, 5):
                write_object(folder, identity)
            write_object(folder, 3, address='')
            write_object(folder, 6, kind='restaurant')
            write_app_report(folder, 2, total=10)
            write_app_report(folder, 4, total=10, reason='requires_manual_action')
            write_app_report(folder, 5, total=1)
            with closing(sqlite3.connect(folder / 'reviews.sqlite3')) as db, db:
                db.execute('CREATE TABLE reviews_scoped (kind TEXT, object_id TEXT)')
                db.executemany('INSERT INTO reviews_scoped VALUES (?, ?)', [('sight', '2')] * 4 + [('sight', '5')])

            before = snapshot_files(folder)
            plan = build_sight_gap_plan(folder)
            self.assertEqual(snapshot_files(folder), before)
            self.assertEqual(plan, {
                'items': [
                    {'object_ref': 'sight-1', 'name': '景点1', 'target': None, 'collected': 0,
                     'missing': None, 'suggested_add': 200},
                    {'object_ref': 'sight-2', 'name': '景点2', 'target': 10, 'collected': 4,
                     'missing': 6, 'suggested_add': 6},
                ],
                'skipped_count': 1, 'missing_address_count': 1,
            })

    def test_applies_cap_after_looking_for_address_verified_sights(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for identity in range(100, 121):
                write_object(folder, identity, address=None)
            for identity in range(200, 222):
                write_object(folder, identity)

            plan = build_sight_gap_plan(folder)
            self.assertEqual([row['object_ref'] for row in plan['items']],
                             [f'sight-{identity}' for identity in range(200, 220)])
            self.assertEqual(plan['missing_address_count'], 21)

    def test_empty_folder_and_unreadable_scoped_database_have_different_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertEqual(build_sight_gap_plan(folder / 'future'),
                             {'items': [], 'skipped_count': 0, 'missing_address_count': 0})
            write_object(folder, 1)
            (folder / 'reviews.sqlite3').write_text('unreadable', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '点评数据库'):
                build_sight_gap_plan(folder)

    def test_suggestion_fits_remaining_queue_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            write_object(folder, 1)
            write_object(folder, 2)
            counts = {'sight-1': 99999, 'sight-2': 100000}
            with patch('phone_queue.phone_review_counts', return_value=(counts, 'read')):
                plan = build_sight_gap_plan(folder)
            self.assertEqual([(row['object_ref'], row['suggested_add']) for row in plan['items']],
                             [('sight-1', 1)])
            self.assertEqual(plan['skipped_count'], 1)

    def test_latest_verified_no_review_entry_is_skipped_until_a_usable_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            write_object(folder, 1)
            no_entry = folder / 'report-20261127T000000-000001.json'
            write_json(no_entry, {
                'kind': 'sight', 'object_id': '1', 'place': '景点1', 'reason': 'no_review_entry',
                'detail_seen': True, 'review_entry_missing': True,
            })
            os.utime(no_entry, (1_700_000_000, 1_700_000_000))
            self.assertEqual(build_sight_gap_plan(folder),
                             {'items': [], 'skipped_count': 1, 'missing_address_count': 0})

            write_app_report(folder, 1, total=15)
            usable = folder / 'report-20261128T000000-000001.json'
            os.utime(usable, (1_700_000_100, 1_700_000_100))
            plan = build_sight_gap_plan(folder)
            self.assertEqual([(item['object_ref'], item['target']) for item in plan['items']], [('sight-1', 15)])
            self.assertEqual(plan['skipped_count'], 0)

    def test_raw_labels_prevent_legacy_rounded_totals_from_becoming_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            cases = [
                (1, ['43000'], [{'label': '全部', 'display_count': 43000,
                                 'display_count_text': '4.3w'}], None),
                (2, ['4.3w'], [], None),
                (3, ['4.3w'], [{'label': '全部', 'display_count': 1099,
                               'display_count_text': '1,099'}], 1099),
                (4, ['43000'], [{'label': '全部', 'display_count': 43000,
                                 'display_count_text': None}], None),
            ]
            for identity, labels, filters, _ in cases:
                write_object(folder, identity)
                run_id = f'20261128T000000-{identity:06x}'
                write_json(folder / f'report-{run_id}.json', {
                    'kind': 'sight', 'object_id': str(identity), 'place': f'景点{identity}',
                    'reason': 'target_reached', 'run_id': run_id,
                    'review_count_check': {'filter': '全部', 'state': 'compared_display_only',
                                           'display_total': 43000, 'display_count_labels': labels},
                    'sampling_context': {'observations': [{'visible_filters': filters}]},
                })

            plan = build_sight_gap_plan(folder)
            expected = {f'sight-{identity}': target for identity, _, _, target in cases}
            self.assertEqual({row['object_ref']: row['target'] for row in plan['items']}, expected)
            audit = write_coverage_audit(folder, persist=False)
            self.assertEqual({row['object_key']: row['phone_target'] for row in audit['objects']}, expected)
