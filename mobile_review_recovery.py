"""Read committed review-run membership and regenerate the ordered sample."""
import io
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import SpooledTemporaryFile

from mobile_scoped_store import MOBILE_REVIEW_RUN_ID, write_review_csv


def review_database(folder):
    root = Path(folder).resolve()
    database = root / 'reviews.sqlite3'
    if database.is_symlink() or database.resolve().parent != root or not database.is_file():
        raise FileNotFoundError('本地手机点评数据库不可用')
    return closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True))


def has_run_tables(db):
    return db.execute("""SELECT COUNT(*) FROM sqlite_master WHERE type='table'
                      AND name IN ('mobile_review_runs','mobile_review_run_items')""").fetchone()[0] == 2


def saved_review_runs(folder):
    try:
        database = review_database(folder)
    except FileNotFoundError:
        return []
    with database as db:
        if not has_run_tables(db):
            return []
        rows = db.execute('''SELECT r.run_id,r.kind,r.object_id,r.place,r.started_at,r.final_reason,
                                  COUNT(i.item_id)
                           FROM mobile_review_runs r JOIN mobile_review_run_items i USING(run_id)
                           GROUP BY r.run_id
                           ORDER BY r.started_at DESC,r.run_id DESC LIMIT 100''').fetchall()
    return [{'run_id': run, 'kind': kind, 'object_id': object_id, 'name': name, 'started_at': stamp,
             'status': 'unfinalized' if reason is None else 'finalized', 'stop_reason': reason,
             'sample_count': count}
            for run, kind, object_id, name, stamp, reason, count in rows if MOBILE_REVIEW_RUN_ID.fullmatch(run)]


def open_saved_review_sample(folder, run_id):
    if not isinstance(run_id, str) or not MOBILE_REVIEW_RUN_ID.fullmatch(run_id):
        raise ValueError('无效的点评任务编号')
    with review_database(folder) as db:
        # One read transaction keeps membership and derived fields in the same database snapshot.
        db.execute('BEGIN')
        if not has_run_tables(db):
            raise LookupError('没有已保存的点评任务')
        run = db.execute('SELECT kind,scope FROM mobile_review_runs WHERE run_id=?', (run_id,)).fetchone()
        if not run:
            raise LookupError('找不到这次点评任务')
        observed_keys = dict(db.execute('''SELECT i.review_key,i.first_source FROM mobile_review_run_items i
                                          JOIN reviews_scoped r ON r.key=i.review_key
                                          WHERE i.run_id=? AND r.kind=? AND r.scope=? ORDER BY i.item_id''',
                                        (run_id, *run)))
        if not observed_keys:
            raise LookupError('这次任务没有已保存的点评')
        stream = SpooledTemporaryFile(max_size=4 * 1024 * 1024)
        try:
            wrapper = io.TextIOWrapper(stream, encoding='utf-8-sig', newline='')
            try:
                write_review_csv(db, *run, wrapper, observed_keys)
                wrapper.flush()
            finally:
                wrapper.detach()
            stream.seek(0)
        except BaseException:
            stream.close()
            raise
    return stream
