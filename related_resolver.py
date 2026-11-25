"""核验餐饮推荐 POI 对应的详情页。"""
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from urllib.request import HTTPRedirectHandler, build_opener

from network import canonical_url, parse_target

logger = logging.getLogger(__name__)


class SameObjectRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 1.1 跳转前限制目标，不能让外部页面冒充携程对象。
        if canonical_url(newurl) != canonical_url(req.full_url):
            raise ValueError('详情页跳转到其他对象')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def load_target(url):
    # /* ===== 步骤1：读取候选详情；数据源：携程公开页；操作：1) 限制跳转 2) 校验业务身份 ===== */
    logger.info('开始读取候选餐厅详情')
    opener = build_opener(SameObjectRedirect())
    with opener.open(url, timeout=30) as response:
        if canonical_url(response.geturl()) != url:
            raise ValueError('详情页跳转到其他对象')
        raw = response.read(12_000_001)
    if len(raw) > 12_000_000:
        raise ValueError('页面超过读取上限')
    target = parse_target(url, raw.decode('utf-8'))
    logger.info('候选餐厅详情读取完成')
    return target


def resolve_related_restaurants(report_file, output, stop=None, emit=None, max_objects=20):
    # /* ===== 步骤1：校验来源报告；数据源：输出目录；操作：1) 校验路径与身份 2) 限制候选数量 ===== */
    logger.info('开始校验餐厅推荐报告')
    if not isinstance(report_file, str) or not re.fullmatch(r'related-restaurant-[0-9a-f]{32}\.json', report_file):
        raise ValueError('请选择输出目录中的餐厅相关对象报告')
    if type(max_objects) is not int or not 1 <= max_objects <= 100:
        raise ValueError('核验数量必须在 1 至 100 之间')
    folder = Path(output).resolve()
    path = folder / report_file
    if (path.is_symlink() or path.is_junction() or not path.is_file()
            or path.resolve().parent != folder):
        raise ValueError('来源报告不存在或不在输出目录中')
    with path.open('rb') as stream:
        raw = stream.read(12_000_001)
    if len(raw) > 12_000_000:
        raise ValueError('来源报告超过读取上限')
    source_report = json.loads(raw.decode('utf-8'))
    if (not isinstance(source_report, dict)
            or source_report.get('source_method') != 'public_restaurant_page_recommendations'
            or source_report.get('reason') not in ('recommendations_read', 'recommendations_partial')):
        raise ValueError('不是有效的餐厅推荐报告')
    availability = source_report.get('list_status')
    if ((availability is not None and (not isinstance(availability, dict)
                                      or availability.get('moreRestaurantList') != 'available'))
            or (availability is None and source_report['reason'] == 'recommendations_partial')):
        raise ValueError('来源页面的餐厅推荐列表不可用')
    source = source_report.get('source')
    if not isinstance(source, dict) or not isinstance(source.get('url'), str):
        raise ValueError('来源报告缺少餐厅身份')
    source_url = canonical_url(source['url'])
    match = re.fullmatch(r'https://you\.ctrip\.com/yougourmet/restdetail/([a-zA-Z0-9]+)/([1-9][0-9]*)\.html', source_url)
    if (not match or source.get('kind') != 'restaurant' or type(source.get('poi_id')) is not int
            or source['poi_id'] != int(match.group(2)) or type(source.get('resource_id')) is not int
            or source['resource_id'] <= 0 or source.get('resource_type') != 12):
        raise ValueError('来源餐厅身份不一致')
    rows = source_report.get('objects')
    if not isinstance(rows, list):
        raise ValueError('来源报告缺少候选列表')
    candidates = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('kind') not in ('hotel', 'restaurant'):
            raise ValueError('候选列表结构错误')
        if row['kind'] == 'hotel':
            continue
        identity = row.get('poi_id')
        if type(identity) is not int or identity <= 0 or identity == source['poi_id']:
            raise ValueError('餐厅候选 POI 无效')
        if identity not in seen:
            candidates.append(identity)
            seen.add(identity)
    selected = candidates[:max_objects]
    stop = stop or Event()
    emit = emit or logger.info
    filename = 'related-restaurant-verified-' + uuid.uuid4().hex + '.json'
    output_path = folder / filename
    report = {'source_method': 'public_restaurant_poi_resolution', 'source_report': report_file,
              'source': {'url': source_url, 'poi_id': source['poi_id'], 'resource_id': source['resource_id']},
              'reason': 'candidate_limit_reached' if len(candidates) > max_objects else 'candidates_processed',
              'error': '', 'complete': False, 'remaining_candidates': max(0, len(candidates) - max_objects),
              'results': [{'poi_id': identity, 'status': 'not_started', 'error': ''} for identity in selected],
              'objects': [], 'report_file': str(output_path),
              'limitations': ['候选路径使用来源城市字段，不代表餐厅真实行政区。',
                              '推荐结果不是城市目录或全平台对象。']}
    logger.info('餐厅推荐报告校验完成：%s 个候选', len(selected))
    # /* ===== 步骤2：核验候选；数据源：公开详情页；操作：1) 逐条核对 POI 和业务 ID 2) 控制请求间隔 ===== */
    logger.info('开始核验餐厅候选')
    for index, item in enumerate(report['results']):
        if stop.is_set() or (index and stop.wait(1.5)):
            report['reason'] = 'cancelled'
            item['status'] = 'cancelled'
            break
        candidate_url = f'https://you.ctrip.com/yougourmet/restdetail/{match.group(1)}/{item["poi_id"]}.html'
        emit(f'正在核验餐厅候选 {index + 1}/{len(selected)}')
        try:
            target = load_target(candidate_url)
            if stop.is_set():
                report['reason'] = 'cancelled'
                item['status'] = 'cancelled'
                break
            if (not isinstance(target, dict) or target.get('url') != candidate_url
                    or target.get('kind') != 'restaurant' or target.get('poi_id') != item['poi_id']
                    or type(target.get('poi_id')) is not int or target.get('resource_type') != 12
                    or type(target.get('resource_id')) is not int or target['resource_id'] <= 0):
                raise ValueError('候选详情的 POI、点评业务 ID 或 URL 不匹配')
            # 2.1 只有完整核对后的链接才进入可采对象清单。
            verified = {'kind': 'restaurant', 'poi_id': item['poi_id'], 'resource_id': target['resource_id'],
                        'resource_type': 12, 'url': candidate_url, 'verified_at': datetime.now(timezone.utc).isoformat()}
            for field in ('name', 'display_total'):
                if type(target.get(field)) in (str, int, float):
                    verified[field] = target[field]
            item.update(status='verified', url=candidate_url, resource_id=target['resource_id'])
            report['objects'].append(verified)
        except Exception as exc:
            item.update(status='error', error=str(exc) or type(exc).__name__)
            report.update(reason='error', error=item['error'])
            break
    logger.info('餐厅候选核验结束：%s', report['reason'])
    # /* ===== 步骤3：保存结果；数据源：已核验对象；操作：1) 写唯一临时文件 2) 原子替换 ===== */
    logger.info('开始保存候选核验报告')
    temporary = output_path.with_name(output_path.name + '.tmp')
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    logger.info('候选核验报告保存完成')
    return report
