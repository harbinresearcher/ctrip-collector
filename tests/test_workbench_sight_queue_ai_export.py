import http.client
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

from mobile_sight_queue import create_sight_queue, save_sight_queue
from workbench_server import create_server


def queue_item(identity):
    return {
        'object_ref': f'sight-{identity}',
        'object_id': str(identity),
        'name': f'景点{identity}',
        'address': f'某县某街{identity}号',
        'identity_evidence': f'sight-{identity}-object.json',
    }


def report_for(queue, item, **changes):
    return {
        'queue_id': queue['queue_id'], 'object_id': item['object_id'], 'name': item['name'], 'kind': 'sight',
        'source_method': 'android_sight_ai_snapshot', 'generated_by_ai': True, 'summary_state': 'expanded',
        'sections': [{'topic': '景色', 'body': 'AI 总结正文', 'internal_file': 'C:/private/section.bin'}],
        'review_display': {'count_text': '123条点评', 'private_file': 'C:/private/description.json'},
        'rating_dimensions': [{'label': '景色', 'score_text': '4.9', 'raw': 'C:/private/scores.json'}],
        'ai_selected_images': {'display_count': 140, 'visible_labels': ['夏日'], 'file': 'C:/private/images.zip'},
        'file': 'C:/private/sight-ai-original.json', 'complete': False, **changes,
    }


def write_summary(folder, queue, item, report_id, *, checkpoint=None, **changes):
    name = f'sight-ai-{report_id}.json'
    item['ai_report_id'] = report_id
    item['ai_summary'] = {
        'status': 'saved', 'report_file': name, 'section_count': 1, 'summary_state': 'expanded',
        **(checkpoint or {}),
    }
    path = Path(folder) / name
    path.write_text(json.dumps(report_for(queue, item, **changes), ensure_ascii=False), encoding='utf-8')
    return path


def ai_queue(folder, *identities):
    queue = create_sight_queue(folder, [queue_item(identity) for identity in identities], 4, 2)
    queue['save_ai_summary'] = True
    return queue


@contextmanager
def local_http(folder):
    with create_server(folder, port=0) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        try:
            yield connection
        finally:
            connection.close()
            server.shutdown()
            thread.join(timeout=3)


