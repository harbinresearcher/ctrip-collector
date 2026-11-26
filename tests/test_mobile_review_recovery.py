import csv
import io
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core import parse_reviews
from mobile_review_recovery import open_saved_review_sample, saved_review_runs
from mobile_scoped_store import MobileScopedStore
from workbench_preview import read_scoped_mobile_preview


FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'hotel-comments.xml'


class MobileReviewRecoveryTests(unittest.TestCase):
    def test_killed_run_recovers_revisited_rows_in_original_order_without_older_rows(self):
        xml = FIXTURE.read_text('utf-8')
        review = parse_reviews(xml, 'hotel')[0]
        older = {**review, 'content': '历史未重读'}
        with TemporaryDirectory() as folder:
            store = MobileScopedStore(folder, 'hotel', '123')
            try:
                store.save('样例', [older, review], 'old.xml')
            finally:
                store.close()
            child = """
import sys
from pathlib import Path
from threading import Event
from runner import collect
class Device:
    def ensure_foreground(self): pass
    def snapshot(self): return Path(sys.argv[2]).read_text('utf-8')
    def run(self, *args):
        print('after_commit', flush=True)
        Event().wait(15)
collect(Device(), 'hotel', '样例', sys.argv[1], limit=2, max_swipes=1, object_id='123', refresh=True)
"""
            with subprocess.Popen([sys.executable, '-c', child, folder, str(FIXTURE)],
                                  stdout=subprocess.PIPE, text=True) as process:
                try:
                    self.assertEqual(process.stdout.readline().strip(), 'after_commit')
                finally:
                    process.kill()
                    process.wait(timeout=5)

            self.assertEqual(list(Path(folder).glob('*.csv')), [])
            runs = saved_review_runs(folder)
            self.assertEqual(len(runs), 1)
            run = runs[0]
            self.assertEqual((run['status'], run['sample_count']), ('unfinalized', 1))
            with open_saved_review_sample(folder, run['run_id']) as file:
                rows = list(csv.DictReader(io.StringIO(file.read().decode('utf-8-sig'))))
            self.assertEqual([row['content'] for row in rows], [review['content']])
            self.assertEqual(rows[0]['run_order'], '1')
            self.assertTrue(rows[0]['run_first_seen_source'].replace('\\', '/').endswith('/00000.xml'))
            with self.assertRaises(LookupError):
                open_saved_review_sample(folder, '20260101T000000-abcdef')

    def test_restaurant_media_count_survives_clipping_and_sample_recovery(self):
        review = {'author': '匿名', 'date': '2025-06-08', 'date_kind': 'review_date', 'rating': '5.0',
                  'content': '餐厅点评正文', 'has_expand': False, 'raw_texts': ['餐厅点评正文']}
        first = {**review, 'card_fields': {'media_tiles_visible': 2, 'photo_count_display': 15}}
        clipped = {**review, 'card_fields': {'media_tiles_visible': 1}}
        latest_badge = {**review, 'card_fields': {'photo_count_display': 11}}
        other = {**review, 'content': '未看到图片的点评', 'raw_texts': ['未看到图片的点评', '99图']}
        run_id = '20260101T000000-abcdef'

        with TemporaryDirectory() as folder:
            store = MobileScopedStore(folder, 'restaurant', '123')
            try:
                observed = {}
                store.start_run(run_id, '样例餐厅')
                store.save('样例餐厅', [first, other], 'first.xml', observed_keys=observed, run_id=run_id)
                store.save('样例餐厅', [latest_badge], 'badge.xml', observed_keys=observed, run_id=run_id)
                store.save('样例餐厅', [clipped, review], 'later.xml', observed_keys=observed, run_id=run_id)
                self.assertEqual(store.db.execute('SELECT visible_tiles,source FROM reviews_scoped_media').fetchall(),
                                 [(2, 'first.xml')])
                for path in (store.export(), store.export(run_id=run_id, observed_keys=observed)):
                    with path.open(encoding='utf-8-sig', newline='') as stream:
                        rows = list(csv.DictReader(stream))
                        self.assertEqual([row['media_tiles_visible'] for row in rows], ['2', ''])
                        self.assertEqual([row['photo_count_display'] for row in rows], ['11', ''])
                self.assertEqual(store.db.execute('SELECT field,value,source FROM reviews_scoped_sight_counts').fetchall(),
                                 [('photo_count_display', 11, 'badge.xml')])
            finally:
                store.close()

            self.assertEqual(saved_review_runs(folder)[0]['sample_count'], 2)
            with open_saved_review_sample(folder, run_id) as file:
                rows = list(csv.DictReader(io.StringIO(file.read().decode('utf-8-sig'))))
            self.assertEqual([row['media_tiles_visible'] for row in rows], ['2', ''])
            self.assertEqual([row['photo_count_display'] for row in rows], ['11', ''])
            self.assertEqual([row['run_first_seen_source'] for row in rows], ['first.xml', 'first.xml'])
            preview = read_scoped_mobile_preview(folder)['items']
            fields = next(row for row in preview if row['content'] == review['content'])['card_fields']
            self.assertEqual(fields, {'media_tiles_visible': 2, 'photo_count_display': 11})

    def test_recovers_restaurant_run_from_before_media_metadata_table(self):
        review = {'author': '匿名', 'date': '2025-06-08', 'date_kind': 'review_date', 'rating': '5.0',
                  'content': '旧餐厅点评', 'has_expand': False, 'raw_texts': ['旧餐厅点评']}
        run_id = '20260101T000000-abcdef'
        with TemporaryDirectory() as folder:
            store = MobileScopedStore(folder, 'restaurant', '123')
            try:
                store.start_run(run_id, '样例餐厅')
                store.save('样例餐厅', [review], 'old.xml', observed_keys={}, run_id=run_id)
                store.db.execute('DROP TABLE reviews_scoped_media')
                store.db.execute('DROP TABLE IF EXISTS reviews_scoped_sight_counts')
            finally:
                store.close()
            with open_saved_review_sample(folder, run_id) as file:
                row = next(csv.DictReader(io.StringIO(file.read().decode('utf-8-sig'))))
            self.assertEqual(row['content'], review['content'])
            self.assertEqual(row['media_tiles_visible'], '')
            self.assertEqual(row['photo_count_display'], '')
