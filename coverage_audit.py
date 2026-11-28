"""覆盖台账：只用已落盘证据对账「平台多少 / 采到多少 / 差在哪类」。

不访问网络、不启动浏览器、不连接手机。与 coverage_report 分工：
coverage_report 统计本地字段，本模块只读地汇总目标数、已采数与缺口原因，
两边输出字段互不修改。每次对账把 summary 追加一行到 audit-history.jsonl，
用来看覆盖曲线和发现「越采越少」；历史读写失败只记警告，不影响对账结果。
"""
import json
import logging
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from android_coverage import local_file
from mobile_scoped_store import MOBILE_REVIEW_RUN_ID

logger = logging.getLogger(__name__)
KINDS = ('hotel', 'sight', 'restaurant', 'shopping')
# 只接受与采集模块一致的规范对象键；其他键只计数，不参与对账。
OBJECT_KEY = re.compile(r'(?:hotel|sight|restaurant|shopping)-[1-9][0-9]{0,19}')
OBJECT_FILE = re.compile(r'((?:hotel|sight|restaurant|shopping)-[1-9][0-9]{0,19})(?:-public|-app-capture)?-object\.json')
NETWORK_REPORT = re.compile(r'((?:hotel|sight|restaurant|shopping)-[1-9][0-9]{0,19})-report-[0-9A-Za-z]+\.json')
MOBILE_REPORT = re.compile(r'report-[0-9A-Za-z-]+\.json')
# 证据读取上限沿用手机报告的 8 MB 边界；超过上限按不可读文件计，不整份载入内存。
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
# 网页目标数证据优先级：详情快照 > 网页运行报告 > 目录卡片点评数。
TARGET_OBJECT, TARGET_NETWORK, TARGET_DIRECTORY = 2, 1, 0
APP_IMPORTED_METHOD = 'app_proxy_capture'
WEB_REVIEW_METHODS = ('public_comment_api', 'public_restaurant_poi_api', 'hotel_browser_response',
                      'hotel_public_html_snapshot', 'shopping_zero_review_snapshot', 'public_shopping_review_overview')
LIMITATIONS = ['只读取已落盘的目录报告、对象快照、运行报告和本地数据库；不访问网络、不启动浏览器、不连接手机。',
               '目录目标来自 listing_total / total；对象目标、缺口和进度汇总按网页证据和网页点评计算。',
               '网页、App 屏幕与离线导入的存储计数仅按各自证据解释。含 App 导入的对象只统计有网页来源的点评为网页已采。',
               'App 本次明确选中全部时可单独对照显示总数与仍保存在本次范围内的点评；对象 ID 来自本地记录。',
               '餐饮目录使用 POI ID，网页点评使用业务 ID；只有对象快照里同时留有两个 ID 时才对账。',
               '手机卡片里的 photo_count_display 是单条点评的图片展示数，不是对象点评总数，因此不作为对象目标。',
               '目录预算、筛选与排序决定已发现对象数，不等于平台全量；整体 complete 仍为 false。',
               '通道到顶不等于采完：只有核对末页或同口径全部展示数才算采完；channel_capped 只表示这条通道翻到上限/末页，缺口仍计入 reachable_missing，需换手机等其它通道。']
# 审计历史：一行一次对账，只追加不覆盖；回读默认最近 20 行用于看缺口曲线。
HISTORY_FILE = 'audit-history.jsonl'
HISTORY_DEFAULT_LIMIT = 20


def count_value(value):
    # /* ===== 步骤1：读取平台计数；数据源：报告字段；操作：1) 只认非负整数 2) 文本计数不当数字 ===== */
    # 1.1 列表卡片常把「12条点评」这类文本写进计数位；无法确认时返回 None，不能用已采数替代。
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,12}', value.strip()):
        return int(value.strip())
    return None


def read_evidence(path):
    # /* ===== 步骤2：读取一份证据；数据源：输出目录内的报告文件；操作：1) 拒绝链接与非普通文件 2) 限制大小 ===== */
    if not local_file(path) or path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise ValueError('无法读取的证据文件')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('证据文件不是对象')
    return data


def latest_report(reports):
    # 最近一次报告按文件修改时间判断：网页报告与手机报告的时间戳格式不同，不能直接比字符串。
    if not reports:
        return None
    return max(reports, key=lambda item: (item['mtime'], item['file']))


def capped_runs(reports, needed=2):
    # /* ===== 步骤2.1：判断通道是否翻到上限；数据源：最近几次报告；操作：1) 只看空页 2) 连续才算数 ===== */
    # 2.1.1 单次空页可能只是临时抖动；连续两次都以 empty_page/queue_exhausted 结束才算这条通道到顶。
    #       到顶不等于采完：没有核对末页或同口径全部展示数，就不能说这个对象已经采尽。
    recent = sorted(reports, key=lambda item: (item['mtime'], item['file']), reverse=True)[:needed]
    if len(recent) < needed:
        return False
    return all(item.get('reason') in {'empty_page', 'queue_exhausted'} for item in recent)


