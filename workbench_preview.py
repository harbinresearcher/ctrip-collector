"""Read a small review preview without exposing arbitrary files or raw fields."""
import json
import logging
import os
import re
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from mobile_review_fields import resolve_card_fields

logger = logging.getLogger(__name__)
REVIEW_SCREEN_SOURCE = re.compile(r'raw/([0-9]{8}T[0-9]{6}-[0-9a-f]{6})/([0-9]{5,6})\.xml')


def first_saved_screen(source):
    # Convert the stored relative name to metadata without reading or exposing the file.
    if isinstance(source, str) and (match := REVIEW_SCREEN_SOURCE.fullmatch(source.replace('\\', '/'))):
        return {'run_id': match[1], 'screen': int(match[2]) + 1}
    return None


def open_network_observations(folder, object_key):
    # /* ===== 步骤1：打开观察文件；数据源：固定输出目录；操作：1) 校验对象名 2) 安全打开普通文件 ===== */
    logger.info('开始核验网页评论观察文件')
    if not isinstance(object_key, str) or not re.fullmatch(r'(hotel|sight|restaurant|shopping)-[1-9][0-9]*', object_key):
        raise ValueError('对象编号无效')
    root = Path(folder).resolve()
    path = root / f'{object_key}-observations.jsonl'
    # 1.1 只接受输出目录直属文件，拒绝链接及指向目录外的路径。
    if (path.is_symlink() or path.is_junction() or not path.is_file()
            or path.resolve(strict=True).parent != root):
        raise ValueError('观察文件不存在或不属于当前输出目录')
    # 1.2 Windows 打开重解析点本身；其他平台拒绝跟随末级符号链接。
    if os.name == 'nt':
        import _winapi
        import msvcrt
        handle = _winapi.CreateFile(str(path), 0x80000000, 7, 0, 3, 0x00200000, 0)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _winapi.CloseHandle(handle)
            raise
    else:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('观察文件不是普通文件')
        stream = os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise
    logger.info('网页评论观察文件核验完成')
    return stream


def read_mobile_preview(folder, before=None, *, scoped=False):
    # /* ===== 步骤1：读取手机评论；数据源：独立手机库；操作：1) 只读分页 2) 标明身份来源 ===== */
    logger.info('开始读取手机评论预览')
    if before is not None and (type(before) is not int or not 0 < before <= 9223372036854775807):
        raise ValueError('无效的评论分页游标')
    database = Path(folder).resolve() / 'reviews.sqlite3'
    if not database.is_file():
        logger.info('手机评论预览结束：尚无评论库')
        return {'items': [], 'has_more': False, 'next_cursor': None, 'scope': 'mobile_scoped_database' if scoped else 'mobile_ui_database'}
    # 1.1 手机评论使用内容指纹，不能伪装成网络评论ID。
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        table = 'reviews_scoped' if scoped else 'reviews'
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            logger.info('手机评论预览结束：所选数据表不存在')
            return {'items': [], 'has_more': False, 'next_cursor': None, 'scope': 'mobile_scoped_database' if scoped else 'mobile_ui_database'}
        rows = db.execute('SELECT rowid,kind,place,content,rating,captured_at,has_expand,raw_texts,'
                          + ('object_id,key' if scoped else 'NULL,NULL') + ',source FROM ' + table + ' '
                          + ('WHERE rowid < ? ' if before is not None else '')
                          + 'ORDER BY rowid DESC LIMIT 21', (before,) if before is not None else ()).fetchall()
        observed = scoped and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviews_scoped_observations'").fetchone()
        media_available = scoped and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviews_scoped_media'").fetchone()
        sight_counts_available = scoped and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviews_scoped_sight_counts'").fetchone()
        card_fields = [resolve_card_fields(db, row[1], row[9], row[3], row[7], row[4], observed,
                                           media_available=media_available, sight_counts_available=sight_counts_available)
                       for row in rows[:20]]
        has_expanded = scoped and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviews_scoped_expansions'").fetchone()
        expanded = {row[9]: result[0] for row in rows[:20]
                    if has_expanded and (result := db.execute(
                        'SELECT expanded_content FROM reviews_scoped_expansions WHERE review_key=?', (row[9],)).fetchone())}
    items = []
    # 1.2 只返回预览所需字段，不暴露作者、原始界面或其他个人信息。
    for (_, kind, place, content, rating, captured_at, has_expand, _, object_id, local_key, source), fields in zip(rows[:20], card_fields):
        text = str(expanded.get(local_key, content) or '')
        item = {'kind': kind, 'place': place, 'content': text[:4000], 'score': str(rating or '')[:80],
                'fetched_at': captured_at, 'truncated': len(text) > 4000,
                'has_expand': bool(has_expand), 'object_id': object_id,
                'identity_verification': 'operator_declared_unverified'}
        if local_key in expanded:
            item['expanded_content_observed'] = True
        if fields:
            reply = fields.get('hotel_reply')
            if reply and len(reply) > 4000:
                fields['hotel_reply'] = reply[:4000]
                item['hotel_reply_truncated'] = True
            item['card_fields'] = fields
        if scoped:
            item['local_key'] = local_key
        if screen := first_saved_screen(source):
            item['first_saved_screen'] = screen
        items.append(item)
    logger.info('手机评论预览读取完成：%s条', len(items))
    return {'items': items, 'has_more': len(rows) > 20,
            'next_cursor': rows[19][0] if len(rows) > 20 else None,
            'scope': 'mobile_scoped_database' if scoped else 'mobile_ui_database'}


