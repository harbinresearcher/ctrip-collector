import http.client
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock, patch

from core import parse_reviews
from mobile_review_recovery import open_saved_review_sample
from mobile_scoped_store import MobileScopedStore
from mobile_sight_queue import create_sight_queue, load_sight_queue, save_sight_queue
from runner import collect as collect_reviews
from workbench_jobs import make_job
from workbench_server import create_server


def write_sight(folder, identity):
    ref = f'sight-{identity}'
    (Path(folder) / f'{ref}-object.json').write_text(json.dumps({
        'kind': 'sight', 'resource_id': identity, 'name': f'景点{identity}',
        'details': {'address': f'江苏省南京市{identity}号'},
    }), encoding='utf-8')
    return ref


def review_result(identity, reason='target_reached'):
    return {'reason': reason, 'run_id': f'20261126T140220-{identity:06x}',
            'count': identity, 'new_count': 1, 'screens': 2, 'sample_csv_count': 1}


def write_ai_report(folder, name, object_id, queue_id, report_id):
    filename = f'sight-ai-{report_id}.json'
    (Path(folder) / filename).write_text(json.dumps({
        'kind': 'sight', 'name': name, 'object_id': object_id, 'queue_id': queue_id,
        'source': 'ctrip_android_ui', 'source_method': 'android_sight_ai_snapshot',
        'generated_by_ai': True, 'summary_state': 'expanded', 'complete': False,
        'sections': [{'topic': '景观', 'body': '模拟验收用总结'}],
    }), encoding='utf-8')
    return filename


def interrupted_queue(folder, *, final_reason=None, report_identity='4'):
    identity, name = '4', '景点4'
    ref = write_sight(folder, identity)
    queue = create_sight_queue(folder, [{'object_ref': ref, 'object_id': identity, 'name': name,
                                          'address': '江苏省南京市4号', 'identity_evidence': f'{ref}-object.json'}], 1, 4)
    run_id = '20261126T140220-000004'
    queue['items'][0].update(status='running', attempt_run_id=run_id)
    save_sight_queue(folder, queue)
    store = MobileScopedStore(folder, 'sight', identity)
    try:
        fixture = Path(__file__).parent / 'fixtures' / 'ctrip-comments2.xml'
        rows = parse_reviews(fixture.read_text('utf-8'), 'sight')
        store.start_run(run_id, name)
        store.save(name, rows, f'raw/{run_id}/00000.xml', observed_keys={}, run_id=run_id)
        if final_reason is not None:
            store.finish_run(run_id, final_reason)
            report = {**review_result(4, final_reason), 'kind': 'sight', 'place': name, 'object_id': report_identity,
                      'count': len(rows), 'new_count': len(rows), 'sample_csv_count': len(rows), 'target': queue['limit']}
            (Path(folder) / f'report-{run_id}.json').write_text(json.dumps(report), encoding='utf-8')
    finally:
        store.close()
    return queue, run_id, len(rows)


