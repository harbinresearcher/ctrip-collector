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

logger = logging.getLogger(__name__)
KINDS = ('hotel', 'sight', 'restaurant', 'shopping')
# 只接受与采集模块一致的规范对象键；其他键只计数，不参与对账。
OBJECT_KEY = re.compile(r'(?:hotel|sight|restaurant|shopping)-[1-9][0-9]{0,19}')
OBJECT_FILE = re.compile(r'((?:hotel|sight|restaurant|shopping)-[1-9][0-9]{0,19})(?:-public|-app-capture)?-object\.json')
NETWORK_REPORT = re.compile(r'((?:hotel|sight|restaurant|shopping)-[1-9][0-9]{0,19})-report-[0-9A-Za-z]+\.json')
MOBILE_REPORT = re.compile(r'report-[0-9A-Za-z-]+\.json')
# 证据读取上限沿用手机报告的 8 MB 边界；超过上限按不可读文件计，不整份载入内存。
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
# 目标数证据优先级：详情快照 > 网页运行报告 > 手机运行报告 > 目录卡片点评数。
TARGET_OBJECT, TARGET_NETWORK, TARGET_MOBILE, TARGET_DIRECTORY = 3, 2, 1, 0
LIMITATIONS = ['只读取已落盘的目录报告、对象快照、运行报告和本地数据库；不访问网络、不启动浏览器、不连接手机。',
               '目标数只取报告回显的 listing_total / total / display_total 或目录卡片点评数；取不到时标记 target_unknown，不用已采数代替。',
               '网页评论库与手机评论库的条数相加时可能包含同一条点评，两个通道的重叠未核验。',
               '餐饮目录使用 POI ID，网页点评使用业务 ID；只有对象快照里同时留有两个 ID 时才对账。',
               '手机卡片里的 photo_count_display 是单条点评的图片展示数，不是对象点评总数，因此不作为对象目标。',
               '目录预算、筛选与排序决定已发现对象数，不等于平台全量；整体 complete 仍为 false。']
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


def exhausted_runs(reports, needed=2):
    # /* ===== 步骤2.1：判断通道是否采不出来；数据源：最近几次报告；操作：1) 只看空页 2) 连续才算数 ===== */
    # 2.1.1 单次空页可能只是临时抖动；连续两次都以 empty_page/queue_exhausted 结束才算采尽。
    recent = sorted(reports, key=lambda item: (item['mtime'], item['file']), reverse=True)[:needed]
    if len(recent) < needed:
        return False
    return all(item.get('reason') in {'empty_page', 'queue_exhausted'} for item in recent)


