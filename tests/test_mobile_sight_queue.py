import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import patch

from mobile_sight_queue import (create_sight_queue, list_sight_queues, load_sight_queue,
                                lock_sight_queue, save_sight_queue)


def sight(number):
    ref = f'sight-{number}'
    return {'object_ref': ref, 'object_id': str(number), 'name': f'景点 {number}',
            'address': '景点的可核对地址', 'identity_evidence': f'{ref}-object.json'}


class MobileSightQueueTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name).resolve()

    def test_create_record_and_resume_two_objects_without_revealing_address_in_list(self):
        items = [sight(35), sight(2)]
        queue = create_sight_queue(self.output, items, 50, 100)
        queue_id = queue['queue_id']
        self.assertRegex(queue_id, r'^[0-9a-f]{32}$')
        self.assertTrue((self.output / f'sight-review-queue-{queue_id}.json').is_file())
        self.assertEqual(queue['mode'], 'app_sight_queue')
        self.assertIs(queue['complete'], False)
        self.assertEqual(queue['reason'], 'ready')
        self.assertEqual(queue['items'], [dict(item, status='pending', result=None) for item in items])
        self.assertNotIn('status', items[0])
        self.assertEqual(load_sight_queue(self.output, queue_id), queue)

        queue['items'][0]['status'] = 'recorded'
        queue['items'][0]['result'] = {'run_id': '20261126T134152-4456c0', 'count': 22,
                                       'reason': 'screen_budget_reached', 'sample_csv_count': 22}
        queue['items'][1]['status'] = 'retry'
        queue['items'][1]['result'] = {'reason': 'navigation_error', 'error': '景点页面未核对'}
        queue['reason'] = 'stopped_by_user'
        save_sight_queue(self.output, queue)
        self.assertEqual(load_sight_queue(self.output, queue_id), queue)
        self.assertEqual(list_sight_queues(self.output), [{
            'queue_id': queue_id, 'created_at': queue['created_at'], 'updated_at': queue['updated_at'],
            'reason': 'stopped_by_user', 'limit': 50, 'max_swipes': 100, 'item_count': 2, 'recorded_count': 1,
        }])

    def test_rejects_invalid_references_duplicates_and_budgets_before_writing(self):
        invalid = [
            ([], 50, 100), ([sight(n) for n in range(1, 22)], 50, 100),
            ([sight(35), sight(35)], 50, 100),
            ([dict(sight(35), object_ref='hotel-35')], 50, 100),
            ([dict(sight(35), object_id='42')], 50, 100),
            ([dict(sight(35), identity_evidence='..\\sight-35-object.json')], 50, 100),
            ([sight(35)], True, 100), ([sight(35)], 100001, 100), ([sight(35)], 50, 0),
            ([sight(35)], 50, 10001),
        ]
        for items, limit, swipes in invalid:
            with self.subTest(items=items, limit=limit, swipes=swipes), self.assertRaises(ValueError):
                create_sight_queue(self.output, items, limit, swipes)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_filename_and_content_must_agree_and_malformed_files_are_not_listed(self):
        queue = create_sight_queue(self.output, [sight(35)], 50, 100)
        original = next(self.output.glob('*.json'))
        bad_id = '9' * 32
        orphan = original.with_name(original.name.replace(queue['queue_id'], bad_id))
        orphan.write_text(json.dumps(queue), encoding='utf-8')
        with self.assertRaises(ValueError):
            load_sight_queue(self.output, bad_id)
        with self.assertRaises(ValueError):
            load_sight_queue(self.output, '../' + queue['queue_id'])
        corrupt = original.with_name(original.name.replace(queue['queue_id'], '8' * 32))
        corrupt.write_text('{', encoding='utf-8')
        self.assertEqual([item['queue_id'] for item in list_sight_queues(self.output)], [queue['queue_id']])

    def test_list_returns_the_newest_50_valid_canonical_queues(self):
        self.assertEqual(list_sight_queues(self.output / 'future-output'), [])
        template = create_sight_queue(self.output, [sight(35)], 50, 100)
        now = datetime.fromisoformat(template['created_at']) + timedelta(days=1)
        queue_ids = [f'{number:032x}' for number in range(1, 53)]
        for minute, queue_id in reversed(list(enumerate(queue_ids))):
            report = dict(template, queue_id=queue_id, created_at=now.isoformat(),
                          updated_at=(now + timedelta(minutes=minute)).isoformat())
            path = self.output / f'sight-review-queue-{queue_id}.json'
            path.write_text(json.dumps(report), encoding='utf-8')
        stray = self.output / f'sight-review-queue-{"F" * 32}.json'
        stray.write_text(json.dumps(dict(template, queue_id='F' * 32)), encoding='utf-8')
        self.assertEqual([row['queue_id'] for row in list_sight_queues(self.output)],
                         list(reversed(queue_ids[2:])))

    def test_save_error_preserves_committed_state_and_removes_temporary_file(self):
        queue = create_sight_queue(self.output, [sight(35)], 50, 100)
        old = load_sight_queue(self.output, queue['queue_id'])
        queue['reason'] = 'running'
        queue['items'][0]['status'] = 'running'
        with patch.object(Path, 'replace', side_effect=OSError('模拟原子替换故障')), self.assertRaises(OSError):
            save_sight_queue(self.output, queue)
        self.assertEqual(load_sight_queue(self.output, queue['queue_id']), old)
        self.assertEqual(len(list(self.output.iterdir())), 1)

    def test_save_retries_a_transient_windows_replace_conflict(self):
        queue = create_sight_queue(self.output, [sight(35)], 50, 100)
        queue['reason'] = 'running'
        blocked = PermissionError('目标报告暂时被占用')
        blocked.winerror = 5
        replace = Path.replace
        calls = []

        def briefly_blocked(temporary, path):
            calls.append(path)
            if len(calls) == 1:
                raise blocked
            return replace(temporary, path)

        with patch.object(Path, 'replace', briefly_blocked):
            save_sight_queue(self.output, queue)
        self.assertEqual(len(calls), 2)
        self.assertEqual(load_sight_queue(self.output, queue['queue_id']), queue)
        self.assertEqual(len(list(self.output.iterdir())), 1)

    def test_locks_exclude_a_second_process_and_allow_reacquiring_after_release(self):
        queue_id = create_sight_queue(self.output, [sight(35)], 50, 100)['queue_id']
        probe = (
            'from mobile_sight_queue import lock_sight_queue; import sys\n'
            'try:\n'
            '    with lock_sight_queue(sys.argv[1], sys.argv[2]):\n'
            '        pass\n'
            'except RuntimeError:\n'
            '    sys.exit(47)\n'
        )
        command = [sys.executable, '-c', probe, str(self.output), queue_id]
        with lock_sight_queue(self.output, queue_id):
            other = subprocess.run(command, capture_output=True, timeout=10)
            self.assertEqual(other.returncode, 47, other.stderr)
        other = subprocess.run(command, capture_output=True, timeout=10)
        self.assertEqual(other.returncode, 0, other.stderr)

    def test_abrupt_process_exit_releases_queue_lock(self):
        queue_id = create_sight_queue(self.output, [sight(35)], 50, 100)['queue_id']
        ready = self.output / 'worker-ready'
        probe = (
            'from mobile_sight_queue import lock_sight_queue\n'
            'from pathlib import Path\n'
            'from threading import Event\n'
            'import sys\n'
            'with lock_sight_queue(sys.argv[1], sys.argv[2]):\n'
            '    Path(sys.argv[3]).touch()\n'
            '    Event().wait(10)\n'
        )
        command = [sys.executable, '-c', probe, str(self.output), queue_id, str(ready)]
        worker = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        stop_at = monotonic() + 5
        try:
            while not ready.exists() and worker.poll() is None and monotonic() < stop_at:
                sleep(.02)
            self.assertTrue(ready.exists())
            with self.assertRaises(RuntimeError):
                with lock_sight_queue(self.output, queue_id):
                    self.fail('队列在另一个进程持锁时不应重复打开')
        finally:
            worker.terminate()
            worker.communicate(timeout=5)
        with lock_sight_queue(self.output, queue_id):
            self.assertEqual(load_sight_queue(self.output, queue_id)['reason'], 'ready')

    def test_symlink_queue_is_not_loaded_listed_or_overwritten(self):
        queue = create_sight_queue(self.output, [sight(35)], 50, 100)
        stored = next(self.output.glob('*.json'))
        actual = self.output / 'queue-baseline.txt'
        stored.rename(actual)
        try:
            stored.symlink_to(actual)
        except OSError as exc:
            self.skipTest(f'系统未授权创建符号链接 {exc.winerror if hasattr(exc, "winerror") else exc.errno}')
        before = actual.read_bytes()
        self.assertEqual(list_sight_queues(self.output), [])
        with self.assertRaises((OSError, ValueError)):
            load_sight_queue(self.output, queue['queue_id'])
        with self.assertRaises((OSError, ValueError)):
            save_sight_queue(self.output, queue)
        self.assertEqual(actual.read_bytes(), before)