class SightQueueWorkbenchTests(unittest.TestCase):
    def test_sight_queue_keeps_ai_and_reviews_separate(self):
        from mobile_sight_queue import load_sight_queue

        def ai_capture(device, output, name, object_id, queue_id, stop, *, report_id):
            state = load_sight_queue(output, queue_id)
            item = next(entry for entry in state['items'] if entry['object_id'] == object_id)
            self.assertEqual((item['status'], item['ai_report_id']), ('running', report_id))
            if object_id == '2':
                return {'status': 'not_visible'}
            filename = write_ai_report(output, name, object_id, queue_id, report_id)
            return {'status': 'saved', 'report_file': filename, 'section_count': 1, 'summary_state': 'expanded'}

        with tempfile.TemporaryDirectory() as folder, patch('app_sight_queue_jobs.Adb'), \
                patch('app_sight_queue_jobs.open_sight_reviews', return_value='opened'), \
                patch('app_sight_queue_jobs.return_from_sight_reviews', return_value='search'), \
                patch('app_sight_queue_jobs.capture_queue_sight_ai', side_effect=ai_capture), \
                patch('app_sight_queue_jobs.collect', side_effect=[review_result(1), review_result(2)]) as collect:
            refs = [write_sight(folder, number) for number in (1, 2)]
            job = make_job({'mode': 'app_sight_queue', 'object_refs': refs, 'save_ai_summary': True}, folder, 'adb.exe')
            result = job(Event(), Mock())
            self.assertTrue(result['save_ai_summary'])
            self.assertEqual([item['ai_summary']['status'] for item in result['items']], ['saved', 'not_visible'])
            self.assertEqual(result['items'][0]['ai_summary']['section_count'], 1)
            self.assertEqual(result['reason'], 'queue_recorded')
            self.assertFalse(result['complete'])
            self.assertEqual(collect.call_count, 2)
            saved = load_sight_queue(folder, result['queue_id'])
            self.assertEqual(saved['items'][0]['ai_summary'], result['items'][0]['ai_summary'])

    def test_sight_queue_reuses_summary_after_unfinished_review_attempt(self):
        from mobile_sight_queue import create_sight_queue, save_sight_queue

        with tempfile.TemporaryDirectory() as folder, patch('app_sight_queue_jobs.Adb'), \
                patch('app_sight_queue_jobs.open_sight_reviews', return_value='opened'), \
                patch('app_sight_queue_jobs.capture_queue_sight_ai') as capture, \
                patch('app_sight_queue_jobs.collect', return_value=review_result(1)):
            write_sight(folder, 1)
            queue = create_sight_queue(folder, [{'object_ref': 'sight-1', 'object_id': '1', 'name': '景点1',
                                                'address': '江苏省南京市1号',
                                                'identity_evidence': 'sight-1-object.json'}],
                                       1, 3, save_ai_summary=True)
            queue['items'][0].update(status='running', ai_report_id='a' * 32)
            save_sight_queue(folder, queue)
            path = write_ai_report(folder, '景点1', '1', queue['queue_id'], 'a' * 32)
            job = make_job({'mode': 'app_sight_queue', 'queue_id': queue['queue_id']}, folder, 'adb.exe')
            result = job(Event(), Mock())
            capture.assert_not_called()
            self.assertEqual(result['items'][0]['ai_summary']['report_file'], path)
            self.assertEqual(result['items'][0]['status'], 'recorded')

    def test_sight_queue_preserves_summary_if_restoring_reviews_fails(self):
        def ai_capture(device, output, name, object_id, queue_id, stop, *, report_id):
            write_ai_report(output, name, object_id, queue_id, report_id)
            raise RuntimeError('点评页恢复失败')

        with tempfile.TemporaryDirectory() as folder, patch('app_sight_queue_jobs.Adb'), \
                patch('app_sight_queue_jobs.open_sight_reviews', return_value='opened'), \
                patch('app_sight_queue_jobs.capture_queue_sight_ai', side_effect=ai_capture), \
                patch('app_sight_queue_jobs.collect') as collect:
            ref = write_sight(folder, 1)
            request = {'mode': 'app_sight_queue', 'object_refs': [ref], 'save_ai_summary': True}
            result = make_job(request, folder, 'adb.exe')(Event(), Mock())
            self.assertEqual(result['reason'], 'needs_attention')
            self.assertEqual(result['items'][0]['ai_summary']['status'], 'saved')
            self.assertEqual(result['items'][0]['result']['error'], '点评页恢复失败')
            collect.assert_not_called()
    def test_new_queue_reads_checkpoint_under_lock(self):
        from mobile_sight_queue import load_sight_queue, save_sight_queue

        @contextmanager
        def another_worker_finishes_before_lock(folder, queue_id):
            state = load_sight_queue(folder, queue_id)
            state['items'][0].update(status='recorded', result=review_result(1))
            state['reason'] = 'queue_recorded'
            save_sight_queue(folder, state)
            yield

        with tempfile.TemporaryDirectory() as folder, patch('app_sight_queue_jobs.Adb') as adb, \
                patch('app_sight_queue_jobs.lock_sight_queue', side_effect=another_worker_finishes_before_lock), \
                patch('app_sight_queue_jobs.open_sight_reviews') as navigate:
            ref = write_sight(folder, 1)
            result = make_job({'mode': 'app_sight_queue', 'object_refs': [ref]}, folder, 'adb.exe')(Event(), Mock())
            self.assertEqual(result['items'][0]['result'], review_result(1))
            adb.return_value.__enter__.return_value.connect.assert_not_called()
            navigate.assert_not_called()

    def test_resume_recovers_finalized_attempt_without_touching_android(self):
        with tempfile.TemporaryDirectory() as folder, patch('app_sight_queue_jobs.Adb') as adb, \
                patch('app_sight_queue_jobs.open_sight_reviews') as navigate, \
                patch('app_sight_queue_jobs.collect') as collect:
            queue, run_id, count = interrupted_queue(folder, final_reason='target_reached')
            result = make_job({'mode': 'app_sight_queue', 'queue_id': queue['queue_id']},
                              folder, 'adb.exe')(Event(), Mock())
            self.assertEqual(result['reason'], 'queue_recorded')
            item, = result['items']
            self.assertEqual((item['status'], item['result']['run_id'], item['result']['sample_csv_count']),
                             ('recorded', run_id, count))
            adb.return_value.__enter__.return_value.connect.assert_not_called()
            navigate.assert_not_called()
            collect.assert_not_called()

    def test_resume_retains_undownloaded_attempt_sample_when_already_at_limit(self):
        for final_reason, report_identity in ((None, '4'), ('target_reached', '5')):
            with self.subTest(final_reason=final_reason), tempfile.TemporaryDirectory() as folder, \
                    patch('app_sight_queue_jobs.Adb') as adb, \
                    patch('app_sight_queue_jobs.open_sight_reviews', return_value='opened'), \
                    patch('app_sight_queue_jobs.collect', wraps=collect_reviews) as collect:
                queue, previous_run, count = interrupted_queue(folder, final_reason=final_reason,
                                                               report_identity=report_identity)
                queued = make_job({'mode': 'app_sight_queue', 'queue_id': queue['queue_id']}, folder, 'adb.exe')
                result = queued(Event(), Mock())
                item, = result['items']
                report = item['result']
                self.assertEqual(item['status'], 'recorded')
                self.assertNotEqual(report['run_id'], previous_run)
                self.assertEqual((report['screens'], report['new_count']), (0, 0))
                self.assertEqual((report['sample_run_id'], report['sample_csv_count']), (previous_run, count))
                with open_saved_review_sample(folder, report['sample_run_id']) as sample:
                    self.assertIn(previous_run, sample.read().decode('utf-8-sig'))
                adb.return_value.__enter__.return_value.snapshot.assert_not_called()
                collect.assert_called_once()

    def test_resume_retries_if_saved_attempt_membership_no_longer_exists(self):
        with tempfile.TemporaryDirectory() as folder, patch('app_sight_queue_jobs.Adb') as adb, \
                patch('app_sight_queue_jobs.open_sight_reviews', return_value='opened'), \
                patch('app_sight_queue_jobs.collect', wraps=collect_reviews) as collect:
            queue, previous_run, _ = interrupted_queue(folder, final_reason='target_reached')
            store = MobileScopedStore(folder, 'sight', '4')
            try:
                with store.db:
                    store.db.execute('DELETE FROM reviews_scoped WHERE scope=?', (store.scope,))
            finally:
                store.close()
            fixture = Path(__file__).parent / 'fixtures' / 'ctrip-comments2.xml'
            snapshot = adb.return_value.__enter__.return_value.snapshot
            snapshot.return_value = fixture.read_text('utf-8').replace('text="长白山西景区"', 'text="景点4"', 1)
            queued = make_job({'mode': 'app_sight_queue', 'queue_id': queue['queue_id']}, folder, 'adb.exe')
            result = queued(Event(), Mock())
            item, = result['items']
            self.assertEqual(item['status'], 'recorded')
            self.assertNotEqual(item['result']['run_id'], previous_run)
            self.assertNotIn('sample_run_id', item['result'])
            self.assertEqual((item['result']['screens'], item['result']['sample_csv_count']), (1, 1))
            collect.assert_called_once()

    def test_records_limited_coverage_then_moves_to_the_next_object(self):
        with tempfile.TemporaryDirectory() as folder, patch('app_sight_queue_jobs.Adb') as adb, \
                patch('app_sight_queue_jobs.open_sight_reviews', return_value='opened') as navigate, \
                patch('app_sight_queue_jobs.return_from_sight_reviews') as leave, \
                patch('app_sight_queue_jobs.collect') as collect:
            refs = [write_sight(folder, item) for item in (4, 7)]
            job = make_job({'mode': 'app_sight_queue', 'object_refs': refs, 'limit': 10, 'max_swipes': 4},
                           folder, 'adb.exe', adb_serial='emulator-5556')
            stop, emit = Event(), Mock()
            collect.side_effect = [review_result(4, 'screen_budget_reached'), review_result(7)]
            state = job(stop, emit)
            self.assertEqual(state['reason'], 'queue_recorded')
            self.assertFalse(state['complete'])
            self.assertEqual([item['status'] for item in state['items']], ['recorded'] * 2)
            self.assertEqual([item['result']['reason'] for item in state['items']],
                             ['screen_budget_reached', 'target_reached'])
            device = adb.return_value.__enter__.return_value
            adb.assert_called_once_with('adb.exe', serial='emulator-5556', ui_backend='uiautomator2')
            device.connect.assert_called_once()
            leave.assert_called_once_with(device, '景点4', stop, next_name='景点7')
            self.assertEqual([call.args[1] for call in navigate.call_args_list], ['景点4', '景点7'])
            self.assertTrue(all(call.kwargs['require_address_search'] is True for call in navigate.call_args_list))
            self.assertEqual(collect.call_args.kwargs['identity_evidence'], 'sight-7-object.json')
            self.assertTrue((Path(folder) / f"sight-review-queue-{state['queue_id']}.json").is_file())

    def test_resume_does_not_repeat_recorded_object_or_discard_failures(self):
        with tempfile.TemporaryDirectory() as folder, patch('app_sight_queue_jobs.Adb') as adb, \
                patch('app_sight_queue_jobs.return_from_sight_reviews') as leave, \
                patch('app_sight_queue_jobs.open_sight_reviews') as navigate, \
                patch('app_sight_queue_jobs.collect') as collect:
            refs = [write_sight(folder, item) for item in (1, 2, 9)]
            request = {'mode': 'app_sight_queue', 'object_refs': refs, 'limit': 5, 'max_swipes': 3}
            navigate.side_effect = ['opened', ValueError('请核对景点地址')]
            collect.side_effect = [review_result(1, 'stalled_unverified')]
            first = make_job(request, folder, 'adb.exe')(Event(), Mock())
            self.assertEqual(first['reason'], 'needs_attention')
            self.assertEqual([item['status'] for item in first['items']], ['recorded', 'retry', 'pending'])
            self.assertEqual(first['items'][1]['result']['error'], '请核对景点地址')
            stop = Event()
            leave.reset_mock()
            navigate.side_effect = ['opened', 'opened']
            collect.side_effect = [review_result(2), review_result(9)]
            second = make_job({'mode': 'app_sight_queue', 'queue_id': first['queue_id']},
                              folder, 'adb.exe')(stop, Mock())
            self.assertEqual(second['reason'], 'queue_recorded')
            self.assertEqual(second['items'][0]['result'], first['items'][0]['result'])
            self.assertEqual([call.kwargs['next_name'] for call in leave.call_args_list], ['景点2', '景点9'])
            self.assertEqual([item['result']['count'] for item in second['items']], [1, 2, 9])
            self.assertEqual(adb.call_count, 2)

    def test_cancel_between_objects_records_the_first_and_does_not_navigate_again(self):
        with tempfile.TemporaryDirectory() as folder, patch('app_sight_queue_jobs.Adb'), \
                patch('app_sight_queue_jobs.return_from_sight_reviews') as leave, \
                patch('app_sight_queue_jobs.open_sight_reviews', return_value='opened'), \
                patch('app_sight_queue_jobs.collect') as collect:
            refs = [write_sight(folder, item) for item in (1, 2)]
            stop = Event()

            def finish_first(*args, **kwargs):
                checkpoint, = Path(folder).glob('sight-review-queue-*.json')
                identifier = json.loads(checkpoint.read_text('utf-8'))['queue_id']
                running = load_sight_queue(folder, identifier)['items'][0]
                self.assertEqual(running['status'], 'running')
                self.assertEqual(running['attempt_run_id'], kwargs['run_id'])
                stop.set()
                return review_result(1)

            collect.side_effect = finish_first
            state = make_job({'mode': 'app_sight_queue', 'object_refs': refs, 'limit': 1, 'max_swipes': 3},
                             folder, 'adb.exe')(stop, Mock())
            self.assertEqual(state['reason'], 'stopped_by_user')
            self.assertEqual([item['status'] for item in state['items']], ['recorded', 'pending'])
            leave.assert_not_called()

    def test_invalid_inputs_raise_before_accessing_device(self):
        with tempfile.TemporaryDirectory() as folder, patch('app_sight_queue_jobs.Adb') as adb:
            ref = write_sight(folder, 1)
            for fields in (
                {'object_refs': [ref, ref]}, {'object_refs': ['hotel-1']}, {'object_refs': [ref], 'limit': True},
                {'object_refs': [ref], 'max_swipes': 10001}, {'queue_id': '../x'},
                {'queue_id': 'f' * 32, 'object_refs': [ref]},
            ):
                with self.subTest(fields=fields), self.assertRaises(ValueError):
                    make_job({'mode': 'app_sight_queue', **fields}, folder, 'adb.exe')
            adb.assert_not_called()

    def test_saved_queue_listing_is_local_and_reports_checkpoint_counts(self):
        with tempfile.TemporaryDirectory() as folder, patch('app_sight_queue_jobs.Adb'), \
                patch('app_sight_queue_jobs.open_sight_reviews', return_value='opened'), \
                patch('app_sight_queue_jobs.collect', return_value=review_result(1)):
            ref = write_sight(folder, 1)
            state = make_job({'mode': 'app_sight_queue', 'object_refs': [ref], 'limit': 1, 'max_swipes': 3},
                             folder, 'adb.exe')(Event(), Mock())
            with create_server(folder, port=0) as server:
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                try:
                    connection.request('GET', '/api/sight-review-queues')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    records = json.load(response)['queues']
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0]['queue_id'], state['queue_id'])
                    self.assertEqual(records[0]['recorded_count'], 1)
                    connection.request('GET', '/api/sight-review-queues', headers={'Origin': 'https://other.example'})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 403)
                    response.read()
                finally:
                    connection.close()
                    server.shutdown()
                    thread.join(timeout=3)
