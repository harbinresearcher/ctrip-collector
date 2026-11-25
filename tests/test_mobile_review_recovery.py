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
