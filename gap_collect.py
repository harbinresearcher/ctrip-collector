"""缺口驱动续采：读只读对账结果，只对仍缺数据的对象把缺口再采小一点。

不另写下载逻辑：每个对象复用 batch.collect_batch 和调用方给的对象采集入口，
本模块只负责挑对象、传目标条数、以及采完后重新对账。
酒店走专用浏览器且需要人工登录，不放进自动续采，缺口对象仍用 --url 采集。
对象之间可以按 --workers 做线程级并发；同一对象内部仍串行翻页，间隔不变。
"""
import logging
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Event, Lock

from batch import collect_batch
from coverage_audit import write_coverage_audit

logger = logging.getLogger(__name__)
# 只驱动网页公开点评通道；手机对象与酒店浏览器对象都不在自动续采范围内。
NETWORK_KINDS = ('sight', 'restaurant', 'shopping')
# 并发上限与命令行一致：同一 IP 上再开更多路请求只会抬高风控概率。
MAX_WORKERS = 3


class QuotaBudget:
    # /* ===== 步骤0：并发下的每日配额闸门；数据源：当日剩余额度；操作：1) 开对象前预占 2) 按实际新增退还 ===== */
    # 预占在开对象之前完成：两个线程同时开工也不会把同一份额度用两遍。
    def __init__(self, remaining):
        self.remaining = max(int(remaining), 0)
        self._lock = Lock()

    def claim(self, wanted):
        with self._lock:
            take = min(max(int(wanted), 0), self.remaining)
            self.remaining -= take
            return take

    def refund(self, amount):
        # 0.1 实际新增少于预占时把差额还回额度：额度按真实新增算，不让保守预占白占额度。
        if amount <= 0:
            return
        with self._lock:
            self.remaining += int(amount)


def is_collectable(obj):
    # /* ===== 步骤1：判断对象能否续采；数据源：对账输出；操作：1) 只认已知目标的缺口 2) 必须有网络对象链接 ===== */
    # 1.1 手机对象键不由网页通道续采；未知目标没有可比较的总数，也不能靠续采把缺口算清。
    key = obj.get('object_key')
    return (isinstance(key, str) and not key.startswith('mobile:') and obj.get('kind') in NETWORK_KINDS
            and obj.get('target_unknown') is False and type(obj.get('missing')) is int and obj['missing'] > 0
            and isinstance(obj.get('url'), str) and obj['url'].strip() != '')


def gap_candidates(objects):
    # 1.2 缺口大的先采；同缺口按对象键排序，保证同一目录每次输出顺序稳定。
    # 1.3 通道翻到上限（channel_capped）或被平台拦住（blocked）的对象不再进续采队列：
    #     前者在这一侧采不出来，再翻也是空转；后者当天必须停跑，不能被当成普通缺口继续打。
    return sorted((obj for obj in objects if is_collectable(obj)
                   and obj.get('gap_reason') not in {'channel_capped', 'blocked'}),
                  key=lambda obj: (-obj['missing'], obj['object_key']))


def blocked_signal(result):
    # /* ===== 步骤1.4：识别被拦结果；数据源：单对象采集报告；操作：1) 只认 blocked=True 2) 返回原始状态 ===== */
    # 1.4.1 被拦不能退回普通 error：缺口续采与驱动循环都要据此整轮停手。
    if not isinstance(result, dict) or result.get('blocked') is not True:
        return None
    return str(result.get('blocked_status') or 'unknown')


def idle_reason(objects):
    # /* ===== 步骤2：说明为什么没有可处理对象；数据源：对账输出；操作：1) 区分无缺口/缺链接/未知目标 ===== */
    if not objects:
        return 'no_objects'
    gaps = [obj for obj in objects
            if obj.get('target_unknown') is False and type(obj.get('missing')) is int and obj['missing'] > 0]
    if not gaps:
        # 2.1 全部对象都没有平台总数时，才是 target_unknown_only。
        return 'target_unknown_only' if all(obj.get('target_unknown') for obj in objects) else 'no_gap'
    if any(not obj.get('url') for obj in gaps):
        return 'gap_url_missing'
    # 2.2 缺口只落在手机对象上：本命令不驱动手机通道，不假装已经续采。
    if all(isinstance(obj.get('object_key'), str) and obj['object_key'].startswith('mobile:') for obj in gaps):
        return 'mobile_object_only'
    # 2.3 酒店要打开专用浏览器并等人工登录，不自动拉起；仍可用 --url 采。
    if all(obj.get('kind') == 'hotel' for obj in gaps):
        return 'hotel_object_only'
    return 'no_collectable_object'


