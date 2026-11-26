"""无人值守驱动循环：第一轮发现目录，之后每轮只看缺口是否真的变小。

不另写采集或对账逻辑：目录发现走调用方给的入口，续采和重新对账都走
gap_collect.collect_gaps（它内部就是 coverage_audit 的 before / after）。
本模块只负责轮次、继续条件、停止原因，以及按实测耗时算出的速率与 ETA，
保证不会无限循环。
"""
import logging
from pathlib import Path
from threading import Event
from time import monotonic

from gap_collect import collect_gaps

logger = logging.getLogger(__name__)
# 停止原因只允许这四种；rounds[].reason 另有一个 continued，表示缺口变小、进入下一轮。
STOP_REASONS = ('round_budget_reached', 'no_progress', 'all_targets_met', 'cancelled')
LIMITATIONS = ['继续的唯一条件是 after.missing 严格小于 before.missing；采集调用自己报的成功不算数。',
               '目录发现只在第一轮执行，后续轮次不再请求目录接口，因此运行期间新增的对象不会被发现。',
               '手机对象与酒店浏览器对象不在自动续采范围，缺口只报不采，会直接表现为 no_progress。',
               '清单内已知目标缺口清零不等于平台全量：目标数只来自目录报告或接口回显，分页与对象预算决定已发现对象数。',
               'complete 固定为 false：本命令只回答「清单内的目标还差多少」，不代表平台数据已采全。',
               'ETA 按本次实测速率外推：平台限速、风控与接口波动都会让它偏乐观。']


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


def drive_rounds(folder, rounds, discovery_pages, max_objects, limit, gap_limit,
                 *, discover=None, collector=None, stop=None, emit=None, clock=None):
    # /* ===== 步骤3：按轮驱动；数据源：只读对账、既有续采入口与时钟；操作：1) 只在首轮发现 2) 每轮对账-续采-对账 3) 缺口不降就停 4) 记耗时与速率 ===== */
    logger.info('开始无人值守驱动循环')
    if rounds < 1 or discovery_pages < 1 or max_objects < 1 or limit < 1 or gap_limit < 1:
        raise ValueError('轮数、目录页数、目录对象数、目标条数和每轮对象上限必须大于零')
    if collector is None:
        raise ValueError('驱动循环需要采集入口')
    stop, emit, clock = stop or Event(), emit or logger.info, clock or monotonic
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    report = {'reason': 'cancelled', 'rounds': [], 'rounds_used': 0, 'discovery': None,
              'output': str(folder), 'complete': False,
              'round_budget': rounds, 'discovery_pages': discovery_pages, 'max_objects': max_objects,
              'limit': limit, 'gap_limit': gap_limit, 'limitations': LIMITATIONS}
    # 3.1 整体吞吐只按全部轮次的实测累计算，不掺目录发现和参数校验的时间。
    spent, gained = 0.0, 0
    for number in range(1, rounds + 1):
        if stop.is_set():
            report['reason'] = 'cancelled'
            break
        emit(f'第 {number}/{rounds} 轮')
        # 3.2 目录接口只在第一轮打一次；后续轮次只续采已落盘的对象清单。
        if number == 1 and discover is not None:
            found = discover(folder, discovery_pages, max_objects)
            report['discovery'] = {key: value for key, value in found.items() if key != 'objects'}
            report['discovery']['object_count'] = len(found.get('objects') or [])
            emit(f"目录发现结束：{found.get('reason')}，发现 {report['discovery']['object_count']} 个对象")
        # 3.3 续采内部先对账再采、采完重新对账；这里的 before / after 就是两次审计的 summary。
        started = clock()
        gap = collect_gaps(folder, limit, gap_limit, stop=stop, emit=emit, collector=collector)
        elapsed = clock() - started
        before, after = gap['before'], gap['after']
        delta = after['collected'] - before['collected']
        spent, gained = spent + elapsed, gained + delta
        progress = after['missing'] < before['missing']
        # 3.4 停止原因只能从四种里挑一个：四个判断都不成立才进入下一轮。
        if gap['reason'] == 'cancelled' or stop.is_set():
            reason = 'cancelled'
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
                                 'reviews_per_minute': reviews_per_minute(delta, elapsed),
                                 'collect_reason': gap['reason'], 'selected': gap['selected'],
                                 'processed': gap['processed'], 'reason': reason})
        report['rounds_used'] = number
        emit(f"第 {number} 轮：missing {before['missing']} → {after['missing']}，{reason}")
        if reason != 'continued':
            report['reason'] = reason
            break
    # 3.5 报告里的吞吐与 ETA 都是实测外推：一条没采到就不给速率，速率为空就不给 ETA。
    report['throughput'] = {'reviews_per_minute': reviews_per_minute(gained, spent),
                            'collected_total': gained, 'elapsed_seconds': round(spent, 3)}
    report['eta_seconds'] = eta_seconds(report['rounds'], report['throughput']['reviews_per_minute'])
    logger.info('驱动循环结束：%s，%s 轮', report['reason'], report['rounds_used'])
    return report