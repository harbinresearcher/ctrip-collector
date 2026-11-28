"""Checkpoints for a Windows workbench scene queue."""

import json
import os
import re
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import sleep

from local_locks import FileLockBusy, exclusive_file_lock
from mobile_scoped_store import MOBILE_REVIEW_RUN_ID
from object_identity import MAX_EVIDENCE_BYTES, OBJECT_REF, read_evidence


QUEUE_PREFIX = 'sight-review-queue-'
QUEUE_ID = re.compile(r'[a-f0-9]{32}')
QUEUE_FILE = re.compile(rf'{QUEUE_PREFIX}([a-f0-9]{{32}})\.json')
EVIDENCE_FILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]*\.json')
AI_REPORT_FILE = re.compile(r'sight-ai-([a-f0-9]{32})\.json')
ITEM_FIELDS = ('object_ref', 'object_id', 'name', 'address', 'identity_evidence')
GAP_EVIDENCE_FIELDS = ('collected', 'target', 'missing', 'suggested_add')
MAX_REVIEW_TARGET = 100000
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


def validate_item_ai(item):
    report_id = item.get('ai_report_id')
    if report_id is not None and (not isinstance(report_id, str) or not QUEUE_ID.fullmatch(report_id)):
        raise ValueError('景点 AI 总结任务编号无效')
    summary = item.get('ai_summary')
    if summary is None:
        return
    if not isinstance(summary, dict) or summary.get('status') not in ('saved', 'not_visible', 'error'):
        raise ValueError('景点 AI 总结状态无效')
    if summary['status'] == 'saved':
        filename = summary.get('report_file')
        match = AI_REPORT_FILE.fullmatch(filename) if isinstance(filename, str) else None
        sections = summary.get('section_count')
        if (match is None or (report_id is not None and match[1] != report_id)
                or type(sections) is not int or sections < 1
                or summary.get('summary_state') not in ('expanded', 'preview', 'visible')):
            raise ValueError('景点 AI 总结报告引用无效')


def planned_review_target(evidence, budget):
    if not isinstance(evidence, dict) or evidence.keys() != set(GAP_EVIDENCE_FIELDS):
        raise ValueError('景点待补清单证据无效')
    collected, target, missing, suggested = (evidence[key] for key in GAP_EVIDENCE_FIELDS)
    if type(collected) is not int or not 0 <= collected < MAX_REVIEW_TARGET:
        raise ValueError('景点待补清单的已采数量无效')
    if type(suggested) is not int or suggested < 1:
        raise ValueError('景点待补清单的建议追加数无效')
    if target is None:
        if missing is not None:
            raise ValueError('目标未知时必须保留未知缺口')
    elif (type(target) is not int or type(missing) is not int or missing != target - collected
          or missing < suggested):
        raise ValueError('景点待补清单的目标与缺口不符')
    return min(MAX_REVIEW_TARGET, collected + min(budget, suggested))


def validate_queue(queue, queue_id):
    if (not isinstance(queue, dict) or queue.get('queue_id') != queue_id
            or queue.get('mode') != QUEUE_MODE or queue.get('reason') not in QUEUE_REASONS
            or queue.get('complete') is not False):
        raise ValueError('景点点评队列记录无效')
    queue_time(queue.get('created_at'))
    queue_time(queue.get('updated_at'))
    if type(queue.get('save_ai_summary', False)) is not bool:
        raise ValueError('景点 AI 总结采集设置无效')
    gap_plan = queue.get('gap_plan', False)
    if type(gap_plan) is not bool:
        raise ValueError('景点待补清单设置无效')
    limit, swipes, items = queue.get('limit'), queue.get('max_swipes'), queue.get('items')
    if type(limit) is not int or not 1 <= limit <= MAX_REVIEW_TARGET or type(swipes) is not int or not 1 <= swipes <= 10000:
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
        validate_item_ai(item)
        if gap_plan:
            target = planned_review_target(item.get('gap_evidence'), limit)
            if type(item.get('review_target')) is not int or item['review_target'] != target:
                raise ValueError('景点待补清单的本轮目标与证据不符')
        elif 'review_target' in item or 'gap_evidence' in item:
            raise ValueError('手动景点队列不能带有待补清单设置')
        result = item.get('result')
        if result is None:
            continue
        if not isinstance(result, dict):
            raise ValueError('景点点评任务结果无效')
        for field in ('run_id', 'sample_run_id'):
            run = result.get(field)
            if run is not None and (not isinstance(run, str) or not MOBILE_REVIEW_RUN_ID.fullmatch(run)):
                raise ValueError('景点点评结果的任务编号无效')


def create_sight_queue(output, items, limit, max_swipes, *, save_ai_summary=False, gap_plan=False):
    if not isinstance(items, (tuple, list)) or any(not isinstance(item, dict) for item in items):
        raise ValueError('请选择 1 至 20 个景点')
    now = datetime.now(timezone.utc).isoformat()
    fields = (*ITEM_FIELDS, 'review_target', 'gap_evidence') if gap_plan else ITEM_FIELDS
    queue = {'queue_id': uuid.uuid4().hex, 'mode': QUEUE_MODE, 'created_at': now, 'updated_at': now,
             'limit': limit, 'max_swipes': max_swipes, 'reason': 'ready', 'complete': False,
             'save_ai_summary': save_ai_summary,
             'items': [dict({key: item.get(key) for key in fields}, status='pending', result=None)
                       for item in items]}
    if gap_plan:
        queue['gap_plan'] = gap_plan
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
        for attempt in range(4):
            try:
                temporary.replace(path)
            except PermissionError as exc:
                if attempt == 3 or getattr(exc, 'winerror', None) not in (5, 32, 33):
                    raise
                # A concurrent Windows reader can briefly prevent the atomic rename.
                sleep(0.03 * (2 ** attempt))
                check_destination(path)
            else:
                break
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
        summary = dict({key: queue[key] for key in fields}, item_count=len(items),
                       recorded_count=sum(item['status'] == 'recorded' for item in items))
        if queue.get('save_ai_summary'):
            summary['ai_saved_count'] = sum((item.get('ai_summary') or {}).get('status') == 'saved' for item in items)
        summaries.append(summary)
    summaries.sort(key=lambda row: (queue_time(row['updated_at']), row['queue_id']), reverse=True)
    return summaries[:50]


@contextmanager
def lock_sight_queue(output, queue_id):
    """Prevent another process from running the same queue while this handle is open."""
    path = queue_path(output, queue_id)
    lock_path = path.with_name(f'.{path.stem}.lock')
    check_destination(lock_path)
    with ExitStack() as lock:
        try:
            lock.enter_context(exclusive_file_lock(lock_path))
        except FileLockBusy as exc:
            raise RuntimeError('这份景点点评队列正在另一个工作台运行') from exc
        yield