def read_scoped_mobile_preview(folder, before=None):
    # /* ===== 步骤1：读取ID作用域；数据源：新手机表；操作：1) 独立读取 2) 保留人工声明标签 ===== */
    logger.info('开始读取作用域评论预览')
    result = read_mobile_preview(folder, before=before, scoped=True)
    logger.info('作用域评论预览读取完成')
    return result


def read_preview(folder, before=None):
    # /* ===== 步骤1：读取本地评论；数据源：固定数据库；操作：1) 只读 2) 最多20条 ===== */
    logger.info('开始读取评论预览')
    if before is not None and (type(before) is not int or not 0 < before <= 9223372036854775807):
        raise ValueError('无效的评论分页游标')
    database = Path(folder).resolve() / 'network.sqlite3'
    if not database.is_file():
        logger.info('预览结束：尚无评论库')
        return {'items': [], 'has_more': False, 'next_cursor': None, 'scope': 'local_database'}
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        # 1.0 游标使用已展示的最小rowid，新插入记录不改变后续页边界。
        rows = db.execute('SELECT rowid,object_key,comment_id,data,fetched_at FROM network_reviews '
                          + ('WHERE rowid < ? ' if before is not None else '')
                          + 'ORDER BY rowid DESC LIMIT 21', (before,) if before is not None else ()).fetchall()
    items = []
    availability = {}
    # 1.1 只投影展示字段，正文用纯文本显示，不解释为HTML。
    for rowid, key, identity, raw, fetched_at in rows[:20]:
        # 1.1.0 每个对象只检查一次文件；旧库没有观察文件时不显示下载入口。
        if key not in availability:
            try:
                with open_network_observations(folder, key):
                    availability[key] = True
            except (OSError, ValueError):
                availability[key] = False
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('invalid row')
            content = data.get('content') or data.get('originContent') or ''
            items.append({'object_key': key, 'comment_id': identity,
                          'content': str(content)[:4000], 'score': str(data.get('score', ''))[:80],
                          'fetched_at': fetched_at, 'truncated': len(str(content)) > 4000,
                          'observations_available': availability[key]})
        except (ValueError, TypeError):
            items.append({'object_key': key, 'comment_id': identity, 'content': '此条记录无法解析', 'score': '', 'fetched_at': fetched_at, 'truncated': False,
                          'observations_available': availability[key]})
    logger.info('评论预览读取完成：%s条', len(items))
    return {'items': items, 'has_more': len(rows) > 20,
            'next_cursor': rows[19][0] if len(rows) > 20 else None, 'scope': 'local_database'}
