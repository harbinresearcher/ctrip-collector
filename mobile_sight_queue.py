"""Checkpoints for a Windows workbench scene queue."""

import errno
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from mobile_scoped_store import MOBILE_REVIEW_RUN_ID
from object_identity import MAX_EVIDENCE_BYTES, OBJECT_REF, read_evidence


QUEUE_PREFIX = 'sight-review-queue-'
QUEUE_ID = re.compile(r'[a-f0-9]{32}')
QUEUE_FILE = re.compile(rf'{QUEUE_PREFIX}([a-f0-9]{{32}})\.json')
EVIDENCE_FILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]*\.json')
ITEM_FIELDS = ('object_ref', 'object_id', 'name', 'address', 'identity_evidence')
QUEUE_MODE = 'app_sight_queue'
QUEUE_REASONS = ('ready', 'running', 'queue_recorded', 'stopped_by_user', 'needs_attention')
ITEM_STATUSES = ('pending', 'running', 'recorded', 'retry')


def queue_path(output, queue_id):
    if not isinstance(queue_id, str) or not QUEUE_ID.fullmatch(queue_id):
        raise ValueError('景点点评队列编号无效')
    return Path(output).resolve() / f'{QUEUE_PREFIX}{queue_id}.json'


def check_destination(path):
    if path.is_symlink() or path.is_junction() or (path.exists() and not path.is_file()):
        raise ValueError('景点点评队列路径必须使用普通文件')


def queue_time(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('景点点评队列时间无效') from exc
    if parsed.tzinfo is None:
        raise ValueError('景点点评队列时间缺少时区')
    return parsed


def validate_queue(queue, queue_id):
    if (not isinstance(queue, dict) or queue.get('queue_id') != queue_id
            or queue.get('mode') != QUEUE_MODE or queue.get('reason') not in QUEUE_REASONS
            or queue.get('complete') is not False):
        raise ValueError('景点点评队列记录无效')
    queue_time(queue.get('created_at'))
    queue_time(queue.get('updated_at'))
    limit, swipes, items = queue.get('limit'), queue.get('max_swipes'), queue.get('items')
    if type(limit) is not int or not 1 <= limit <= 100000 or type(swipes) is not int or not 1 <= swipes <= 10000:
        raise ValueError('目标点评数须为 1 至 100000，滑动上限须为 1 至 10000')
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise ValueError('请选择 1 至 20 个景点')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or item.get('status') not in ITEM_STATUSES:
            raise ValueError('景点点评队列里的对象状态无效')
        ref = item.get('object_ref')
        match = OBJECT_REF.fullmatch(ref) if isinstance(ref, str) else None
        if match is None or match[1] != 'sight' or item.get('object_id') != match[2] or ref in seen:
            raise ValueError('队列只能加入有唯一编号的景点')
        seen.add(ref)
        for key in ('name', 'address'):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip() or not value.isprintable():
                raise ValueError('队列中的景点需要名称和地址证据')
        evidence = item.get('identity_evidence')
        if not isinstance(evidence, str) or not EVIDENCE_FILE.fullmatch(evidence):
            raise ValueError('景点身份证据文件名无效')
        attempt = item.get('attempt_run_id')
        if attempt is not None and (not isinstance(attempt, str) or not MOBILE_REVIEW_RUN_ID.fullmatch(attempt)):
            raise ValueError('景点点评队列的尝试编号无效')
        result = item.get('result')
        if result is None:
            continue
        if not isinstance(result, dict):
            raise ValueError('景点点评任务结果无效')
        for field in ('run_id', 'sample_run_id'):
            run = result.get(field)
            if run is not None and (not isinstance(run, str) or not MOBILE_REVIEW_RUN_ID.fullmatch(run)):
                raise ValueError('景点点评结果的任务编号无效')


def create_sight_queue(output, items, limit, max_swipes):
    if not isinstance(items, (tuple, list)) or any(not isinstance(item, dict) for item in items):
        raise ValueError('请选择 1 至 20 个景点')
    now = datetime.now(timezone.utc).isoformat()
    queue = {'queue_id': uuid.uuid4().hex, 'mode': QUEUE_MODE, 'created_at': now, 'updated_at': now,
             'limit': limit, 'max_swipes': max_swipes, 'reason': 'ready', 'complete': False,
             'items': [dict({key: item.get(key) for key in ITEM_FIELDS}, status='pending', result=None)
                       for item in items]}
    save_sight_queue(output, queue)
    return queue


def load_sight_queue(output, queue_id):
    path = queue_path(output, queue_id)
    if not path.exists() and not path.is_symlink():
        raise FileNotFoundError('找不到这份景点点评队列')
    queue = read_evidence(path)
    validate_queue(queue, queue_id)
    return queue


def save_sight_queue(output, queue):
    if not isinstance(queue, dict):
        raise ValueError('景点点评队列记录无效')
    queue_id = queue.get('queue_id')
    path = queue_path(output, queue_id)
    updated = dict(queue, updated_at=datetime.now(timezone.utc).isoformat())
    validate_queue(updated, queue_id)
    payload = json.dumps(updated, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(payload) > MAX_EVIDENCE_BYTES:
        raise ValueError('景点点评队列报告过大')
    path.parent.mkdir(parents=True, exist_ok=True)
    check_destination(path)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    queue['updated_at'] = updated['updated_at']


def list_sight_queues(output):
    folder = Path(output).resolve()
    summaries = []
    fields = ('queue_id', 'created_at', 'updated_at', 'reason', 'limit', 'max_swipes')
    for path in folder.glob(f'{QUEUE_PREFIX}*.json'):
        if (match := QUEUE_FILE.fullmatch(path.name)) is None:
            continue
        try:
            queue = load_sight_queue(folder, match[1])
        except (OSError, ValueError):
            continue
        items = queue['items']
        summaries.append(dict({key: queue[key] for key in fields}, item_count=len(items),
                              recorded_count=sum(item['status'] == 'recorded' for item in items)))
    summaries.sort(key=lambda row: (queue_time(row['updated_at']), row['queue_id']), reverse=True)
    return summaries[:50]


@contextmanager
def lock_sight_queue(output, queue_id):
    """Prevent another process from running the same queue while this handle is open."""
    path = queue_path(output, queue_id)
    lock_path = path.with_name(f'.{path.stem}.lock')
    check_destination(lock_path)
    with lock_path.open('a+b') as stream:
        try:
            if os.name == 'nt':
                import msvcrt

                if stream.tell() == 0:
                    stream.write(b'\0')
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or getattr(exc, 'winerror', None) in (33, 36):
                raise RuntimeError('这份景点点评队列正在另一个工作台运行') from exc
            raise
        # Keep the lock file so all processes use the same handle target. The OS releases the lock on close or exit.
        yield