class SightQueueAIExportTests(unittest.TestCase):
    def test_attachment_preserves_queue_order_and_labels_unavailable_reports(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = ai_queue(folder, 4, 9, 2, 6, 13, 14, 15)
            expanded, pending, preview, foreign, corrupt, mismatched, unseen = queue['items']
            write_summary(folder, queue, expanded, 'a' * 32)
            write_summary(folder, queue, pending, 'b' * 32).unlink()
            write_summary(folder, queue, preview, 'c' * 32, checkpoint={'summary_state': 'preview'},
                          summary_state='preview')
            write_summary(folder, queue, foreign, 'd' * 32, queue_id='f' * 32)
            write_summary(folder, queue, corrupt, 'e' * 32, sections=[{'topic': '无正文'}])
            write_summary(folder, queue, mismatched, 'f' * 32, checkpoint={'section_count': 2})
            unseen['ai_summary'] = {'status': 'not_visible'}
            save_sight_queue(folder, queue)

            with local_http(folder) as connection:
                connection.request('GET', f"/api/sight-queue-ai?queue={queue['queue_id']}")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader('Content-Type'), 'application/json; charset=utf-8')
                self.assertEqual(response.getheader('Content-Disposition'),
                                 f'attachment; filename="sight-queue-ai-{queue["queue_id"]}.json"')
                self.assertEqual(response.getheader('Cache-Control'), 'no-store')
                raw = response.read()
                self.assertEqual(int(response.getheader('Content-Length')), len(raw))
                result = json.loads(raw)

            self.assertNotIn('C:/private', raw.decode('utf-8'))
            self.assertFalse(result['complete'])
            self.assertEqual(result['scope'], 'saved_sight_ai_summaries')
            self.assertEqual(result['queue_id'], queue['queue_id'])
            self.assertEqual((result['requested_count'], result['exported_count'], result['omitted_count']), (7, 2, 5))
            self.assertEqual([item['object_ref'] for item in result['items']],
                             ['sight-4', 'sight-9', 'sight-2', 'sight-6', 'sight-13', 'sight-14', 'sight-15'])
            self.assertEqual([item['status'] for item in result['items']],
                             ['saved', 'unavailable', 'saved', 'unavailable', 'unavailable', 'unavailable', 'not_visible'])
            self.assertEqual(result['items'][0]['sections'], [{'topic': '景色', 'body': 'AI 总结正文'}])
            self.assertEqual(result['items'][0]['review_display'], {'count_text': '123条点评'})
            self.assertEqual(result['items'][0]['rating_dimensions'], [{'label': '景色', 'score_text': '4.9'}])
            self.assertEqual(result['items'][0]['ai_selected_images'], {'display_count': 140, 'visible_labels': ['夏日']})
            self.assertEqual(result['items'][2]['summary_state'], 'preview')
            self.assertTrue(all('sections' not in item for item in result['items'] if item['status'] != 'saved'))

    def test_queue_query_rejects_cross_origin_invalid_and_repeated_identifiers(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = ai_queue(folder, 1)
            write_summary(folder, queue, queue['items'][0], 'a' * 32)
            save_sight_queue(folder, queue)
            endpoint = '/api/sight-queue-ai'
            queue_id = queue['queue_id']
            with local_http(folder) as connection:
                for query, headers, status in (
                    (f'queue={queue_id}', {'Origin': 'https://other.example'}, 403),
                    ('queue=..%2Fother', {}, 404),
                    ('queue=', {}, 404),
                    (f'queue={queue_id}&queue={queue_id}', {}, 404),
                    (f'queue={queue_id}&file=sight-ai-{"a" * 32}.json', {}, 404),
                    (f'queue={"1" * 32}', {}, 404),
                ):
                    with self.subTest(query=query):
                        connection.request('GET', f'{endpoint}?{query}', headers=headers)
                        response = connection.getresponse()
                        self.assertEqual(response.status, status)
                        self.assertIsNone(response.getheader('Content-Disposition'))
                        self.assertIn('error', json.load(response))

    def test_no_valid_artifact_returns_not_found(self):
        with tempfile.TemporaryDirectory() as folder:
            queue = ai_queue(folder, 4, 9)
            write_summary(folder, queue, queue['items'][0], 'a' * 32, object_id='9')
            queue['items'][1]['ai_summary'] = {'status': 'error', 'error': 'C:/private/diagnostics.txt'}
            save_sight_queue(folder, queue)
            with local_http(folder) as connection:
                connection.request('GET', f"/api/sight-queue-ai?queue={queue['queue_id']}")
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                self.assertIsNone(response.getheader('Content-Disposition'))
                self.assertNotIn('C:/private', response.read().decode('utf-8'))

    def test_report_validator_can_recover_before_summary_is_checkpointed(self):
        from sight_queue_ai_export import read_sight_queue_ai_report

        with tempfile.TemporaryDirectory() as folder:
            queue = ai_queue(folder, 4)
            item = queue['items'][0]
            path = write_summary(folder, queue, item, 'a' * 32)
            item['ai_summary'] = None
            report = read_sight_queue_ai_report(folder, queue['queue_id'], item, path.name)
            self.assertEqual((report['object_id'], report['queue_id'], report['section_count']),
                             ('4', queue['queue_id'], 1))
            for changes in (
                {'queue_id': 'd' * 32}, {'object_id': 4}, {'name': '另一个景点'}, {'kind': 'hotel'},
                {'source_method': 'web'}, {'generated_by_ai': False}, {'summary_state': 'all'},
                {'section_count': True}, {'section_count': 2}, {'sections': []},
                {'sections': [{'topic': '景色', 'body': ''}]},
                {'sections': [{'topic': '景色', 'body': '\ud800'}]},
            ):
                with self.subTest(changes=repr(changes)):
                    path.write_text(json.dumps(report_for(queue, item, **changes)), encoding='utf-8')
                    with self.assertRaises(ValueError):
                        read_sight_queue_ai_report(folder, queue['queue_id'], item, path.name)
            with self.assertRaises(ValueError):
                read_sight_queue_ai_report(folder, queue['queue_id'], item, '../' + path.name)
            with self.assertRaises(ValueError):
                read_sight_queue_ai_report(folder, queue['queue_id'], item, f'sight-ai-{"b" * 32}.json')

    def test_report_in_output_cannot_be_a_symlink(self):
        from sight_queue_ai_export import read_sight_queue_ai_report

        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as external:
            queue = ai_queue(folder, 4)
            item = queue['items'][0]
            report_id = 'a' * 32
            item['ai_report_id'] = report_id
            path = Path(folder) / f'sight-ai-{report_id}.json'
            source = Path(external) / path.name
            source.write_text(json.dumps(report_for(queue, item)), encoding='utf-8')
            try:
                path.symlink_to(source)
            except OSError as exc:
                self.skipTest(str(exc))
            with self.assertRaises((ValueError, OSError)):
                read_sight_queue_ai_report(folder, queue['queue_id'], item, path.name)