def evidence_url(value):
    # 对象链接只收 https 地址：报告或快照里出现其他协议时不猜，保持 None。
    return value.strip() if isinstance(value, str) and value.strip().startswith('https://') else None


def blocked_today(report):
    # /* ===== 步骤2.2：拦截是否仍在今天；数据源：报告修改时间；操作：1) 换算本地日期 2) 只认当天 ===== */
    # 2.2.1 平台限流的处置是「停今天、明天再试」，所以隔天的被拦报告要放行续采。
    try:
        stamp = datetime.fromtimestamp(report['mtime']).date()
    except (KeyError, TypeError, ValueError, OSError):
        return False
    return stamp == datetime.now().date()


def phone_all_label(data):
    # /* ===== 步骤5.2：手机侧「全部」计数；数据源：手机报告的屏幕观察；操作：1) 只认精确整数 2) 出现多个值就不猜 ===== */
    # 5.2.1 页面不会把页签标成 selected，所以这里只记录「屏幕上看到的全部计数」，
    #       不声称当前筛选就是全部；调用方必须带上来源标注。
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
                value = count_value(entry.get('display_count'))
                if value is not None:
                    totals.add(value)
    return totals.pop() if len(totals) == 1 else None


def app_all_run_evidence(path, data):
    run_id, check = data.get('run_id'), data.get('review_count_check')
    if (not isinstance(run_id, str) or not MOBILE_REVIEW_RUN_ID.fullmatch(run_id)
            or path.name != f'report-{run_id}.json' or not isinstance(check, dict)
            or check.get('scope') != 'current_run_fingerprints_vs_app_filter_label'
            or check.get('state') != 'compared_display_only' or check.get('filter') != '全部'
            or not isinstance(data.get('reason'), str) or not data['reason']):
        return None
    total = count_value(check.get('display_total'))
    if total is None:
        return None
    return {'run_id': run_id, 'kind': data['kind'], 'object_id': str(data['object_id']),
            'place': data['place'].strip(), 'reason': data['reason'], 'display_total': total,
            'file': path.name, 'mtime': path.stat().st_mtime}


def matched_app_run(db, reports):
    for report in sorted(reports, key=lambda row: (row['mtime'], row['file']), reverse=True):
        kind, identity = report['kind'], report['object_id']
        row = db.execute('''SELECT COUNT(review.key)
                            FROM mobile_review_runs run
                            LEFT JOIN mobile_review_run_items item ON item.run_id=run.run_id
                            LEFT JOIN reviews_scoped review
                              ON review.key=item.review_key AND review.kind=run.kind
                             AND review.scope=run.scope AND review.object_id=run.object_id
                            WHERE run.run_id=? AND run.kind=? AND run.object_id=? AND run.scope=?
                              AND run.place=? AND run.final_reason=?
                            GROUP BY run.run_id''',
                         (report['run_id'], kind, identity, f'declared:ctrip:{kind}:{identity}',
                          report['place'], report['reason'])).fetchone()
        if row is not None:
            collected, total = row[0], report['display_total']
            return {'run_id': report['run_id'], 'evidence_file': report['file'], 'display_total': total,
                    'collected': collected, 'missing': max(total - collected, 0),
                    'display_total_matched': total == collected, 'identity_id_verified': False}
    return None


def review_source_counts(db):
    # An ID observed through both methods counts once toward the web target.
    web_methods = ','.join('?' for _ in WEB_REVIEW_METHODS)
    return db.execute(f'''SELECT review.object_key,
                                SUM(EXISTS(SELECT 1 FROM network_review_observations seen
                                           WHERE seen.object_key=review.object_key AND seen.comment_id=review.comment_id
                                             AND seen.source_method IN ({web_methods}))),
                                MAX(EXISTS(SELECT 1 FROM network_review_observations seen
                                           WHERE seen.object_key=review.object_key AND seen.comment_id=review.comment_id
                                             AND seen.source_method=?)),
                                SUM(NOT EXISTS(SELECT 1 FROM network_review_observations seen
                                               WHERE seen.object_key=review.object_key AND seen.comment_id=review.comment_id
                                                 AND seen.source_method<>'historical_unknown'))
                         FROM network_reviews review GROUP BY review.object_key''',
                      (*WEB_REVIEW_METHODS, APP_IMPORTED_METHOD))


COVERAGE_NOTE = '网页与手机没有共同主键，不能相加去重；best_effort 表示每个对象取已采更多的那条通道'


