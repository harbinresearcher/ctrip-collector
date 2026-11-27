"""手机侧缺口队列：只读对账与手机库，列出下一步该在手机上采哪个对象。

只读：不连接设备、不访问网络。对象清单、链接与网页通道的被拦状态来自
coverage_audit.write_coverage_audit 的落盘对账；手机侧已采数来自 reviews.sqlite3 的
reviews_scoped；手机侧目标数来自手机报告里「全部」筛选的精确计数。拿不到目标就是
未知，不拿已采数冒充目标。

网页通道的 source_exhausted 不等于手机侧采尽：网页采不出来时手机正是剩下的通道，
所以本队列只按手机侧证据判断采尽（手机报告连续多屏没有新点评）与被拦
（手机报告要求人工验证或登录，且只在当天）。
"""
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from android_coverage import local_file
from coverage_audit import KINDS, OBJECT_KEY, blocked_today, count_value, read_evidence, write_coverage_audit
from mobile_review_context import exact_count

logger = logging.getLogger(__name__)
# 手机报告停止原因里能判定「这一侧采不出来」与「今天被拦」的取值。
EXHAUSTED_REASONS = {'stalled_unverified'}
BLOCKED_REASONS = {'requires_manual_action'}
IDENTITY = re.compile(r'[1-9][0-9]{0,19}')
# 单个对象单轮最多建议补采的条数，与 --drive / --collect-gaps 的口径一致。
DEFAULT_LIMIT = 200
DEFAULT_MAX_OBJECTS = 10


def phone_review_counts(folder):
    # /* ===== 步骤1：统计手机库已采数；数据源：reviews.sqlite3 的 reviews_scoped；操作：1) 只读打开 2) 按对象分组 ===== */
    counts, state = {}, 'missing'
    database = Path(folder) / 'reviews.sqlite3'
    # 1.1 缺失、无表、不可读三种状态分开；读不到不能当成「已采 0 条」照常输出。
    if not local_file(database):
        return counts, state
    try:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            db.execute('BEGIN')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviews_scoped'").fetchone():
                return counts, 'no_table'
            for kind, identity, amount in db.execute(
                    'SELECT kind,object_id,COUNT(*) FROM reviews_scoped GROUP BY kind,object_id'):
                identity = str(identity)
                if kind in KINDS and IDENTITY.fullmatch(identity):
                    counts[f'{kind}-{identity}'] = amount
    except sqlite3.Error:
        return {}, 'unreadable'
    return counts, 'read'


def report_target(data):
    # /* ===== 步骤2：读手机侧「全部」计数；数据源：手机报告；操作：1) 优先本次全部核对 2) 退到筛选条计数 3) 不一致就不猜 ===== */
    # 2.1 本次明确选中「全部」且已核对时，报告里的 display_total 是可直接比较的整数。
    check = data.get('review_count_check')
    if isinstance(check, dict) and check.get('filter') == '全部' and check.get('state') == 'compared_display_only':
        value = count_value(check.get('display_total'))
        if value is not None:
            return value
    # 2.2 否则读屏幕上「全部」筛选条的精确计数；4.3w 这类近似计数按未知处理。
    context = data.get('sampling_context')
    observations = context.get('observations') if isinstance(context, dict) else None
    if not isinstance(observations, list):
        return None
    totals = set()
    for observation in observations:
        filters = observation.get('visible_filters') if isinstance(observation, dict) else None
        if not isinstance(filters, list):
            continue
        for entry in filters:
            if isinstance(entry, dict) and entry.get('label') == '全部':
                value = exact_count(entry.get('display_count'))
                if value is not None:
                    totals.add(value)
    return totals.pop() if len(totals) == 1 else None


def phone_evidence(folder):
    # /* ===== 步骤3：读手机报告；数据源：report-*.json；操作：1) 取最近一次停止原因 2) 取最近可用的全部计数 ===== */
    evidence = {}
    for path in sorted(Path(folder).glob('report-*.json')):
        try:
            data = read_evidence(path)
            kind, identity = data.get('kind'), data.get('object_id')
            if kind not in KINDS or identity is None:
                continue
            identity = str(identity)
            if not IDENTITY.fullmatch(identity):
                continue
            mtime = path.stat().st_mtime
        except (OSError, ValueError, TypeError):
            continue
        order = (mtime, path.name)
        key = f'{kind}-{identity}'
        record = evidence.setdefault(key, {'target': None, 'target_order': None, 'reason': None,
                                           'mtime': None, 'latest_order': None, 'place': None})
        # 3.1 最近一次报告的停止原因决定今天还能不能采；目标数取最近一次带「全部」计数的报告。
        if record['latest_order'] is None or order > record['latest_order']:
            reason = data.get('reason')
            record.update(latest_order=order, mtime=mtime,
                          reason=reason if isinstance(reason, str) else None)
        target = report_target(data)
        if target is not None and (record['target_order'] is None or order > record['target_order']):
            record.update(target=target, target_order=order)
        place = data.get('place')
        if record['place'] is None and isinstance(place, str) and place.strip():
            record['place'] = place.strip()
    return evidence


