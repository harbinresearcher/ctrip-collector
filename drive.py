"""无人值守驱动循环：第一轮发现目录，之后每轮只看缺口是否真的变小。

不另写采集或对账逻辑：目录发现走调用方给的入口，续采和重新对账都走
gap_collect.collect_gaps（它内部就是 coverage_audit 的 before / after）。
本模块只负责轮次、继续条件和停止原因，保证不会无限循环。
"""
import logging
from pathlib import Path
from threading import Event

from gap_collect import collect_gaps

logger = logging.getLogger(__name__)
# 停止原因只允许这四种；rounds[].reason 另有一个 continued，表示缺口变小、进入下一轮。
STOP_REASONS = ('round_budget_reached', 'no_progress', 'all_targets_met', 'cancelled')
LIMITATIONS = ['继续的唯一条件是 after.missing 严格小于 before.missing；采集调用自己报的成功不算数。',
               '目录发现只在第一轮执行，后续轮次不再请求目录接口，因此运行期间新增的对象不会被发现。',
               '手机对象与酒店浏览器对象不在自动续采范围，缺口只报不采，会直接表现为 no_progress。',
               '清单内已知目标缺口清零不等于平台全量：目标数只来自目录报告或接口回显，分页与对象预算决定已发现对象数。',
               'complete 固定为 false：本命令只回答「清单内的目标还差多少」，不代表平台数据已采全。']


def targets_met(summary):
    # /* ===== 步骤1：判断已知目标是否全达成；数据源：对账 summary；操作：1) 至少一个已知目标 2) 缺口为零 ===== */
    # 1.1 目标全未知时不能算达成：没有可比较的总数，也就没有证据说已经采完。
    known = summary.get('object_count', 0) - summary.get('unknown_target', 0)
    return known > 0 and summary.get('missing') == 0


def drive_rounds(folder, rounds, discovery_pages, max_objects, limit, gap_limit,
                 *, discover=None, collector=None, stop=None, emit=None):
    # /* ===== 步骤2：按轮驱动；数据源：只读对账与既有续采入口；操作：1) 只在首轮发现 2) 每轮对账-续采-对账 3) 缺口不降就停 ===== */
    logger.info('开始无人值守驱动循环')
    if rounds < 1 or discovery_pages < 1 or max_objects < 1 or limit < 1 or gap_limit < 1:
        raise ValueError('轮数、目录页数、目录对象数、目标条数和每轮对象上限必须大于零')
    if collector is None:
        raise ValueError('驱动循环需要采集入口')
    stop, emit = stop or Event(), emit or logger.info
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    report = {'reason': 'cancelled', 'rounds': [], 'rounds_used': 0, 'discovery': None,
              'output': str(folder), 'complete': False,
              'round_budget': rounds, 'discovery_pages': discovery_pages, 'max_objects': max_objects,
              'limit': limit, 'gap_limit': gap_limit, 'limitations': LIMITATIONS}
    for number in range(1, rounds + 1):
        if stop.is_set():
            report['reason'] = 'cancelled'
            break
        emit(f'第 {number}/{rounds} 轮')
        # 2.1 目录接口只在第一轮打一次；后续轮次只续采已落盘的对象清单。
        if number == 1 and discover is not None:
            found = discover(folder, discovery_pages, max_objects)
            report['discovery'] = {key: value for key, value in found.items() if key != 'objects'}
            report['discovery']['object_count'] = len(found.get('objects') or [])
            emit(f"目录发现结束：{found.get('reason')}，发现 {report['discovery']['object_count']} 个对象")
        # 2.2 续采内部先对账再采、采完重新对账；这里的 before / after 就是两次审计的 summary。
        gap = collect_gaps(folder, limit, gap_limit, stop=stop, emit=emit, collector=collector)
        before, after = gap['before'], gap['after']
        progress = after['missing'] < before['missing']
        # 2.3 停止原因只能从四种里挑一个：四个判断都不成立才进入下一轮。
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
                                 'collected_delta': after['collected'] - before['collected'],
                                 'missing_delta': before['missing'] - after['missing'],
                                 'collect_reason': gap['reason'], 'selected': gap['selected'],
                                 'processed': gap['processed'], 'reason': reason})
        report['rounds_used'] = number
        emit(f"第 {number} 轮：missing {before['missing']} → {after['missing']}，{reason}")
        if reason != 'continued':
            report['reason'] = reason
            break
    logger.info('驱动循环结束：%s，%s 轮', report['reason'], report['rounds_used'])
    return report