def run_concurrently(selected, limit, workers, stop, emit, run_object, quota_remaining):
    # /* ===== 步骤3.4：对象级并发调度；数据源：候选对象与当日剩余额度；操作：1) 只给空闲线程开新对象 2) stop/配额到点就不再提交 ===== */
    # 3.4.0 并发只并行不同对象：同一对象始终只有一个线程在跑，翻页间隔与串行一致。
    budget = QuotaBudget(quota_remaining) if quota_remaining is not None else None
    finished = {}
    pending = list(selected)
    active = {}
    halted = None
    # 3.4.0.1 已经提交但还没开跑的任务也要能停：拦到就置位，任务进采集前先看这个标志，
    #         否则瞬间返回的采集会让排队任务在 pending 清空前就发出请求。
    blocked_flag = Event()
    if stop is None:
        stop = Event()

    def guarded(obj, add):
        if blocked_flag.is_set() or stop.is_set():
            return None
        return run_object(obj, add)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='gap-collect') as pool:
        while True:
            while len(active) < workers and pending:
                if stop.is_set() or blocked_flag.is_set():
                    pending.clear()
                    break
                obj = pending.pop(0)
                add = limit
                if budget is not None:
                    # 3.4.1 额度按对象预占：开对象前先扣，另一路线程不可能再拿到同一份额度。
                    add = budget.claim(min(limit, int(obj['target']) - int(obj['collected'])))
                    if add <= 0:
                        pending.clear()
                        emit('配额到点：不再开始新对象，已开对象跑完就停')
                        break
                active[pool.submit(guarded, obj, add)] = (obj, add)
            if not active:
                break
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            # 3.4.4 完成顺序不确定：按对象键处理，被拦对象与在途对象的先后不随线程调度变化。
            for future in sorted(done, key=lambda item: active[item][0]['object_key']):
                obj, add = active.pop(future)
                outcome = future.result()
                if outcome is None:
                    # 3.4.4.1 提交后才发现被拦：这个对象一次请求都没发，不写进 processed。
                    continue
                effective, result = outcome
                finished[obj['object_key']] = (obj, result, add, effective)
                signal = blocked_signal(result)
                if signal is not None and halted is None:
                    # 3.4.5 被平台拦住：已排队的后续对象全部取消，在途请求跑完就停，不再开新对象。
                    halted = {'object_key': obj['object_key'], 'status': signal}
                    blocked_flag.set()
                    pending.clear()
                if budget is not None:
                    # 3.4.2 只按实测新增退还差额；采集没报新增就按已预占算，宁少开对象也不超发。
                    actual = result.get('new_count')
                    if type(actual) is int and 0 <= actual < add:
                        budget.refund(add - actual)
    # 3.4.3 完成先后不确定：按对象键排序输出，报告不随线程调度变化。
    return [finished[key] for key in sorted(finished)], halted


