"""Reparse stored Android sight review evidence without modifying the review database."""
import csv
import json
import re
import uuid
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree as ET

from core import parse_reviews
from mobile_review_fields import SIGHT_COUNT_FIELDS
from mobile_review_recovery import review_database
from mobile_scoped_store import MOBILE_REVIEW_RUN_ID
from runner import sight_screen_title
from workbench_result_files import open_output_file


SOURCE = 'android_sight_review_evidence_replay'
EVIDENCE = re.compile(r'raw/(' + MOBILE_REVIEW_RUN_ID.pattern + r')/([0-9]{5,6}\.xml)')
MAX_BYTES = 8 * 1024 * 1024
FIELDS = ('travel_type', 'visit_duration', 'verified_purchase_visible', *SIGHT_COUNT_FIELDS)
COLUMNS = ('record_source', 'object_id', 'place', 'review_key', 'date', 'rating', 'content',
           *FIELDS, 'evidence_status', 'evidence_sources')


def read_screen(folder, source, opened):
    match = EVIDENCE.fullmatch(source)
    if not match:
        return 'unavailable_evidence', None, {}
    raw = folder / 'raw'
    run = raw / match[1]
    if not run.is_dir() or run.resolve() != run or raw.is_junction() or run.is_junction():
        return 'unavailable_evidence', None, {}
    try:
        with open_output_file(run, run / match[2]) as stream:
            evidence = stream.read(MAX_BYTES + 1)
    except (OSError, ValueError):
        return 'unavailable_evidence', None, {}
    opened.add(source)
    if len(evidence) > MAX_BYTES:
        return 'invalid_evidence', None, {}
    try:
        xml = evidence.decode('utf-8-sig')
        title = sight_screen_title(ET.fromstring(xml))
        if not title or '…' in title or '...' in title:
            return 'title_mismatch', None, {}
        matches = defaultdict(list)
        for row in parse_reviews(xml, 'sight'):
            matches[row['author'], row['date'], row['content']].append(row['card_fields'])
        return 'matched', title, {key: matches[key][0] for key in matches if len(matches[key]) == 1}
    except (ValueError, ET.ParseError):
        return 'invalid_evidence', None, {}


def reconcile_row(row, sources, screen, stop):
    _, place, _, author, date, _, content, _ = row
    fields, matched_sources = {}, []
    errors = set()
    for source in dict.fromkeys(path.replace('\\', '/') for path in sources if isinstance(path, str)):
        if stop.is_set():
            break
        status, title, matches = screen(source)
        if status != 'matched':
            errors.add(status)
            continue
        if title != place:
            errors.add('title_mismatch')
            continue
        matched = matches.get((author, date, content))
        if matched is None:
            errors.add('review_not_matched')
            continue
        matched_sources.append(source)
        for field in FIELDS:
            if field in matched:
                fields.setdefault(field, matched[field])
        if len(fields) == len(FIELDS):
            break
    if matched_sources:
        return 'matched', fields, matched_sources
    status = next((reason for reason in ('review_not_matched', 'title_mismatch', 'invalid_evidence')
                   if reason in errors), 'unavailable_evidence')
    return status, fields, matched_sources


def csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def replay_sight_reviews(output, stop, emit):
    folder = Path(output).resolve()
    identity = uuid.uuid4().hex
    csv_path = folder / f'sight-review-replay-{identity}.csv'
    report_path = csv_path.with_suffix('.json')
    staged_csv, staged_report = csv_path.with_suffix('.csv.part'), report_path.with_suffix('.json.part')
    statuses, counts, tables_written = Counter(), Counter(), Counter()
    opened = set()
    screen = lru_cache(maxsize=16)(lambda source: read_screen(folder, source, opened))
    total = 0
    try:
        with review_database(folder) as db, staged_csv.open('w', encoding='utf-8-sig', newline='') as target:
            db.execute('BEGIN')
            existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            generations = (('reviews_scoped', 'object_id', 'scoped'), ('reviews', "''", 'legacy'))
            writer = csv.writer(target)
            writer.writerow(COLUMNS)
            for table, object_id, source_kind in generations:
                if table not in existing:
                    continue
                total += db.execute(f"SELECT COUNT(*) FROM {table} WHERE kind='sight'").fetchone()[0]
                rows = db.execute(f'''SELECT key,place,{object_id},author,date,rating,content,source
                                      FROM {table} WHERE kind='sight' ORDER BY rowid''')
                for row in rows:
                    if stop.is_set():
                        break
                    sources = [row[7]]
                    if table == 'reviews_scoped' and 'reviews_scoped_observations' in existing:
                        observed = db.execute('''SELECT source FROM reviews_scoped_observations
                                                 WHERE review_key=? ORDER BY observation_id DESC''', (row[0],))
                        sources = [item[0] for item in observed] + sources
                    status, fields, provenance = reconcile_row(row, sources, screen, stop)
                    if stop.is_set():
                        break
                    values = (source_kind, row[2], row[1], row[0], row[4], row[5], row[6],
                              *(fields.get(field) for field in FIELDS), status, ' | '.join(provenance))
                    writer.writerow([csv_cell(value) for value in values])
                    statuses[status] += 1
                    tables_written[source_kind] += 1
                    counts.update(fields.keys())
                    if sum(statuses.values()) % 100 == 0:
                        emit(f'已核对 {sum(statuses.values())} 条已保存点评')
        if not total:
            raise ValueError('当前目录没有可重读的已保存景点点评')
        result = {'source_method': SOURCE, 'replay_id': identity, 'csv': str(csv_path), 'report_file': str(report_path),
                  'reason': 'cancelled' if stop.is_set() else 'replayed', 'complete': False,
                  'rows_written': sum(statuses.values()), 'local_reviews': total, 'matched_reviews': statuses['matched'],
                  'status_counts': dict(statuses), 'record_sources': dict(tables_written),
                  'replayed_field_counts': {field: counts[field] for field in FIELDS},
                  'evidence_screens_read': len(opened),
                  'limitations': ['只重新读取已保存点评关联的本地页面。对象编号仍沿用原任务声明。',
                                  '旧版未绑定编号的记录和按编号保存的记录可能重叠。']}
        staged_report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        staged_csv.replace(csv_path)
        try:
            staged_report.replace(report_path)
        except OSError:
            csv_path.unlink(missing_ok=True)
            raise
    finally:
        staged_csv.unlink(missing_ok=True)
        staged_report.unlink(missing_ok=True)
    emit(f'导出 {result["rows_written"]} 条，{result["matched_reviews"]} 条找到对应页面')
    return result
