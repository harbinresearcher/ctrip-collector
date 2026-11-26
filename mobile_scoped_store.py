"""手机评论作用域存储；对象ID由操作者声明，不能证明页面归属。"""
import csv
import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

from mobile_review_fields import CARD_FIELDS, SCOPED_COUNT_FIELDS, SHOPPING_CARD_FIELDS, resolve_card_fields

logger = logging.getLogger(__name__)
MOBILE_REVIEW_RUN_ID = re.compile(r'[0-9]{8}T[0-9]{6}-[a-f0-9]{6}')


class MobileScopedStore:
    def __init__(self, folder, kind, object_id):
        # /* ===== 步骤1：建立独立作用域；数据源：操作者声明；操作：1) 校验类型和ID 2) 创建新表 ===== */
        logger.info('开始建立手机评论作用域')
        if (kind not in ('hotel', 'sight', 'restaurant', 'shopping') or not isinstance(object_id, str)
                or not re.fullmatch(r'[0-9]{1,20}', object_id) or int(object_id) <= 0):
            raise ValueError('手机点评需要支持的类型及正整数对象 ID')
        self.kind = kind
        self.object_id = str(int(object_id))
        self.scope = f'declared:ctrip:{kind}:{self.object_id}'
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.folder / 'reviews.sqlite3')
        # 1.1 不修改旧reviews表、旧主键或旧版无列名INSERT所依赖的列数。
        try:
            with self.db:
                self.db.execute('''CREATE TABLE IF NOT EXISTS reviews_scoped (
                    key TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL,
                    object_id TEXT NOT NULL, identity_source TEXT NOT NULL, place TEXT NOT NULL,
                    author TEXT, date TEXT, date_kind TEXT, rating TEXT, content TEXT,
                    has_expand INTEGER, raw_texts TEXT, source TEXT, captured_at TEXT)''')
                self.db.execute('CREATE INDEX IF NOT EXISTS reviews_scoped_object_idx ON reviews_scoped(kind,scope)')
                # 1.2 观察表不改变旧评论表或主评论计数，记录同指纹可见状态的变化。
                self.db.execute('''CREATE TABLE IF NOT EXISTS reviews_scoped_observations (
                    observation_id INTEGER PRIMARY KEY, review_key TEXT NOT NULL,
                    rating TEXT, has_expand INTEGER, raw_texts TEXT, source TEXT, captured_at TEXT,
                    FOREIGN KEY(review_key) REFERENCES reviews_scoped(key))''')
                self.db.execute('CREATE INDEX IF NOT EXISTS reviews_scoped_observation_key_idx ON reviews_scoped_observations(review_key,observation_id)')
                self.db.execute('''CREATE TABLE IF NOT EXISTS reviews_scoped_expansions (
                    review_key TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL, author TEXT NOT NULL,
                    stay_date TEXT NOT NULL, expanded_content TEXT NOT NULL, source TEXT NOT NULL, captured_at TEXT NOT NULL)''')
                self.db.execute('''CREATE INDEX IF NOT EXISTS reviews_scoped_expanded_idx
                    ON reviews_scoped_expansions(kind,scope,author,stay_date,expanded_content)''')
                self.db.execute('''CREATE TABLE IF NOT EXISTS reviews_scoped_media (
                    review_key TEXT PRIMARY KEY, visible_tiles INTEGER NOT NULL, source TEXT NOT NULL,
                    FOREIGN KEY(review_key) REFERENCES reviews_scoped(key))''')
                if self.kind in SCOPED_COUNT_FIELDS:
                    # Keep the historical table name and schema for existing databases.
                    self.db.execute('''CREATE TABLE IF NOT EXISTS reviews_scoped_sight_counts (
                        review_key TEXT NOT NULL, field TEXT NOT NULL, value INTEGER NOT NULL, source TEXT NOT NULL,
                        PRIMARY KEY(review_key, field),
                        FOREIGN KEY(review_key) REFERENCES reviews_scoped(key))''')
                self.db.execute('''CREATE TABLE IF NOT EXISTS mobile_review_runs (
                    run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL,
                    object_id TEXT NOT NULL, place TEXT NOT NULL, started_at TEXT NOT NULL,
                    final_reason TEXT)''')
                self.db.execute('''CREATE TABLE IF NOT EXISTS mobile_review_run_items (
                    item_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, review_key TEXT NOT NULL,
                    first_source TEXT NOT NULL, UNIQUE(run_id, review_key),
                    FOREIGN KEY(run_id) REFERENCES mobile_review_runs(run_id),
                    FOREIGN KEY(review_key) REFERENCES reviews_scoped(key))''')
        except Exception:
            self.db.close()
            raise
        logger.info('手机评论作用域建立完成')

    def start_run(self, run_id, place):
        with self.db:
            self.db.execute('INSERT INTO mobile_review_runs VALUES (?,?,?,?,?,?,NULL)',
                            (run_id, self.kind, self.scope, self.object_id, place,
                             datetime.now(timezone.utc).isoformat()))

    def finish_run(self, run_id, reason):
        with self.db:
            self.db.execute('UPDATE mobile_review_runs SET final_reason=? WHERE run_id=? AND scope=?',
                            (reason, run_id, self.scope))

    def save(self, place, rows, source, *, observed_keys=None, run_id=None):
        # /* ===== 步骤1：保存作用域评论；数据源：手机解析卡片；操作：1) 同ID去重 2) 单页事务提交 ===== */
        logger.info('开始保存作用域评论')
        if not isinstance(place, str) or not place.strip():
            raise ValueError('请填写对象名称')
        if run_id is not None and observed_keys is None:
            raise ValueError('记录本次点评需要提供当前屏的索引')
        added = 0
        with self.db:
            if run_id is not None and not self.db.execute(
                    'SELECT 1 FROM mobile_review_runs WHERE run_id=? AND scope=? AND final_reason IS NULL',
                    (run_id, self.scope)).fetchone():
                raise ValueError('没有对应的进行中点评任务')
            for row in rows:
                # 1.1 对象名称只用于显示；ID一致且名称变化不会造成重复评论。
                key = self.review_key(row)
                # Match an exact previously verified expansion to its original row, without rewriting the fingerprint.
                alias = self.db.execute('''SELECT review_key FROM reviews_scoped_expansions
                    WHERE kind=? AND scope=? AND author=? AND stay_date=? AND expanded_content=? LIMIT 2''',
                    (self.kind, self.scope, row['author'], row['date'], row['content'])).fetchall()
                if len(alias) == 1 and not self.db.execute('SELECT 1 FROM reviews_scoped WHERE key=?', (key,)).fetchone():
                    if observed_keys is not None:
                        observed_keys.setdefault(alias[0][0], source)
                    self._save_media_tiles(alias[0][0], row, source)
                    continue
                timestamp = datetime.now(timezone.utc).isoformat()
                raw_texts = json.dumps(row['raw_texts'], ensure_ascii=False)
                added += self.db.execute('''INSERT OR IGNORE INTO reviews_scoped
                    (key,kind,scope,object_id,identity_source,place,author,date,date_kind,rating,
                     content,has_expand,raw_texts,source,captured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (key, self.kind, self.scope, self.object_id, 'operator_declared_unverified', place,
                     row['author'], row['date'], row['date_kind'], row['rating'], row['content'],
                     int(row['has_expand']), raw_texts, source, timestamp)).rowcount
                if observed_keys is not None:
                    observed_keys.setdefault(key, source)
                self._save_media_tiles(key, row, source)
                if self.kind in SCOPED_COUNT_FIELDS:
                    self._save_display_counts(key, row, source)
                # 1.2 老记录第一次再次出现时，先补记原始状态和原采集时间。
                last = self.db.execute('''SELECT rating,has_expand,raw_texts FROM reviews_scoped_observations
                    WHERE review_key=? ORDER BY observation_id DESC LIMIT 1''', (key,)).fetchone()
                if last is None:
                    original = self.db.execute('''SELECT rating,has_expand,raw_texts,source,captured_at
                        FROM reviews_scoped WHERE key=?''', (key,)).fetchone()
                    self.db.execute('''INSERT INTO reviews_scoped_observations
                        (review_key,rating,has_expand,raw_texts,source,captured_at) VALUES (?,?,?,?,?,?)''',
                        (key, *original))
                    last = original[:3]
                # 1.3 只保存评分、展开标记或原始文本变化，不把重复出现算作新评论。
                current = (row['rating'], int(row['has_expand']), raw_texts)
                if current != last:
                    self.db.execute('''INSERT INTO reviews_scoped_observations
                        (review_key,rating,has_expand,raw_texts,source,captured_at) VALUES (?,?,?,?,?,?)''',
                        (key, *current, source, timestamp))
            if run_id is not None:
                self.db.executemany('''INSERT INTO mobile_review_run_items(run_id,review_key,first_source)
                    VALUES (?,?,?) ON CONFLICT(run_id,review_key) DO NOTHING''',
                                    ((run_id, key, first_source) for key, first_source in observed_keys.items()))
        logger.info('作用域评论保存完成：新增%s条', added)
        return added

    def _save_media_tiles(self, key, row, source):
        count = row.get('card_fields', {}).get('media_tiles_visible')
        if self.kind in ('hotel', 'restaurant') and type(count) is int and count > 0:
            self.db.execute('''INSERT INTO reviews_scoped_media VALUES (?,?,?)
                ON CONFLICT(review_key) DO UPDATE SET visible_tiles=excluded.visible_tiles, source=excluded.source
                WHERE excluded.visible_tiles > reviews_scoped_media.visible_tiles''', (key, count, source))

    def _save_display_counts(self, key, row, source):
        fields = row.get('card_fields', {})
        self.db.executemany('''INSERT INTO reviews_scoped_sight_counts VALUES (?,?,?,?)
            ON CONFLICT(review_key,field) DO UPDATE SET value=excluded.value, source=excluded.source''',
            [(key, field, value, source) for field in SCOPED_COUNT_FIELDS[self.kind]
             if type(value := fields.get(field)) is int and 0 <= value < 1_000_000_000])

    def review_key(self, row):
        fingerprint = json.dumps(['scoped-v1', self.kind, self.scope, row['author'], row['date'], row['content']],
                                 ensure_ascii=False)
        return hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()

    def save_expansion(self, row, expanded, source):
        logger.info('保存已核对的长评补采结果')
        key = self.review_key(row)
        with self.db:
            if not self.db.execute('SELECT 1 FROM reviews_scoped WHERE key=?', (key,)).fetchone():
                return False
            # A separately stored matching row is ambiguous; do not merge existing comments.
            if self.db.execute('SELECT 1 FROM reviews_scoped WHERE key=?', (self.review_key(expanded),)).fetchone():
                return False
            changed = self.db.execute('''INSERT INTO reviews_scoped_expansions VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(review_key) DO UPDATE SET expanded_content=excluded.expanded_content,
                    source=excluded.source,captured_at=excluded.captured_at
                WHERE length(excluded.expanded_content)>length(reviews_scoped_expansions.expanded_content)
                  AND substr(excluded.expanded_content,1,length(reviews_scoped_expansions.expanded_content))
                      =reviews_scoped_expansions.expanded_content''',
                (key, self.kind, self.scope, row['author'], row['date'], expanded['content'], source,
                 datetime.now(timezone.utc).isoformat())).rowcount
        return changed == 1

    def count(self):
        # /* ===== 步骤1：统计作用域；数据源：新评论表；操作：1) 限定类型和ID 2) 返回累计数 ===== */
        logger.info('开始统计作用域评论')
        # 1.1 旧表和其他ID不参与本次目标判断。
        count = self.db.execute('SELECT COUNT(*) FROM reviews_scoped WHERE kind=? AND scope=?',
                                (self.kind, self.scope)).fetchone()[0]
        logger.info('作用域评论统计完成：%s条', count)
        return count

    def export(self, *, run_id=None, observed_keys=None):
        # /* ===== 步骤1：导出作用域；数据源：新评论表；操作：1) 阻止公式 2) 独立文件原子替换 ===== */
        logger.info('开始导出作用域评论')
        # 1.1 v2前缀与旧文件名不同，杜绝覆盖同名历史导出。
        tag = hashlib.sha256(self.scope.encode('utf-8')).hexdigest()[:24]
        if observed_keys is None:
            target = self.folder / f'{self.kind}-v2-{tag}.csv'
        else:
            if not run_id or not MOBILE_REVIEW_RUN_ID.fullmatch(run_id):
                raise ValueError('本次手机点评任务编号无效')
            target = self.folder / f'{self.kind}-sample-{run_id}.csv'
        temporary = target.with_suffix('.csv.tmp')
        try:
            with temporary.open('w', encoding='utf-8-sig', newline='') as output:
                write_review_csv(self.db, self.kind, self.scope, output, observed_keys)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        logger.info('作用域评论导出完成：%s', target)
        return target

    def close(self):
        # /* ===== 步骤1：关闭存储；数据源：当前连接；操作：1) 释放数据库句柄 ===== */
        logger.info('开始关闭作用域存储')
        self.db.close()
        logger.info('作用域存储关闭完成')


def write_review_csv(db, kind, scope, output, observed_keys):
    query = 'SELECT * FROM reviews_scoped WHERE kind=? AND scope=?'
    params = kind, scope
    columns = [column[0] for column in db.execute(query + ' LIMIT 0', params).description]
    if observed_keys is None:
        rows = db.execute(query + ' ORDER BY rowid', params)
        extra_cols = []
    else:
        rows = sample_rows(db, query, params, observed_keys)
        extra_cols = ['run_order', 'run_first_seen_source']
    media_available = kind in ('hotel', 'restaurant') and db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviews_scoped_media'").fetchone()
    sight_counts_available = kind in SCOPED_COUNT_FIELDS and db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviews_scoped_sight_counts'").fetchone()
    card_columns = (*CARD_FIELDS, *SHOPPING_CARD_FIELDS) if kind == 'shopping' else CARD_FIELDS
    writer = csv.writer(output)
    writer.writerow([*columns, *card_columns, 'expanded_content', *extra_cols])
    for index, row in enumerate(rows, start=1):
        record = dict(zip(columns, row))
        fields = resolve_card_fields(db, kind, record['key'], record['content'],
                                     record['raw_texts'], record['rating'], observed=True,
                                     media_available=media_available, sight_counts_available=sight_counts_available)
        expanded = db.execute('SELECT expanded_content FROM reviews_scoped_expansions WHERE review_key=?',
                                   (record['key'],)).fetchone()
        values = [*row, *(fields.get(field) for field in card_columns), expanded[0] if expanded else '']
        if observed_keys is not None:
            values.extend((index, observed_keys[record['key']]))
        writer.writerow(["'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value for value in values])

def sample_rows(db, query, params, observed_keys):
    key_iter = iter(observed_keys)
    while keys := tuple(islice(key_iter, 300)):
        placeholders = ','.join('?' for _ in keys)
        rows = {row[0]: row for row in db.execute(
            f'{query} AND key IN ({placeholders})', (*params, *keys))}
        for key in keys:
            yield rows[key]
