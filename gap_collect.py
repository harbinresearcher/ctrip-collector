"""缺口驱动续采：读只读对账结果，只对仍缺数据的对象把缺口再采小一点。

不另写下载逻辑：每个对象复用 batch.collect_batch 和调用方给的对象采集入口，
本模块只负责挑对象、传目标条数、以及采完后重新对账。
酒店走专用浏览器且需要人工登录，不放进自动续采，缺口对象仍用 --url 采集。
"""
import logging
from pathlib import Path
from threading import Event

from batch import collect_batch
from coverage_audit import write_coverage_audit

logger = logging.getLogger(__name__)
# 只驱动网页公开点评通道；手机对象与酒店浏览器对象都不在自动续采范围内。
NETWORK_KINDS = ('sight', 'restaurant', 'shopping')


def is_collectable(obj):
    # /* ===== 步骤1：判断对象能否续采；数据源：对账输出；操作：1) 只认已知目标的缺口 2) 必须有网络对象链接 ===== */
    # 1.1 手机对象键不由网页通道续采；未知目标没有可比较的总数，也不能靠续采把缺口算清。
    key = obj.get('object_key')
    return (isinstance(key, str) and not key.startswith('mobile:') and obj.get('kind') in NETWORK_KINDS
            and obj.get('target_unknown') is False and type(obj.get('missing')) is int and obj['missing'] > 0
            and isinstance(obj.get('url'), str) and obj['url'].strip() != '')


def gap_candidates(objects):
    # 1.2 缺口大的先采；同缺口按对象键排序，保证同一目录每次输出顺序稳定。
    return sorted((obj for obj in objects if is_collectable(obj)),
                  key=lambda obj: (-obj['missing'], obj['object_key']))


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


def collect_gaps(folder, limit, gap_limit=10, stop=None, emit=None, collector=None):
    # /* ===== 步骤3：按缺口续采；数据源：只读对账与既有采集入口；操作：1) 排序限量 2) 逐对象采 3) 重新对账 ===== */
    logger.info('开始缺口续采')
    if limit < 1 or gap_limit < 1:
        raise ValueError('目标条数和对象上限必须大于零')
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
        return {'reason': reason, 'limit': limit, 'gap_limit': gap_limit,
                'before': before['summary'], 'after': before['summary'],
                'selected': [], 'processed': [], 'output': str(folder)}
    emit(f"缺口续采：{len(selected)} 个对象（候选 {len(candidates)} 个）")
    runs = []
    for obj in selected:
        if stop.is_set():
            break
        emit(f"续采 {obj['object_key']}：缺口 {obj['missing']}，本次目标 {limit} 条")
        # 3.3 单对象走既有批任务链路：停止原因、错误和导出错误都由它原样记录，这里不改写成成功。
        batch = collect_batch([obj['url']], folder, limit, collector, stop, emit)
        results = batch.get('results') or []
        if results:
            result = results[0]
        elif batch.get('reused_results'):
            result = dict(batch['reused_results'][0], reason='reused_verified')
        else:
            result = {'reason': 'no_result', 'error': batch.get('error') or batch.get('reason') or ''}
        runs.append((obj, result))
    # 3.4 采完重新对账：after 是数据库与证据的真实计数，不是对本次调用的推测。
    after = write_coverage_audit(folder)
    by_key = {row['object_key']: row for row in after['objects']}
    processed = []
    for obj, result in runs:
        row = by_key.get(obj['object_key'], {})
        processed.append({'object_key': obj['object_key'], 'name': obj.get('name'), 'url': obj['url'],
                          'before_missing': obj['missing'],
                          'after_collected': row.get('collected'), 'after_missing': row.get('missing'),
                          'reason': result.get('reason') or 'unknown',
                          'error': result.get('error') or '',
                          'export_error': result.get('export_error') or ''})
    report = {'reason': 'cancelled' if stop.is_set() else 'gap_processed',
              'limit': limit, 'gap_limit': gap_limit,
              'before': before['summary'], 'after': after['summary'],
              'selected': [obj['object_key'] for obj in selected], 'processed': processed,
              'output': str(folder)}
    logger.info('缺口续采结束：处理 %s 个对象', len(processed))
    return report