"""沿公开景点推荐逐层发现对象，并保存可继续的队列。"""
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from network import canonical_url, sanitize
from sight_related import discover_sight_related

logger = logging.getLogger(__name__)
PATTERN = re.compile(r'https://you\.ctrip\.com/(sight|food|shopping)/[a-zA-Z0-9]+/([1-9][0-9]*)\.html')
KINDS = {'sight':'sight', 'food':'restaurant', 'shopping':'shopping'}


def checked_url(url, kind=None):
    # /* ===== 步骤1：校验对象链接；数据源：页面候选或本地队列；操作：1) 限定携程详情 2) 核对类型 ===== */
    logger.info('开始核验关联对象链接')
    # 1.1 缺失或非文本候选应作为无效条目，不能中断整页。
    if not isinstance(url, str):
        raise ValueError('关联对象链接无效')
    value = canonical_url(url)
    match = PATTERN.fullmatch(value)
    if not match or (kind is not None and KINDS[match.group(1)] != kind):
        raise ValueError('关联对象链接类型无效')
    logger.info('关联对象链接核验完成')
    return value, int(match.group(2))


def digest_report(report):
    # /* ===== 步骤1：核对队列摘要；数据源：本地报告；操作：1) 排除摘要字段 2) 计算内容摘要 ===== */
    logger.info('开始计算关联队列摘要')
    data = {key:value for key,value in report.items() if key != 'state_sha256'}
    value = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    logger.info('关联队列摘要计算完成')
    return value


def save_checkpoint(report):
    # /* ===== 步骤1：保存队列断点；数据源：当前发现结果；操作：1) 唯一临时文件 2) 原子替换 ===== */
    logger.info('开始保存关联发现断点')
    path = Path(report['report_file'])
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    report['updated_at'] = datetime.now(timezone.utc).isoformat()
    report['state_sha256'] = digest_report(report)
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    logger.info('关联发现断点保存完成')


def normalize_checkpoint(report):
    # /* ===== 步骤1：归并已有对象；数据源：已验摘要的断点；操作：1) 按类型和资源 ID 合并 2) 保留来源边 ===== */
    logger.info('开始归并关联发现断点')
    objects, aliases, unique = {}, {}, []
    # 1.1 同资源的别名使用首个地址；POI 冲突不能自动选择。
    for item in report['objects']:
        if not isinstance(item, dict) or item.get('kind') not in {'sight','restaurant','shopping'}:
            raise ValueError('断点对象类型无效')
        link, identity = checked_url(item.get('url'), item['kind'])
        if type(item.get('resource_id')) is not int or item['resource_id'] != identity:
            raise ValueError('断点对象资源编号不一致')
        key = (item['kind'], identity)
        previous = objects.get(key)
        if previous:
            if (previous.get('poi_id') is not None and item.get('poi_id') is not None
                    and previous['poi_id'] != item['poi_id']):
                raise ValueError('断点包含冲突的对象 POI')
            if previous.get('poi_id') is None and item.get('poi_id') is not None:
                previous['poi_id'] = item['poi_id']
            if item.get('identity_status') == 'page_verified':
                previous['identity_status'] = 'page_verified'
            aliases[link] = previous['url']
        else:
            item['url'] = link
            objects[key] = item
            aliases[link] = link
            unique.append(item)
    # 1.2 保留不同来源边，将别名映射到同一对象并合并相同边的观察。
    edges, combined = {}, []
    for edge in report['edges']:
        if not isinstance(edge, dict) or edge.get('source') not in aliases or edge.get('target') not in aliases:
            raise ValueError('断点关联边缺少对象')
        source, target = aliases[edge['source']], aliases[edge['target']]
        key = (source, target)
        if key not in edges:
            edge['source'], edge['target'] = source, target
            edges[key] = edge
            combined.append(edge)
        else:
            prior = edges[key].setdefault('observations', [])
            prior.extend(value for value in edge.get('observations', []) if value not in prior)
    # 1.3 旧断点可能把同一资源别名列入待访问队列，已展开资源不能重复请求。
    expanded = list(dict.fromkeys(aliases[url] for url in report['expanded_sights']))
    pending = [url for url in dict.fromkeys(aliases[url] for url in report['pending_sights']) if url not in expanded]
    report.update(objects=unique, edges=combined, expanded_sights=expanded, pending_sights=pending)
    logger.info('关联发现断点归并完成')
    return objects, edges


