"""Read a bounded, privacy-limited history for one scoped mobile review."""
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)


def read_observations(folder, review_key, before=None):
    # /* ===== 步骤1：核验预览输入；数据源：本地评论指纹和游标；操作：1) 限定格式 2) 只读固定数据库 ===== */
    logger.info('开始核验手机观察预览')
    if not isinstance(review_key, str) or not re.fullmatch(r'[0-9a-f]{64}', review_key):
        raise ValueError('评论指纹须为64位小写十六进制')
    if before is not None and (type(before) is not int or not 0 < before <= 9223372036854775807):
        raise ValueError('观察分页游标无效')
    root = Path(folder).resolve()
    database = root / 'reviews.sqlite3'
    # 1.1 不允许固定数据库路径通过符号链接指向其他位置，也不创建缺失数据库。
    if database.is_symlink():
        raise ValueError('手机评论数据库不能是符号链接')
    if not database.is_file():
        raise FileNotFoundError('本地手机评论数据库不存在')
    if database.resolve(strict=True).parent != root:
        raise ValueError('手机评论数据库不在输出目录')
    result = {'items': [], 'has_more': False, 'next_cursor': None,
              'history_status': 'not_recorded',
              'review_identity': 'content_fingerprint_not_platform_id',
              'object_identity': 'operator_declared_unverified',
              'limitations': ['内容指纹不是平台评论ID，可能合并不同评论。',
                              '对象ID由操作者填写，不能证明当前手机页面归属。',
                              '无观察记录不代表历史上没有变化。']}
    logger.info('手机观察预览输入核验完成')
    # /* ===== 步骤2：读取指定评论历史；数据源：作用域评论及观察表；操作：1) 限定对象 2) 游标分页 ===== */
    logger.info('开始读取手机观察历史')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        # 2.1 同一只读事务中确认主评论存在，不返回游离观察记录。
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviews_scoped'").fetchone():
            raise ValueError('本地作用域评论表不存在')
        if not db.execute('SELECT 1 FROM reviews_scoped WHERE key=?', (review_key,)).fetchone():
            raise LookupError('指定的手机评论不存在')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviews_scoped_observations'").fetchone():
            result['history_status'] = 'unavailable'
        else:
            # 2.2 在数据库内比较前一观察原文，即使跨页也能正确判断；原文和来源不离开数据库。
            sql = '''SELECT o.observation_id,o.rating,o.has_expand,o.captured_at,
                CASE WHEN EXISTS (SELECT 1 FROM reviews_scoped_observations p
                    WHERE p.review_key=o.review_key AND p.observation_id<o.observation_id)
                THEN o.raw_texts IS NOT (SELECT p.raw_texts FROM reviews_scoped_observations p
                    WHERE p.review_key=o.review_key AND p.observation_id<o.observation_id
                    ORDER BY p.observation_id DESC LIMIT 1) ELSE NULL END
                FROM reviews_scoped_observations o WHERE o.review_key=? '''
            params = [review_key]
            if before is not None:
                sql += 'AND o.observation_id<? '
                params.append(before)
            sql += 'ORDER BY o.observation_id DESC LIMIT 21'
            rows = db.execute(sql, params).fetchall()
            # 2.3 空页与记录未建立分开：游标翻到末尾不等于历史不存在。
            exists = bool(rows) or bool(db.execute('SELECT 1 FROM reviews_scoped_observations WHERE review_key=? LIMIT 1', (review_key,)).fetchone())
            result['history_status'] = 'available' if exists else 'not_recorded'
            result['items'] = [{'score': score, 'has_expand': None if expanded is None else bool(expanded),
                                'captured_at': stamp, 'raw_texts_changed': None if changed is None else bool(changed)}
                               for _, score, expanded, stamp, changed in rows[:20]]
            result['has_more'] = len(rows) > 20
            result['next_cursor'] = rows[19][0] if len(rows) > 20 else None
    logger.info('手机观察历史读取完成：%s条', len(result['items']))
    return result