def discovery_summary(directories):
    # /* ===== 步骤5.3.0：对象发现覆盖；数据源：目录报告行；操作：1) 平台目录总数 2) 已发现对象 3) 比值 ===== */
    # 5.3.0.1 平台目录总数可能是截断上限（整千值尤其可疑），不是真实总数；这里按报告原样记录
    #         并给出比值，同时把这点写进说明，不拿它当「全量已发现」的结论。
    # 5.3.0.2 同一城市跑多次发现会留下多份目录报告，只取每个 (类型, 范围) 的最新一份，
    #         否则总数与已发现对象会被重复累加（实测 3000/19 会变成 6000/38）。
    latest = {}
    for row in directories:
        scope = (row.get('kind'), row.get('scope_id'))
        current = latest.get(scope)
        if current is None or (row.get('report') or '') > (current.get('report') or ''):
            latest[scope] = row
    rows = list(latest.values())
    # 5.3.0.3 老报告没有 listing_total_suspect 字段：按「整千值」兜底判断，
    #         否则历史目录会继续用截断值当分母（实测 3000 就这样被当成真实总数）。
    suspect = any(row.get('listing_total_suspect')
                  or (isinstance(row.get('listing_total'), int) and row['listing_total'] > 0
                      and row['listing_total'] % 1000 == 0)
                  for row in rows)
    known = [row for row in rows if row.get('listing_total') is not None and not suspect]
    listing_total = sum(row['listing_total'] for row in known) if known else None
    discovered = sum(row.get('discovered_objects') or 0 for row in rows)
    pages = sum(row.get('pages') or 0 for row in rows)
    # 5.3.0.2 平台总数可疑（整千截断值）时不给分母，也不给比值：
    #         用截断值当分母算出来的「发现率」是假的（实测 19/3000 只是读了 2 页的产物）。
    ratio = None
    if listing_total:
        ratio = round(min(discovered / listing_total, 1.0), 4)
    note = ('listing_total 是平台截断值（整千），不能当分母；已发现 %d 个对象、读了 %d 页'
            % (discovered, pages)) if suspect else \
           'listing_total 按目录报告原样汇总；已发现 %d 个对象、读了 %d 页' % (discovered, pages)
    return {'listing_total': listing_total, 'listing_total_suspect': suspect,
            'discovered': discovered, 'pages_read': pages, 'ratio': ratio, 'note': note}


def coverage_summary(objects):
    # /* ===== 步骤5.3：统一覆盖率总表；数据源：对象行；操作：1) 两条通道分开算 2) 跨通道只取较大值不相加 ===== */
    # 5.3.1 网页口径与 summary 一致；手机没有任何目标时为 null，已采数仍给真实值。
    web_known = [obj for obj in objects if obj['target'] is not None]
    phone_known = [obj for obj in objects if obj['phone_target'] is not None]
    complete = partial = unknown_target = no_phone_data = 0
    covered = best_target = 0
    for obj in objects:
        web_target, phone_target = obj['target'], obj['phone_target']
        # 5.3.2 任一通道达到目标就算完整；两条通道都没有目标时单独计数，不冒充 0。
        if (web_target is not None and obj['collected'] >= web_target) or (
                phone_target is not None and obj['phone_collected'] >= phone_target):
            complete += 1
        elif web_target is not None or phone_target is not None:
            partial += 1
        else:
            unknown_target += 1
        if phone_target is None and obj['phone_collected'] == 0:
            no_phone_data += 1
        # 5.3.3 best_effort 每个对象只取一条通道：已采更多的那条；目标取同一条通道的目标，
        #       该通道没有目标时退回另一条已知目标，仍不把两条已采数相加。
        phone_bigger = obj['phone_collected'] > obj['collected']
        covered += obj['phone_collected'] if phone_bigger else obj['collected']
        chosen_target = phone_target if phone_bigger else web_target
        other_target = web_target if phone_bigger else phone_target
        if chosen_target is not None:
            best_target += chosen_target
        elif other_target is not None:
            best_target += other_target
    # 5.3.4 网页侧一个目标都没有时同样给 null：没有目标不等于目标是 0。
    web_collected = sum(obj['collected'] for obj in objects)
    # 5.3.5 通道到顶的对象已经拿不到更多了：它的「网页可得量」就等于已采量。
    #       于是有两个覆盖率——相对平台总数（可能很低）与相对网页通道能给的量（通常接近满）。
    #       实测：万达长白山国际滑雪场详情页写 357 条，接口翻到第 5 页就空、只给 61 条。
    capped = [obj for obj in objects if obj.get('gap_reason') == 'channel_capped']
    reachable = (sum(obj['target'] for obj in web_known if obj.get('gap_reason') != 'channel_capped')
                 + sum(obj['collected'] for obj in capped))
    return {'web': {'target': sum(obj['target'] for obj in web_known) if web_known else None,
                    'collected': web_collected,
                    'missing': sum(obj['missing'] for obj in web_known) if web_known else None,
                    'reachable': {'target': reachable if web_known else None,
                                  'collected': web_collected,
                                  'ratio': round(min(web_collected / reachable, 1.0), 4) if reachable else None,
                                  'capped_objects': len(capped),
                                  'note': '通道到顶的对象按已采量计入可得量；ratio 表示网页通道能给的量里我们采到了多少'}},
            'phone': {'target': sum(obj['phone_target'] for obj in phone_known) if phone_known else None,
                      'collected': sum(obj['phone_collected'] for obj in objects),
                      'missing': sum(obj['phone_missing'] for obj in phone_known) if phone_known else None},
            'objects': {'total': len(objects), 'complete': complete, 'partial': partial,
                        'unknown_target': unknown_target, 'no_phone_data': no_phone_data},
            'best_effort': {'covered': covered, 'target': best_target,
                            'ratio': covered / best_target if best_target else None},
            'note': COVERAGE_NOTE}