def skip_reason(obj, phone):
    # /* ===== 步骤4：判断是否不进待办；数据源：对账被拦状态与手机报告；操作：1) 当天被拦优先 2) 手机侧采尽 3) 人工验证仅当天 ===== */
    if obj.get('blocked'):
        status = obj.get('blocked_status') or '未知状态'
        return {'reason': 'blocked',
                'detail': f'网页通道今天被平台拦截（{status}）：今天停跑，明天再试，不换 IP 或账号重试'}
    report_reason = phone.get('reason')
    if report_reason in EXHAUSTED_REASONS:
        return {'reason': 'source_exhausted',
                'detail': f'手机侧这一轮连续多屏没有新点评（{report_reason}）：这一侧已采尽，先不要重复翻页'}
    # 4.1 手机侧要求人工验证/登录也是「被拦」，但只挡当天，隔天重新进队列。
    if report_reason in BLOCKED_REASONS and blocked_today({'mtime': phone.get('mtime')}):
        return {'reason': 'blocked',
                'detail': f'手机侧要求人工验证或登录（{report_reason}）：今天停跑，明天再试'}
    return None


def queue_order(entry):
    # never_collected 在前；其余按 missing 降序，未知缺口排在有数缺口之后；同 missing 按对象键升序。
    return (0 if entry['reason'] == 'never_collected' else 1,
            0 if entry['missing'] is not None else 1,
            -(entry['missing'] or 0), entry['object_key'])


def write_phone_queue(folder, max_objects=DEFAULT_MAX_OBJECTS):
    # /* ===== 步骤5：生成手机侧缺口队列；数据源：只读对账与手机库；操作：1) 合并对象 2) 分类排序 3) 按上限截断 ===== */
    logger.info('开始生成手机侧缺口队列')
    if type(max_objects) is not int or max_objects < 1:
        raise ValueError('对象上限必须是正整数')
    folder = Path(folder).resolve()
    # 5.0 对账只读落盘证据；它本身会把本次汇总追加到 audit-history.jsonl。
    audit = write_coverage_audit(folder)
    counts, database_state = phone_review_counts(folder)
    evidence = phone_evidence(folder)
    queue, skipped = [], []
    considered = never_collected = target_unknown = with_phone_gap = 0
    for obj in audit['objects']:
        key = obj.get('object_key')
        # 5.1 旧表 mobile: 类型的键没有对象编号，无法生成 --object-ref 命令，不混进待办。
        if not isinstance(key, str) or not OBJECT_KEY.fullmatch(key):
            continue
        considered += 1
        phone = evidence.get(key, {})
        collected = counts.get(key, 0)
        target = phone.get('target')
        missing = max(target - collected, 0) if target is not None else None
        if collected == 0:
            never_collected += 1
        if target is None:
            target_unknown += 1
        if missing is not None and missing > 0:
            with_phone_gap += 1
        row = {'object_key': key, 'place': obj.get('name') or phone.get('place'),
               'kind': obj.get('kind'), 'url': obj.get('url'), 'target': target,
               'target_unknown': target is None, 'phone_collected': collected, 'missing': missing}
        # 5.2 手机侧没有缺口（已知目标且已采满）不占待办位置，也不算采尽或被拦。
        if missing == 0:
            continue
        skip = skip_reason(obj, phone)
        if skip:
            skipped.append({**row, **skip})
            continue
        if collected == 0:
            reason = 'never_collected'
        elif target is None:
            reason = 'target_unknown'
        else:
            reason = 'partial'
        suggested = DEFAULT_LIMIT if missing is None else min(missing, DEFAULT_LIMIT)
        # 5.3 --collect 的 --limit 是本次累计目标：已有 N 条时再补 M 条要传 N+M，否则会空跑。
        command = (f'python app.py --collect --object-ref {key} --limit {collected + suggested} '
                   f'--output {folder}')
        queue.append({**row, 'reason': reason, 'limit_suggestion': suggested,
                      'limit_note': '目标未知，先按 200 条试采；采完重新对账' if missing is None else None,
                      'command': command})
    queue.sort(key=queue_order)
    queue = queue[:max_objects]
    summary = {'objects': considered, 'with_phone_gap': with_phone_gap,
               'never_collected': never_collected, 'target_unknown': target_unknown,
               'skipped': len(skipped)}
    report = {'scope': 'phone_gap_queue_readonly', 'output': str(folder), 'max_objects': max_objects,
              'phone_database': database_state, 'queue': queue, 'skipped': skipped, 'summary': summary,
              'limitations': ['不连接设备、不访问网络；只读对账与已被手机采集落库的数据。',
                              '手机侧目标来自手机报告里「全部」筛选的精确计数；4.3w 这类近似计数按目标未知处理。',
                              '--collect 的 --limit 是本次累计目标，command 里已换算成「已采 + 建议追加」。',
                              '网页通道采尽不代表手机侧采尽，本队列只按手机侧证据判断采尽与被拦。']}
    logger.info('手机侧缺口队列完成：%d 个对象，%d 个待办，%d 个跳过',
                considered, len(queue), len(skipped))
    return report