def collect_gaps(folder, limit, gap_limit=10, stop=None, emit=None, collector=None,
                 *, workers=1, quota_remaining=None):
    # /* ===== 步骤3：按缺口续采；数据源：只读对账与既有采集入口；操作：1) 排序限量 2) 逐对象采（可并发）3) 重新对账 ===== */
    logger.info('开始缺口续采')
    if limit < 1 or gap_limit < 1:
        raise ValueError('目标条数和对象上限必须大于零')
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f'并发数必须是 1 到 {MAX_WORKERS} 的整数')
    if collector is None:
        raise ValueError('缺口续采需要采集入口')
    stop = stop or Event()
    emit = emit or logger.info
    folder = Path(folder).resolve()
    # 3.1 先对账：缺口只从已落盘证据算，不为算缺口额外发请求。
    before = write_coverage_audit(folder)
    objects = before['objects']
    candidates = gap_candidates(objects)
    selected = candidates[:gap_limit]
    if not selected:
        # 3.2 没有可处理对象不是错误：写清原因，调用方按退出码 0 处理。
        reason = idle_reason(objects)
        logger.info('没有可处理的缺口对象：%s', reason)
        return {'reason': reason, 'blocked': None, 'limit': limit, 'gap_limit': gap_limit, 'workers': workers,
                'before': before['summary'], 'after': before['summary'],
                'selected': [], 'processed': [], 'output': str(folder)}
    emit(f"缺口续采：{len(selected)} 个对象（候选 {len(candidates)} 个）")

    def target_for(obj, add):
        # 3.2.2 目标 = min(已有 + 本次追加, 平台总数)；两处口径统一走这个函数。
        return min(int(obj['collected']) + add, int(obj['target']))

    def run_object(obj, add):
        # 3.3 单对象走既有批任务链路：停止原因、错误和导出错误都由它原样记录，这里不改写成成功。
        effective = target_for(obj, add)
        # 3.3.1 --limit 是「本次最多再补多少条」，不是对象总目标：已有 30 条时再补 10 条
        #       必须是 40 条的目标，否则目标早就达到，续采变成空跑、缺口永远不动。
        emit(f"续采 {obj['object_key']}：缺口 {obj['missing']}，已有 {obj['collected']} 条，本次目标 {effective} 条")
        batch = collect_batch([obj['url']], folder, effective, collector, stop, emit)
        results = batch.get('results') or []
        if results:
            return effective, results[0]
        if batch.get('reused_results'):
            return effective, dict(batch['reused_results'][0], reason='reused_verified')
        return effective, {'reason': 'no_result', 'error': batch.get('error') or batch.get('reason') or ''}

    if workers == 1:
        # 3.3.2 默认串行：保持原有处理顺序与日志，一个对象采完再开下一个。
        serial, halted = [], None
        for obj in selected:
            if stop.is_set():
                break
            effective, result = run_object(obj, limit)
            serial.append((obj, result, limit, effective))
            signal = blocked_signal(result)
            if signal is not None:
                # 3.3.4 被平台拦住就整轮停：后面的对象一个请求都不发。
                halted = {'object_key': obj['object_key'], 'status': signal}
                break
        runs = serial
    else:
        # 3.3.3 串行路径不传配额：配额仍按轮结束累计判定，workers=1 的行为与以前完全一致。
        runs, halted = run_concurrently(selected, limit, workers, stop, emit, run_object, quota_remaining)
    # 3.4 采完重新对账：after 是数据库与证据的真实计数，不是对本次调用的推测。
    after = write_coverage_audit(folder)
    by_key = {row['object_key']: row for row in after['objects']}
    processed = []
    for obj, result, add, effective in runs:
        row = by_key.get(obj['object_key'], {})
        processed.append({'object_key': obj['object_key'], 'name': obj.get('name'), 'url': obj['url'],
                          'before_missing': obj['missing'],
                          'requested_add': add,
                          'target_used': effective,
                          'after_collected': row.get('collected'), 'after_missing': row.get('missing'),
                          'reason': result.get('reason') or 'unknown',
                          'error': result.get('error') or '',
                          'export_error': result.get('export_error') or '',
                          'blocked': result.get('blocked') is True,
                          'blocked_status': result.get('blocked_status') or ''})
    # 3.5 被拦优先于取消：平台拒绝本身就是这一轮的停止原因，报告里要留下对象与状态。
    report = {'reason': 'blocked' if halted else 'cancelled' if stop.is_set() else 'gap_processed',
              'blocked': halted,
              'limit': limit, 'gap_limit': gap_limit, 'workers': workers,
              'before': before['summary'], 'after': after['summary'],
              'selected': [obj['object_key'] for obj in selected], 'processed': processed,
              'output': str(folder)}
    logger.info('缺口续采结束：处理 %s 个对象', len(processed))
    return report