def write_coverage_audit(folder):
    # /* ===== 步骤3：读取对象级与目录级证据；数据源：输出目录；操作：1) 只读打开 2) 记录目标数来源 3) 记录对象链接 ===== */
    logger.info('开始对账已落盘采集证据')
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise ValueError('输出目录不存在，无法对账')
    evidence = {}
    directories = []
    invalid_files = 0
    invalid_rows = 0
    poi_keys = {}

    def record(key, kind):
        return evidence.setdefault(key, {'key': key, 'kind': kind, 'name': None, 'url': None, 'target': None,
                                         'target_rank': -1, 'target_source': None, 'reports': [], 'files': [],
                                         'web_attempted': False, 'app_import_seen': False, 'app_runs': []})

    def offer_url(item, value):
        # 3.0 链接只用于后续定位对象；对象快照优先，已有时不覆盖。
        if item['url'] is None:
            item['url'] = evidence_url(value)

    def offer_target(item, rank, value, source):
        # 3.1 同级证据按文件排序后写回：同一对象有多次采集时以较晚的报告为准。
        if value is None or rank < item['target_rank']:
            return
        item.update(target=value, target_rank=rank, target_source=source)

    def add_report(item, path, data):
        item['reports'].append({'file': path.name, 'reason': data.get('reason'),
                                'error': data.get('error') if isinstance(data.get('error'), str) else '',
                                'blocked': data.get('blocked') is True,
                                'blocked_status': data.get('blocked_status')
                                if isinstance(data.get('blocked_status'), str) else '',
                                'mtime': path.stat().st_mtime})

    # 3.2 对象快照是详情页/接口回显的落盘证据，优先级最高。
    for path in sorted(folder.glob('*-object.json')):
        match = OBJECT_FILE.fullmatch(path.name)
        if not match:
            continue
        try:
            data = read_evidence(path)
            kind = data.get('kind')
            if kind not in KINDS or match[1] != f"{kind}-{str(data.get('resource_id'))}":
                raise ValueError('对象快照身份不一致')
        except (OSError, ValueError, TypeError):
            invalid_files += 1
            continue
        item = record(match[1], kind)
        item['files'].append(path.name)
        if not item['name'] and isinstance(data.get('name'), str) and data['name'].strip():
            item['name'] = data['name'].strip()
        if path.name.endswith('-app-capture-object.json') or data.get('source_method') == APP_IMPORTED_METHOD:
            item['app_import_seen'] = True
        elif data.get('source_method') is None or data.get('source_method') in WEB_REVIEW_METHODS:
            item['web_attempted'] = True
            offer_url(item, data.get('url'))
            offer_target(item, TARGET_OBJECT, count_value(data.get('display_total')), path.name + ':display_total')
        # 3.3 餐饮快照同时留有两个身份，可把目录里的 POI ID 映射到点评业务 ID。
        if kind == 'restaurant' and type(data.get('poi_id')) is int and data['poi_id'] > 0:
            poi_keys[data['poi_id']] = match[1]

    # 3.4 网页运行报告保留停止原因与错误；display_total 是接口回显的平台总数。
    for path in sorted(folder.glob('*-report-*.json')):
        match = NETWORK_REPORT.fullmatch(path.name)
        if not match:
            continue
        try:
            data = read_evidence(path)
            if data.get('object_key') != match[1]:
                raise ValueError('报告与对象键不一致')
        except (OSError, ValueError, TypeError):
            invalid_files += 1
            continue
        kind = match[1].split('-', 1)[0]
        item = record(match[1], kind)
        item['files'].append(path.name)
        if not item['name'] and isinstance(data.get('name'), str) and data['name'].strip():
            item['name'] = data['name'].strip()
        if data.get('source_method') == APP_IMPORTED_METHOD:
            item['app_import_seen'] = True
        elif data.get('source_method') is None or data.get('source_method') in WEB_REVIEW_METHODS:
            item['web_attempted'] = True
            add_report(item, path, data)
            offer_url(item, data.get('source_url'))
            offer_target(item, TARGET_NETWORK, count_value(data.get('display_total')), path.name + ':display_total')

    # 3.5 App 只比较本次全部筛选和已落库的本次成员，独立于网页目标。
    for path in sorted(folder.glob('report-*.json')):
        if not MOBILE_REPORT.fullmatch(path.name):
            continue
        try:
            data = read_evidence(path)
            kind, place = data.get('kind'), data.get('place')
            if kind not in KINDS or not isinstance(place, str) or not place.strip():
                raise ValueError('手机报告缺少对象身份')
            if data.get('object_id') is None:
                key = f'mobile:{kind}:{place.strip()}'
            else:
                identity = str(data['object_id'])
                if not re.fullmatch(r'[1-9][0-9]{0,19}', identity):
                    raise ValueError('手机报告对象 ID 无效')
                key = f'{kind}-{identity}'
        except (OSError, ValueError, TypeError, KeyError):
            invalid_files += 1
            continue
        item = record(key, kind)
        item['files'].append(path.name)
        if not item['name']:
            item['name'] = place.strip()
        if not key.startswith('mobile:'):
            if app_run := app_all_run_evidence(path, data):
                item['app_runs'].append(app_run)
            # 3.5.1 页面不会标注当前筛选，所以这里只记「屏幕上看到的全部计数」，
            #       取最新一份报告，多个值不一致就不采用。
            if (label_total := phone_all_label(data)) is not None:
                item['phone_label'] = {'display_total': label_total, 'file': path.name,
                                       'mtime': path.stat().st_mtime}
            # 3.5.2 手机侧「没认出点评页」也要留下一笔：这类报告没有数据，
            #       但它是可数的布局缺口，对账要能统计「几个对象因为页面没认出来而没采到」。
            if data.get('reason') == 'arrival_failed':
                previous = item.get('phone_arrival')
                if previous is None or path.stat().st_mtime >= previous['mtime']:
                    item['phone_arrival'] = {'reason': data.get('reason'),
                                             'arrival_reason': data.get('arrival_reason'),
                                             'file': path.name, 'mtime': path.stat().st_mtime}

    # 3.6 目录报告给出平台总数与已发现对象；目录卡片的点评数可作对象级目标。
    for path in sorted(folder.glob('discovery-*.json')):
        try:
            data = read_evidence(path)
            items = data.get('objects')
            if not isinstance(items, list):
                raise ValueError('目录报告没有对象清单')
        except (OSError, ValueError, TypeError):
            invalid_files += 1
            continue
        if path.name.startswith('discovery-regions-'):
            kind = 'regions'
        else:
            kind = data.get('kind') if data.get('kind') in KINDS else next(
                (candidate for candidate in KINDS if path.name.startswith(f'discovery-{candidate}-')), None)
            if kind is None:
                invalid_files += 1
                continue
        listing_total, total_source = None, None
        for field in ('listing_total', 'total'):
            value = count_value(data.get(field))
            if value is not None:
                listing_total, total_source = value, field
                break
        directories.append({'report': path.name, 'kind': kind,
                            'scope_id': data.get('district_id') if type(data.get('district_id')) is int else
                            data.get('city_id') if type(data.get('city_id')) is int else None,
                            'listing_total': listing_total, 'listing_total_source': total_source,
                            'discovered_objects': len(items),
                            # 3.6.1 目录读了几页、看到多少卡片、总数是否可疑，都随行带下去，
                            #       否则覆盖率总表只能拿到一个数字、判断不了分母可不可信。
                            'pages': count_value(data.get('pages')),
                            'cards_seen': count_value(data.get('cards_seen')),
                            'own_cards': count_value(data.get('own_cards')),
                            'listing_total_suspect': data.get('listing_total_suspect') is True,
                            'regions': len(data['regions']) if isinstance(data.get('regions'), list) else None,
                            'reason': data.get('reason'),
                            'error': data.get('error') if isinstance(data.get('error'), str) else '',
                            'captured_at': data.get('captured_at') if isinstance(data.get('captured_at'), str) else None,
                            'target_unknown': listing_total is None, 'complete': False})
        # 3.7 地区汇总报告的对象来自各地区报告，已单独计入，不能重复统计。
        if kind == 'regions':
            continue
        for row in items:
            if not isinstance(row, dict):
                invalid_rows += 1
                continue
            item_kind = row.get('kind') if row.get('kind') in KINDS else kind
            key = None
            if type(row.get('resource_id')) is int and row['resource_id'] > 0 and item_kind in ('hotel', 'sight', 'shopping'):
                key = f"{item_kind}-{row['resource_id']}"
            elif item_kind == 'restaurant' and type(row.get('poi_id')) is int:
                key = poi_keys.get(row['poi_id'])
            if key is None:
                continue
            item = record(key, item_kind)
            if not item['name'] and isinstance(row.get('name'), str) and row['name'].strip():
                item['name'] = row['name'].strip()
            # 3.8 目录卡片链接是无对象快照时唯一的定位证据；未采集对象也能据此续采。
            offer_url(item, row.get('url'))
            offer_target(item, TARGET_DIRECTORY, count_value(row.get('comment_count')),
                         f"{path.name}:objects[{key}].comment_count")
    logger.info('对象级证据读取完成：%s个对象，%s个目录', len(evidence), len(directories))

    # /* ===== 步骤4：从本地数据库取已采数；数据源：只读连接；操作：1) 按对象分组 2) 坏键单独计数 ===== */
    logger.info('开始统计本地数据库已采数')
    collected = {}

    def add_count(key, store, amount):
        counts = collected.setdefault(key, {})
        counts[store] = counts.get(store, 0) + amount

    network_state = 'missing'
    web_collected = {}
    network_db = folder / 'network.sqlite3'
    if local_file(network_db):
        try:
            with closing(sqlite3.connect(network_db.as_uri() + '?mode=ro', uri=True)) as db:
                db.execute('BEGIN')
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'network_reviews' in tables:
                    for key, amount in db.execute('SELECT object_key,COUNT(*) FROM network_reviews GROUP BY object_key'):
                        if isinstance(key, str) and OBJECT_KEY.fullmatch(key):
                            add_count(key, 'network_reviews', amount)
                        else:
                            invalid_rows += amount
                    if 'network_review_observations' in tables:
                        for key, web, imported, legacy in review_source_counts(db):
                            if key in collected:
                                has_import = imported or evidence.get(key, {}).get('app_import_seen', False)
                                web_collected[key] = web if has_import else web + legacy
                    else:
                        # Older databases without source observations can only use unattributed web rows
                        # for objects with no evidence of an App import.
                        web_collected = {key: counts['network_reviews'] for key, counts in collected.items()
                                         if not evidence.get(key, {}).get('app_import_seen')}
                    network_state = 'read'
                else:
                    network_state = 'no_table'
        except sqlite3.Error:
            network_state = 'unreadable'
    mobile_state = 'missing'
    app_comparisons = {}
    mobile_db = folder / 'reviews.sqlite3'
    if local_file(mobile_db):
        try:
            with closing(sqlite3.connect(mobile_db.as_uri() + '?mode=ro', uri=True)) as db:
                db.execute('BEGIN')
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                read = False
                if 'reviews_scoped' in tables:
                    for kind, identity, amount in db.execute(
                            'SELECT kind,object_id,COUNT(*) FROM reviews_scoped GROUP BY kind,object_id'):
                        if kind in KINDS and re.fullmatch(r'[1-9][0-9]{0,19}', str(identity)):
                            add_count(f'{kind}-{identity}', 'reviews_scoped', amount)
                        else:
                            invalid_rows += amount
                    read = True
                if 'reviews' in tables:
                    # 4.1 旧表没有对象 ID，只用操作者声明的类型与名称作键，目标数仍是未知。
                    for kind, place, amount in db.execute('SELECT kind,place,COUNT(*) FROM reviews GROUP BY kind,place'):
                        if kind in KINDS and isinstance(place, str) and place.strip():
                            add_count(f'mobile:{kind}:{place.strip()}', 'reviews', amount)
                        else:
                            invalid_rows += amount
                    read = True
                if {'reviews_scoped', 'mobile_review_runs', 'mobile_review_run_items'} <= tables:
                    for key, item in evidence.items():
                        if item['app_runs']:
                            app_comparisons[key] = matched_app_run(db, item['app_runs'])
                mobile_state = 'read' if read else 'no_table'
        except sqlite3.Error:
            mobile_state = 'unreadable'
    for key in collected:
        record(key, key.split(':', 2)[1] if key.startswith('mobile:') else key.split('-', 1)[0])
    logger.info('本地数据库已采数统计完成')

    # /* ===== 步骤5：计算缺口；数据源：目标数与已采数；操作：1) 分类原因 2) 未知目标不算完整 ===== */
    logger.info('开始计算覆盖缺口')
    objects = []
    for key in sorted(evidence):
        item = evidence[key]
        counts = collected.get(key, {})
        total = web_collected.get(key, 0)
        target = item['target']
        missing = max(target - total, 0) if target is not None else None
        report = latest_report(item['reports'])
        # 5.0.1 被平台拦住与「没数据」分开：最新报告标了 blocked 就不算可续采缺口，
        #       也不能降级成普通 failed，否则 --drive 会把它当成还能继续采的对象。
        # 5.0.2 但拦截只在**当天**有效：纪律是「停今天、明天再试」，所以昨天的被拦报告
        #       不再挡住续采，否则一次偶发 429 会让这个对象永久退出队列。
        blocked = bool(report and report.get('blocked') and blocked_today(report))
        failed = bool(report and (report['reason'] == 'error' or report['error']))
        # 5.0.3 「没认出点评页」单独成一类：它不是网络失败，是布局/入口问题，
        #       对账要能数出「有几个对象是因为页面没认出来才没采到」。
        arrival_failed = bool(report and report.get('reason') == 'arrival_failed')
        attempted = item['web_attempted'] or total > 0
        # 5.1 连续两次都「翻到空页」且没采到目标数：这条通道翻到上限了，但拿不到同口径的
        #     全部展示数，不能说对象采完（真机反证：长白山西景区网页 577 条、手机「全部」1099 条）。
        #     缺口标 channel_capped 让 --drive 不再空转，同时保留真实缺口并提示换通道。
        capped_channel = bool(missing and not failed and capped_runs(item['reports']))
        if missing == 0:
            gap = None
        elif blocked:
            gap = 'blocked'
        elif arrival_failed:
            gap = 'arrival_failed'
        elif failed:
            gap = 'failed'
        elif capped_channel:
            gap = 'channel_capped'
        elif attempted:
            gap = 'missing'
        else:
            gap = 'unadapted'
        # 5.1.1 capped 只描述最终缺口原因：被拦或失败的对象即使也连续空页，也不说成「通道到顶」。
        capped = gap == 'channel_capped'
        # 5.2.2 手机侧目标：优先用已核对「全部」筛选的本次运行，其次用屏幕上看到的「全部」计数。
        app_run = app_comparisons.get(key)
        phone_collected = counts.get('reviews_scoped', 0)
        label = item.get('phone_label') or {}
        if app_run:
            phone_target, phone_target_source = app_run['display_total'], 'confirmed_run'
        elif label:
            phone_target, phone_target_source = label['display_total'], 'all_label'
        else:
            phone_target, phone_target_source = None, None
        phone_missing = max(phone_target - phone_collected, 0) if phone_target is not None else None
        # 5.2.3 两条通道对「这个对象有多少点评」说法不同时明确标出来。
        #       实测：讷殷古城网页 display_total=420、手机「全部」=450——差 30 条。
        target_mismatch = None
        if target is not None and phone_target is not None and target != phone_target:
            target_mismatch = {'web': target, 'phone': phone_target, 'delta': phone_target - target}
        objects.append({'object_key': key, 'kind': item['kind'], 'name': item['name'], 'url': item['url'],
                        'target': target, 'target_source': item['target_source'], 'target_unknown': target is None,
                        'collected': total,
                        'collected_by_source': {store: counts.get(store, 0)
                                                for store in ('network_reviews', 'reviews_scoped', 'reviews')},
                        'app_all_run': app_comparisons.get(key),
                        'phone_collected': counts.get('reviews_scoped', 0),
                        'phone_target': phone_target,
                        'phone_target_source': phone_target_source,
                        'phone_missing': phone_missing,
                        'target_mismatch': target_mismatch,
                        'missing': missing, 'gap_reason': gap,
                        'capped': capped,
                        # 5.2.3 到顶只说明这一条通道，不代表对象没数据：提醒去手机等其它通道继续找。
                        'needs_other_channel': capped,
                        'blocked': blocked,
                        'blocked_status': report.get('blocked_status') if report else '',
                        # 5.2.4 可达缺口只在 missing == 0（确实采完）时才为 0；通道到顶仍保留真实缺口。
                        'reachable_missing': missing,
                        # 5.2.5 未知布局：最新一次手机采集没认出点评页时，记下缺哪一条判据，
                        #       让「补样本 / 换入口」变成可数、可排期的缺口。
                        'layout_unknown': arrival_failed,
                        'arrival_reason': (report or {}).get('arrival_reason') if arrival_failed else None,
                        # 5.2.6 手机上「没认出点评页」的那次尝试：与网页缺口分开记，
                        #       否则一个从来没被认出来的对象看起来和「还没轮到」一模一样。
                        'phone_layout_unknown': bool(item.get('phone_arrival')),
                        'phone_arrival_reason': (item.get('phone_arrival') or {}).get('arrival_reason'),
                        'latest_reason': report['reason'] if report else None,
                        'latest_error': report['error'] if report else '',
                        'evidence_files': sorted(item['files']),
                        'complete': target is not None and missing == 0})
    known = [obj for obj in objects if obj['target'] is not None]
    summary = {'target': sum(obj['target'] for obj in known),
               'collected': sum(obj['collected'] for obj in objects),
               'missing': sum(obj['missing'] for obj in known),
               'reachable_missing': sum(obj['reachable_missing'] for obj in known),
               'capped_count': sum(1 for obj in objects if obj['capped']),
               'layout_unknown': sum(1 for obj in objects if obj['layout_unknown']),
               'phone_layout_unknown': sum(1 for obj in objects if obj['phone_layout_unknown']),
               'unknown_target': sum(1 for obj in objects if obj['target_unknown']),
               'object_count': len(objects), 'directory_count': len(directories),
               'coverage': dict(coverage_summary(objects), discovery=discovery_summary(directories)),
               'complete': False}
    report_path = folder / ('audit-' + uuid.uuid4().hex + '.json')
    report = {'scope': 'landed_evidence_only', 'review_totals_scope': 'web_reviews', 'complete': False,
              'objects': objects, 'directories': sorted(directories, key=lambda row: row['report']),
              'summary': summary, 'network_database': network_state, 'mobile_database': mobile_state,
              'invalid_evidence_files': invalid_files, 'invalid_rows': invalid_rows,
              'limitations': LIMITATIONS, 'report_file': str(report_path), 'output': str(folder)}
    # 5.1 历史是旁路：追加当前这一行再回读曲线；写入失败只记警告，对账结果照常输出。
    append_audit_history(folder, report)
    report['history'] = read_audit_history(folder)
    # 5.2 原子写出：审计报告本身不是采集证据，写入失败不能覆盖上一次的完整报告。
    temporary = report_path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(report_path)
    finally:
        temporary.unlink(missing_ok=True)
    logger.info('覆盖对账完成：%s个对象，%s个未知目标', len(objects), summary['unknown_target'])
    return report


