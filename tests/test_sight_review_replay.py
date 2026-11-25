import csv
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from xml.etree import ElementTree as ET

from core import Store, parse_reviews
from mobile_scoped_store import MobileScopedStore
from runner import sight_screen_title
from workbench_jobs import make_job


FIXTURES = Path(__file__).parent / 'fixtures'
XML = (FIXTURES / 'ctrip-comments.xml').read_text('utf-8')
NAME = '长白山西景区'
RAW = 'raw/20261018T111713-2a6a4f/00000.xml'


def old_toolbar_xml():
    root = ET.fromstring(XML)
    toolbar = next(node for node in root.iter('node')
                   if any(child.get('text') == NAME for child in node)
                   and any(child.get('content-desc') == '\ue957' for child in node))
    share = next(child for child in toolbar if child.get('content-desc') == '\ue957')
    toolbar.remove(share)
    list(toolbar)[2].set('index', '2')
    return ET.tostring(root, encoding='unicode')


class SightReviewReplayTests(unittest.TestCase):
    def test_verified_legacy_toolbar_variant(self):
        self.assertEqual(sight_screen_title(ET.fromstring(old_toolbar_xml())), NAME)

    def test_replay_both_database_generations_matches_records_and_keeps_database_bytes(self):
        raw = old_toolbar_xml()
        review = parse_reviews(raw, 'sight')[0]
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            evidence = folder / RAW
            evidence.parent.mkdir(parents=True)
            evidence.write_text(raw, encoding='utf-8')
            legacy = Store(folder)
            try:
                legacy.save('sight', NAME, [review], str(Path(RAW)))
                legacy.save('sight', NAME, [{**review, 'content': '不在该页的正文'}], str(Path(RAW)))
                legacy.save('sight', '另一处景点', [review], str(Path(RAW)))
            finally:
                legacy.close()
            scoped = MobileScopedStore(folder, 'sight', '136039')
            try:
                scoped.save(NAME, [review], 'raw/20261018T111713-2a6a4f/99999.xml')
                scoped.save(NAME, [{**review, 'raw_texts': review['raw_texts'] + ['受控差异']}], RAW)
            finally:
                scoped.close()
            database = folder / 'reviews.sqlite3'
            digest = hashlib.sha256(database.read_bytes()).hexdigest()
            result = make_job({'mode': 'sight_review_replay'}, folder)(Event(), lambda _: None)
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), digest)
            self.assertEqual(result['rows_written'], 4)
            self.assertEqual(result['matched_reviews'], 2)
            self.assertEqual(result['evidence_screens_read'], 1)
            self.assertEqual(result['replayed_field_counts']['photo_count_display'], 2)
            with Path(result['csv']).open(encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertCountEqual([row['evidence_status'] for row in rows],
                                  ['matched', 'review_not_matched', 'title_mismatch', 'matched'])
            self.assertEqual([row['photo_count_display'] for row in rows if row['evidence_status'] == 'matched'],
                             ['4', '4'])
            self.assertNotIn('author', rows[0])
            self.assertNotIn('raw_texts', rows[0])
            self.assertFalse(result['complete'])

    def test_rejects_paths_outside_the_known_raw_archive(self):
        with TemporaryDirectory() as tmp:
            review = parse_reviews(XML, 'sight')[0]
            store = Store(tmp)
            try:
                store.save('sight', NAME, [review], 'raw/20261018T111713-2a6a4f/../../other.xml')
            finally:
                store.close()
            result = make_job({'mode': 'sight_review_replay'}, tmp)(Event(), lambda _: None)
            self.assertEqual(result['status_counts'], {'unavailable_evidence': 1})
            self.assertEqual(result['evidence_screens_read'], 0)