def evidence_url(value):
    # 对象链接只收 https 地址：报告或快照里出现其他协议时不猜，保持 None。
    return value.strip() if isinstance(value, str) and value.strip().startswith('https://') else None


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
                                         'target_rank': -1, 'target_source': None, 'reports': [], 'files': []})

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
        item['files'].append(path.name)
        item['reports'].append({'file': path.name, 'reason': data.get('reason'),
                                'error': data.get('error') if isinstance(data.get('error'), str) else '',
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
        add_report(item, path, data)
        if not item['name'] and isinstance(data.get('name'), str) and data['name'].strip():
            item['name'] = data['name'].strip()
        offer_url(item, data.get('source_url'))
        offer_target(item, TARGET_NETWORK, count_value(data.get('display_total')), path.name + ':display_total')

    # 3.5 手机运行报告的 target 是本次采集上限，不是平台总数；只认页面回显的 display_total。
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
        add_report(item, path, data)
        if not item['name']:
            item['name'] = place.strip()
        check = data.get('review_count_check')
        offer_target(item, TARGET_MOBILE, count_value(check.get('display_total')) if isinstance(check, dict) else None,
                     path.name + ':review_count_check.display_total')

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
    network_db = folder / 'network.sqlite3'
    if local_file(network_db):
        try:
            with closing(sqlite3.connect(network_db.as_uri() + '?mode=ro', uri=True)) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'network_reviews' in tables:
                    for key, amount in db.execute('SELECT object_key,COUNT(*) FROM network_reviews GROUP BY object_key'):
                        if isinstance(key, str) and OBJECT_KEY.fullmatch(key):
                            add_count(key, 'network_reviews', amount)
                        else:
                            invalid_rows += amount
                    network_state = 'read'
                else:
                    network_state = 'no_table'
        except sqlite3.Error:
            network_state = 'unreadable'
    mobile_state = 'missing'
    mobile_db = folder / 'reviews.sqlite3'
    if local_file(mobile_db):
        try:
            with closing(sqlite3.connect(mobile_db.as_uri() + '?mode=ro', uri=True)) as db:
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
        total = sum(counts.get(store, 0) for store in ('network_reviews', 'reviews_scoped', 'reviews'))
        target = item['target']
        missing = max(target - total, 0) if target is not None else None
        report = latest_report(item['reports'])
        failed = bool(report and (report['reason'] == 'error' or report['error']))
        attempted = bool(item['files']) or total > 0
        # 5.1 连续两次都「翻到空页」且没采到目标数：这条通道已经采不出来，
        #     缺口要标成 source_exhausted，否则 --drive 会一直空转报 no_progress。
        exhausted = bool(missing and not failed and exhausted_runs(item['reports']))
        if missing == 0:
            gap = None
        elif failed:
            gap = 'failed'
        elif exhausted:
            gap = 'source_exhausted'
        elif attempted:
            gap = 'missing'
        else:
            gap = 'unadapted'
        objects.append({'object_key': key, 'kind': item['kind'], 'name': item['name'], 'url': item['url'],
                        'target': target, 'target_source': item['target_source'], 'target_unknown': target is None,
                        'collected': total,
                        'collected_by_source': {store: counts.get(store, 0)
                                                for store in ('network_reviews', 'reviews_scoped', 'reviews')},
                        'missing': missing, 'gap_reason': gap,
                        'reachable_missing': 0 if gap == 'source_exhausted' else missing,
                        'latest_reason': report['reason'] if report else None,
                        'latest_error': report['error'] if report else '',
                        'evidence_files': sorted(item['files']),
                        'complete': target is not None and missing == 0})
    known = [obj for obj in objects if obj['target'] is not None]
    summary = {'target': sum(obj['target'] for obj in known),
               'collected': sum(obj['collected'] for obj in objects),
               'missing': sum(obj['missing'] for obj in known),
               'reachable_missing': sum(obj['reachable_missing'] for obj in known),
               'unknown_target': sum(1 for obj in objects if obj['target_unknown']),
               'object_count': len(objects), 'directory_count': len(directories),
               'complete': False}
    report_path = folder / ('audit-' + uuid.uuid4().hex + '.json')
    report = {'scope': 'landed_evidence_only', 'complete': False,
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
    counts = {'failed': 0, 'unadapted': 0, 'source_exhausted': 0}
    for obj in objects:
        if isinstance(obj, dict) and obj.get('gap_reason') in counts:
            counts[obj['gap_reason']] += 1
    return {'captured_at': captured_at,
            'missing': summary.get('missing'), 'reachable_missing': summary.get('reachable_missing'),
            'target': summary.get('target'), 'collected': summary.get('collected'),
            'object_count': summary.get('object_count'), 'unknown_target': summary.get('unknown_target'),
            'failed_count': counts['failed'], 'unadapted_count': counts['unadapted'],
            'source_exhausted_count': counts['source_exhausted']}


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
        rows.append(row)
    if limit > 0:
        rows = rows[-limit:]
    # 6.5 差值只认可比较的整数缺口；缺失或非整数的 missing 不给趋势，避免编造告警。
    trend = None
    if len(rows) >= 2:
        previous, latest = rows[-2].get('missing'), rows[-1].get('missing')
        if type(previous) is int and type(latest) is int:
            trend = latest - previous
    warning = None
    if trend is not None and trend > 0:
        warning = f'缺口比上次多了 {trend} 条（missing {rows[-2]["missing"]} → {rows[-1]["missing"]}），先查 failed / unadapted 是否上升'
    return {'file': str(path), 'rows': rows, 'trend': trend, 'warning': warning, 'bad_rows': bad_rows}