def utc_now():
    # 历史时间统一用 UTC ISO（秒级、以 Z 结尾），跨机器比对时不用猜时区。
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def audit_history_row(report, captured_at):
    # /* ===== 步骤6：把一次对账压成一行历史；数据源：对账报告；操作：1) 取 summary 计数 2) 按 gap_reason 统计缺口原因 ===== */
    # 6.1 只落对账已有的数字；summary 缺字段时留 null，不用已采数或其他值填补。
    summary = report.get('summary') if isinstance(report.get('summary'), dict) else {}
    objects = report.get('objects') if isinstance(report.get('objects'), list) else []
    counts = {'failed': 0, 'unadapted': 0, 'channel_capped': 0}
    for obj in objects:
        if isinstance(obj, dict) and obj.get('gap_reason') in counts:
            counts[obj['gap_reason']] += 1
    return {'captured_at': captured_at,
            'review_totals_scope': report.get('review_totals_scope'),
            'missing': summary.get('missing'), 'reachable_missing': summary.get('reachable_missing'),
            'target': summary.get('target'), 'collected': summary.get('collected'),
            'object_count': summary.get('object_count'), 'unknown_target': summary.get('unknown_target'),
            'failed_count': counts['failed'], 'unadapted_count': counts['unadapted'],
            'capped_count': counts['channel_capped']}