def discover_sight_graph(root_url, output, max_visits=5, stop=None, emit=None, fetch=None, delay=1.5, resume_file=None):
    # /* ===== 步骤1：建立发现队列；数据源：景点根链接或旧报告；操作：1) 限制预算 2) 核验可恢复状态 ===== */
    logger.info('开始准备景点关联发现')
    root, identity = checked_url(root_url, 'sight')
    if type(max_visits) is not int or not 1 <= max_visits <= 100:
        raise ValueError('每次访问的景点数量须为 1 至 100')
    folder = Path(output).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    stop, emit = stop or Event(), emit or logger.info
    finder = fetch or discover_sight_related
    if resume_file is None:
        path = folder / ('related-graph-' + uuid.uuid4().hex + '.json')
        report = {'schema':1, 'source_method':'public_sight_recommendation_graph',
                  'root_url':root, 'report_file':str(path), 'complete':False,
                  'reason':'running', 'error':'', 'objects':[{'kind':'sight', 'url':root,
                  'resource_id':identity, 'identity_status':'page_link_only'}], 'edges':[],
                  'expanded_sights':[], 'pending_sights':[root], 'page_reports':[],
                  'skipped_invalid':0, 'identity_conflicts':0, 'run_visits':0,
                  'limitations':['仅沿来源页面推荐展开，不代表全部对象。',
                                 '候选链接来自页面；未打开的对象身份未逐个核验。']}
        save_checkpoint(report)
    else:
        if not isinstance(resume_file, str) or not re.fullmatch(r'related-graph-[0-9a-f]{32}\.json', resume_file):
            raise ValueError('请选择本目录的关联发现报告')
        path = folder / resume_file
        if path.is_symlink() or path.is_junction() or not path.is_file() or path.resolve().parent != folder:
            raise ValueError('关联发现报告不存在或不属于当前目录')
        with path.open('rb') as stream:
            raw = stream.read(12_000_001)
        if len(raw) > 12_000_000:
            raise ValueError('关联发现报告超过读取上限')
        report = json.loads(raw.decode('utf-8'))
        if (not isinstance(report, dict) or report.get('schema') != 1
                or report.get('source_method') != 'public_sight_recommendation_graph'
                or report.get('root_url') != root or report.get('report_file') != str(path)
                or report.get('complete') is not False or report.get('state_sha256') != digest_report(report)):
            raise ValueError('关联发现报告与根对象不一致或已损坏')
        for field in ('objects', 'edges', 'expanded_sights', 'pending_sights', 'page_reports'):
            if not isinstance(report.get(field), list):
                raise ValueError('关联发现报告结构无效')
        for url in report['expanded_sights'] + report['pending_sights']:
            checked_url(url, 'sight')
        if (len(set(report['pending_sights'])) != len(report['pending_sights'])
                or len(set(report['expanded_sights'])) != len(report['expanded_sights'])
                or set(report['pending_sights']) & set(report['expanded_sights'])):
            raise ValueError('关联发现队列重复或状态冲突')
        report.update(reason='running', error='', run_visits=0)
    logger.info('景点关联发现准备完成')

    # /* ===== 步骤2：逐层访问景点；数据源：验证后的队列；操作：1) 留存页面关系 2) 每页保存断点 ===== */
    logger.info('开始逐层发现景点关联对象')
    objects, edges = normalize_checkpoint(report)
    while report['pending_sights'] and report['run_visits'] < max_visits:
        if stop.is_set():
            report['reason'] = 'cancelled'
            break
        # 2.0 续跑也沿用上次请求时间，防止相邻任务绕过请求间隔。
        last = report.get('last_request_at')
        if last:
            previous = datetime.fromisoformat(last)
            if previous.utcoffset() is None:
                raise ValueError('关联队列请求时间缺少时区')
            elapsed = (datetime.now(timezone.utc) - previous).total_seconds()
            remaining = max(0.0, min(delay, delay - elapsed))
            if remaining and stop.wait(remaining):
                report['reason'] = 'cancelled'
                break
        url = report['pending_sights'][0]
        emit('正在读取第 %s 个关联景点' % (report['run_visits'] + 1))
        try:
            # 2.0.1 请求前记录时间，出错或中断后恢复也能遵守间隔。
            report['last_request_at'] = datetime.now(timezone.utc).isoformat()
            save_checkpoint(report)
            page = finder(url, folder, stop=stop, emit=emit)
            if stop.is_set() or page.get('reason') == 'cancelled':
                report['reason'] = 'cancelled'
                break
            if page.get('reason') not in {'recommendations_read','recommendations_partial'}:
                raise ValueError(page.get('error') or '景点推荐页读取失败')
            source = page.get('source')
            source_id = checked_url(url, 'sight')[1]
            if (not isinstance(source, dict) or source.get('url') != url or source.get('kind') != 'sight'
                    or type(source.get('resource_id')) is not int or source['resource_id'] != source_id
                    or type(source.get('poi_id')) is not int or source['poi_id'] < 1
                    or not isinstance(page.get('objects'), list)):
                raise ValueError('景点推荐来源身份或对象列表不一致')
            current = objects.get(('sight', source_id))
            if current is None:
                raise ValueError('待访问景点不在对象清单中')
            if current.get('poi_id') is not None and current['poi_id'] != source['poi_id']:
                report['identity_conflicts'] += 1
                raise ValueError('推荐景点与详情页 POI 不一致')
            # 2.1 验证每个候选，关系保留来源，未确认身份的候选不能标为已核验。
            additions, staged, new_edges, conflicts, invalid = [], {}, {}, 0, 0
            for candidate in page['objects']:
                try:
                    if not isinstance(candidate, dict) or candidate.get('kind') not in {'sight','restaurant','shopping'}:
                        raise ValueError('候选类型无效')
                    link, resource_id = checked_url(candidate.get('url'), candidate['kind'])
                    if type(candidate.get('resource_id')) is not int or candidate['resource_id'] != resource_id:
                        raise ValueError('候选资源编号不一致')
                    if type(candidate.get('poi_id')) is not int or candidate['poi_id'] < 1:
                        raise ValueError('候选 POI 无效')
                    if not isinstance(candidate.get('observations', []), list):
                        raise ValueError('候选关系格式无效')
                except (TypeError, ValueError):
                    invalid += 1
                    continue
                object_key = (candidate['kind'], resource_id)
                old = objects.get(object_key) or staged.get(object_key)
                if old and old.get('poi_id') is not None and old['poi_id'] != candidate['poi_id']:
                    conflicts += 1
                    continue
                if old is None:
                    entry = {key:candidate[key] for key in ('kind','url','resource_id','poi_id','name','identity_status') if key in candidate}
                    entry['url'] = link
                    entry['identity_status'] = 'page_link_only'
                    additions.append(entry)
                    staged[object_key] = entry
                    old = entry
                # 2.1.1 地址别名指向同一资源；不同来源的边分别保留。
                edge_key = (url, old['url'])
                if edge_key not in edges and edge_key not in new_edges:
                    new_edges[edge_key] = {'source':url,'target':old['url'],'kind':candidate['kind'],
                                           'observations':sanitize(candidate.get('observations', []))}
                elif edge_key in new_edges:
                    observations = new_edges[edge_key]['observations']
                    observations.extend(value for value in sanitize(candidate.get('observations', [])) if value not in observations)
            # 2.2 单页全部处理完毕后提交内存状态，再写断点；请求失败保留原队列。
            current['poi_id'] = source['poi_id']
            current['identity_status'] = 'page_verified'
            report['pending_sights'].pop(0)
            report['expanded_sights'].append(url)
            for entry in additions:
                objects[(entry['kind'],entry['resource_id'])] = entry
                report['objects'].append(entry)
                if entry['kind'] == 'sight' and entry['url'] not in report['expanded_sights'] and entry['url'] not in report['pending_sights']:
                    report['pending_sights'].append(entry['url'])
            for edge_key, edge in new_edges.items():
                edges[edge_key] = edge
                report['edges'].append(edge)
            report['skipped_invalid'] += invalid
            report['identity_conflicts'] += conflicts
            report['page_reports'].append({'url':url, 'report_file':page.get('report_file'),
                                           'recommendations_status':page.get('recommendations_status'),
                                           'list_status':page.get('list_status')})
            report['run_visits'] += 1
            save_checkpoint(report)
        except Exception as exc:
            report.update(reason='error', error=str(exc) or type(exc).__name__)
            break
    if report['reason'] == 'running':
        report['reason'] = 'visit_budget_reached' if report['pending_sights'] else 'queue_exhausted'
    save_checkpoint(report)
    logger.info('景点关联发现结束：%s', report['reason'])
    return report
