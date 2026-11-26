"""无人值守驱动循环：第一轮发现目录，之后每轮只看缺口是否真的变小。

不另写采集或对账逻辑：目录发现走调用方给的入口，续采和重新对账都走
gap_collect.collect_gaps（它内部就是 coverage_audit 的 before / after）。
本模块只负责轮次、继续条件、停止原因、按实测耗时算出的速率与 ETA，
以及三种长跑保护（每日配额、连续失败熔断、速率下降降速），保证不会无限循环。
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from threading import Event
from time import monotonic

from gap_collect import collect_gaps

logger = logging.getLogger(__name__)
# 停止原因：前四种由轮次进度决定，后两种由长跑保护触发（配额到点 / 连续失败熔断）；
# rounds[].reason 另有一个 continued，表示缺口变小、进入下一轮。
STOP_REASONS = ('round_budget_reached', 'no_progress', 'all_targets_met', 'cancelled',
                'daily_quota_reached', 'circuit_open')
LIMITATIONS = ['继续的唯一条件是 after.missing 严格小于 before.missing；采集调用自己报的成功不算数。',
               '目录发现只在第一轮执行，后续轮次不再请求目录接口，因此运行期间新增的对象不会被发现。',
               '手机对象与酒店浏览器对象不在自动续采范围，缺口只报不采，会直接表现为 no_progress。',
               '清单内已知目标缺口清零不等于平台全量：目标数只来自目录报告或接口回显，分页与对象预算决定已发现对象数。',
               'complete 固定为 false：本命令只回答「清单内的目标还差多少」，不代表平台数据已采全。',
               'ETA 按本次实测速率外推：平台限速、风控与接口波动都会让它偏乐观。',
               '每日配额按轮结束时的累计新增判定，最后一轮可能超过剩余额度；只统计本命令各轮的实测新增。',
               '熔断只看本轮内连续失败的对象，成功一次即清零；降速只调下一轮的每轮对象数（gap_limit），不改每对象条数（limit）。']
# 配额状态落在输出目录：同一天重启接着算，本地日期变了自动归零。
STATE_FILE = 'drive-state.json'


def reviews_per_minute(collected_delta, elapsed_seconds):
    # /* ===== 步骤1：把实测条数与实测耗时换成速率；数据源：本轮/整体 delta 与时钟差；操作：1) delta 为 0 或耗时为 0 时给 null ===== */
    # 1.1 没有新增条数时速度没有定义：不写 0、也不写 inf，避免把「没采到」表现成「速度是 0」。
    if collected_delta == 0 or elapsed_seconds <= 0:
        return None
    return round(collected_delta / elapsed_seconds * 60, 1)


def eta_seconds(rounds, rate):
    # /* ===== 步骤2：用实测速率外推剩余缺口；数据源：最后一轮 after.summary + 整体速率；操作：1) 缺速率给 null 2) 目标未知给 null ===== */
    # 2.1 missing 只是已知目标的缺口：有 unknown_target 时它不是真实剩余量，外推会变成假精确。
    if not rounds or not rate:
        return None
    after = rounds[-1]['after']
    missing = after.get('missing')
    if missing is None or after.get('unknown_target'):
        return None
    return round(missing / rate * 60, 1)


def targets_met(summary):
    # /* ===== 步骤1：判断已知目标是否全达成；数据源：对账 summary；操作：1) 至少一个已知目标 2) 缺口为零 ===== */
    # 1.1 目标全未知时不能算达成：没有可比较的总数，也就没有证据说已经采完。
    known = summary.get('object_count', 0) - summary.get('unknown_target', 0)
    return known > 0 and summary.get('missing') == 0


def quota_view(daily, used):
    # /* ===== 步骤3.1：给出配额口径；数据源：配置与当日累计；操作：1) 不限量时 remaining 写 null ===== */
    # 3.1.1 不限量时 remaining 不写 0：0 读起来是「额度已用完」，那是另一种含义。
    return {'daily': daily, 'used_today': used,
            'remaining': None if daily <= 0 else max(daily - used, 0)}


def local_day():
    # 配额按本地时区的自然日切分：跨零点的下一次运行自动从零开始。
    return datetime.now().date().isoformat()


def read_state(folder, day):
    # /* ===== 步骤3.2：读回当天累计；数据源：输出目录的 drive-state.json；操作：1) 读不到或坏文件按零 2) 日期不是今天就归零 ===== */
    # 3.2.1 隔天必须从 0 起步：不能让前一天的额度压死今天。
    path = Path(folder) / STATE_FILE
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'date': day, 'collected_today': 0}
    if not isinstance(data, dict) or data.get('date') != day:
        return {'date': day, 'collected_today': 0}
    used = data.get('collected_today')
    return {'date': day, 'collected_today': used if type(used) is int and used >= 0 else 0}


def save_state(folder, state):
    # /* ===== 步骤3.3：原子写回配额状态；数据源：本轮结束后的累计；操作：1) 先写临时文件再替换 ===== */
    path = Path(folder) / STATE_FILE
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
    except OSError as exc:
        # 3.3.1 状态写不进去不影响本轮结果，但重启后配额会从旧数字继续算。
        logger.warning('驱动状态写入失败，重启后配额可能不准：%s', exc)
    finally:
        temporary.unlink(missing_ok=True)


def failed_result(result):
    # 与 gap_collect 的 processed 口径一致：reason=error 或 error 非空都算这个对象失败。
    return isinstance(result, dict) and (result.get('reason') == 'error' or bool(result.get('error')))


class BreakerStop(Event):
    # 熔断的停止信号叠加调用方停止信号：哪个先到，本轮都立刻收手。
    def __init__(self, outer):
        super().__init__()
        self.outer = outer

    def is_set(self):
        return self.outer.is_set() or super().is_set()


def breaker_guard(collector, threshold, stop):
    # /* ===== 步骤3.4：包一层采集入口做熔断；数据源：对象采集结果；操作：1) 连续失败计数 2) 到阈值记下对象与错误并置停止信号 ===== */
    trip = {'tripped': False, 'object_url': None, 'error': ''}
    consecutive = [0]

    def note(url, error):
        # 3.4.1 成功一次就清零计数：熔断看的是连续失败，不是累计失败。
        consecutive[0] += 1
        if consecutive[0] >= threshold and not trip['tripped']:
            trip.update(tripped=True, object_url=url, error=error)
            stop.set()

    def guarded(url, output, limit, round_stop, emit):
        try:
            result = collector(url, output, limit, round_stop, emit)
        except Exception as exc:
            # 3.4.2 采集入口抛异常也算失败：先记数，再原样抛给批任务去记 reason=error。
            note(url, str(exc))
            raise
        if failed_result(result):
            note(url, result.get('error') or '采集失败')
        else:
            consecutive[0] = 0
        return result

    return guarded, trip


def drive_rounds(folder, rounds, discovery_pages, max_objects, limit, gap_limit,
                 *, discover=None, collector=None, stop=None, emit=None, clock=None,
                 daily_quota=0, breaker_failures=0, min_per_minute=0):
    # /* ===== 步骤4：按轮驱动；数据源：只读对账、既有续采入口与时钟；操作：1) 只在首轮发现 2) 每轮对账-续采-对账 3) 缺口不降就停 4) 记耗时与速率 5) 配额/熔断/降速保护 ===== */
    logger.info('开始无人值守驱动循环')
    if rounds < 1 or discovery_pages < 1 or max_objects < 1 or limit < 1 or gap_limit < 1:
        raise ValueError('轮数、目录页数、目录对象数、目标条数和每轮对象上限必须大于零')
    if daily_quota < 0 or breaker_failures < 0 or min_per_minute < 0:
        raise ValueError('每日配额、熔断阈值和最低速率不能为负数')
    if collector is None:
        raise ValueError('驱动循环需要采集入口')
    stop, emit, clock = stop or Event(), emit or logger.info, clock or monotonic
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    # 4.0 配额状态按本地自然日读回：同一天重启接着算，隔天归零。
    state = read_state(folder, local_day())
    report = {'reason': 'cancelled', 'rounds': [], 'rounds_used': 0, 'discovery': None,
              'output': str(folder), 'complete': False,
              'round_budget': rounds, 'discovery_pages': discovery_pages, 'max_objects': max_objects,
              'limit': limit, 'gap_limit': gap_limit, 'limitations': LIMITATIONS,
              'quota': quota_view(daily_quota, state['collected_today']),
              'breaker': {'threshold': breaker_failures, 'tripped': False, 'object_key': None, 'error': ''},
              'pacing': {'min_per_minute': min_per_minute, 'degraded': False, 'gap_limit_next': gap_limit}}
    # 4.1 整体吞吐只按全部轮次的实测累计算，不掺目录发现和参数校验的时间。
    spent, gained = 0.0, 0
    round_gap = gap_limit
    for number in range(1, rounds + 1):
        if stop.is_set():
            report['reason'] = 'cancelled'
            break
        # 4.2 当天额度已用完就不再开始新一轮：进程重启后读状态也照样生效。
        if daily_quota > 0 and state['collected_today'] >= daily_quota:
            report['reason'] = 'daily_quota_reached'
            emit(f"当天已新增 {state['collected_today']} 条，达到每日配额 {daily_quota}，停止")
            break
        emit(f'第 {number}/{rounds} 轮')
        # 4.3 目录接口只在第一轮打一次；后续轮次只续采已落盘的对象清单。
        if number == 1 and discover is not None:
            found = discover(folder, discovery_pages, max_objects)
            report['discovery'] = {key: value for key, value in found.items() if key != 'objects'}
            report['discovery']['object_count'] = len(found.get('objects') or [])
            emit(f"目录发现结束：{found.get('reason')}，发现 {report['discovery']['object_count']} 个对象")
        # 4.4 续采内部先对账再采、采完重新对账；这里的 before / after 就是两次审计的 summary。
        #     熔断开着时另包一层采集入口：连续失败到阈值就让本轮立刻收手，不再打下一个对象。
        round_stop, round_collector = stop, collector
        trip = {'tripped': False, 'object_url': None, 'error': ''}
        if breaker_failures > 0:
            round_stop = BreakerStop(stop)
            round_collector, trip = breaker_guard(collector, breaker_failures, round_stop)
        used_gap = round_gap
        started = clock()
        gap = collect_gaps(folder, limit, used_gap, stop=round_stop, emit=emit, collector=round_collector)
        elapsed = clock() - started
        before, after = gap['before'], gap['after']
        delta = after['collected'] - before['collected']
        spent, gained = spent + elapsed, gained + delta
        progress = after['missing'] < before['missing']
        # 4.5 配额是跨进程的累计：每轮采完先落状态，再判断要不要停；跨零点按新的一天从零算。
        day = local_day()
        if day != state['date']:
            state = {'date': day, 'collected_today': 0}
        state['collected_today'] += delta
        save_state(folder, state)
        report['quota'] = quota_view(daily_quota, state['collected_today'])
        # 4.6 降速：本轮确实采到东西但速率低于阈值，就把下一轮每轮对象数减半（至少 1），不清零、不放弃。
        rate = reviews_per_minute(delta, elapsed)
        degraded = min_per_minute > 0 and rate is not None and rate < min_per_minute
        if degraded:
            round_gap = max(round_gap // 2, 1)
            report['pacing'].update(degraded=True, gap_limit_next=round_gap)
            emit(f"降速：本轮 {rate} 条/分钟低于 {min_per_minute}，下一轮每轮对象数减为 {round_gap}")
        # 4.7 停止原因：长跑保护优先于进度判断，被保护停下的运行 complete 仍为 false。
        if stop.is_set():
            reason = 'cancelled'
        elif trip['tripped']:
            reason = 'circuit_open'
            key = next((row['object_key'] for row in gap['processed'] if row['url'] == trip['object_url']), None)
            report['breaker'].update(tripped=True, object_key=key, error=trip['error'])
            emit(f"熔断：连续 {breaker_failures} 个对象失败，{key}：{trip['error']}")
        elif daily_quota > 0 and state['collected_today'] >= daily_quota:
            reason = 'daily_quota_reached'
        elif targets_met(after):
            reason = 'all_targets_met'
        elif not progress:
            reason = 'no_progress'
        elif number == rounds:
            reason = 'round_budget_reached'
        else:
            reason = 'continued'
        report['rounds'].append({'round': number, 'before': before, 'after': after,
                                 'collected_delta': delta,
                                 'missing_delta': before['missing'] - after['missing'],
                                 'elapsed_seconds': round(elapsed, 3),
                                 'reviews_per_minute': rate, 'gap_limit': used_gap,
                                 'degraded': degraded,
                                 'collect_reason': gap['reason'], 'selected': gap['selected'],
                                 'processed': gap['processed'], 'reason': reason})
        report['rounds_used'] = number
        emit(f"第 {number} 轮：missing {before['missing']} → {after['missing']}，{reason}")
        if reason != 'continued':
            report['reason'] = reason
            break
    # 4.8 报告里的吞吐与 ETA 都是实测外推：一条没采到就不给速率，速率为空就不给 ETA。
    report['throughput'] = {'reviews_per_minute': reviews_per_minute(gained, spent),
                            'collected_total': gained, 'elapsed_seconds': round(spent, 3)}
    report['eta_seconds'] = eta_seconds(report['rounds'], report['throughput']['reviews_per_minute'])
    logger.info('驱动循环结束：%s，%s 轮', report['reason'], report['rounds_used'])
    return report