def append_audit_history(folder, report):
    # /* ===== 步骤6.2：追加一行历史；数据源：本次对账；操作：1) 只追加不覆盖 2) 写失败只警告并返回 None ===== */
    row = audit_history_row(report, utc_now())
    try:
        with (Path(folder) / HISTORY_FILE).open('a', encoding='utf-8', newline='\n') as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    except (OSError, TypeError, ValueError) as exc:
        logger.warning('审计历史写入失败，本次对账结果仍照常输出：%s', exc)
        return None
    return row


def read_audit_history(folder, limit=HISTORY_DEFAULT_LIMIT):
    # /* ===== 步骤6.3：只读汇总历史；数据源：audit-history.jsonl；操作：1) 跳过坏行并计数 2) 算 missing 差值 3) 变大时告警 ===== */
    path = Path(folder) / HISTORY_FILE
    lines = []
    if path.is_file():
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except OSError as exc:
            logger.warning('审计历史读取失败，按无历史处理：%s', exc)
    rows, bad_rows = [], 0
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            bad_rows += 1
            continue
        # 6.4 历史是外部文件，可能被手工改坏：非对象行跳过并计数，不中断汇总。
        if not isinstance(row, dict):
            bad_rows += 1
            continue
        # 6.4.1 旧行只有 source_exhausted_count：字段改名后按同一含义继续读，旧历史不因缺新字段报错。
        if 'capped_count' not in row and 'source_exhausted_count' in row:
            row['capped_count'] = row['source_exhausted_count']
        rows.append(row)
    if limit > 0:
        rows = rows[-limit:]
    # 6.5 只有同一统计范围内的整数缺口才适合比较。
    trend = None
    if len(rows) >= 2 and rows[-2].get('review_totals_scope') == rows[-1].get('review_totals_scope'):
        previous, latest = rows[-2].get('missing'), rows[-1].get('missing')
        if type(previous) is int and type(latest) is int:
            trend = latest - previous
    warning = None
    if trend is not None and trend > 0:
        warning = f'缺口比上次多了 {trend} 条（missing {rows[-2]["missing"]} → {rows[-1]["missing"]}），先查 failed / unadapted 是否上升'
    return {'file': str(path), 'rows': rows, 'trend': trend, 'warning': warning, 'bad_rows': bad_rows}
