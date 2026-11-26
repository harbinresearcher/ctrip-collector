"""当前全球购公开目录；不把请求页码当成服务端已验证页码。"""
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from network import HEAD, post_json, sanitize

logger = logging.getLogger(__name__)
ENDPOINT = 'https://m.ctrip.com/restapi/soa2/18423/shoppingPoiSearch'


def save_report(stem, report):
    # /* ===== 步骤1：原子保存目录报告；数据源：当前清单；操作：1) 唯一临时文件 2) 写完再替换 ===== */
    # 1.1 每页都会重写这份报告；写到一半被打断不能留下半个 JSON。
    for suffix, content in (('.txt', ''.join(item['url'] + '\n' for item in report['objects'])),
                            ('.json', json.dumps(report, ensure_ascii=False, indent=2))):
        target = stem.with_suffix(suffix)
        temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(content, encoding='utf-8')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def discover_shopping(district, output, max_pages=5, max_objects=50, stop=None, fetch=None, delay=1.5):
    # /* ===== 步骤1：建立购物目录；数据源：地区ID；操作：1) 有界分页 2) 保存来源和时间 ===== */
    logger.info('开始购物目录发现')
    district = int(district)
    if district < 0 or max_pages < 1 or max_objects < 1:
        raise ValueError('地区ID不能为负数，页数和对象上限必须为正数')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    stop = stop or Event()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    stem = output / f'discovery-shopping-{district}-{stamp}'
    report = {'kind': 'shopping', 'district_id': district, 'source_url': ENDPOINT,
              'captured_at': datetime.now(timezone.utc).isoformat(), 'objects': [], 'pages': 0,
              'listing_total': None, 'complete': False, 'server_pages_verified': False,
              'reason': 'page_budget_reached', 'error': '', 'skipped_ads': 0, 'skipped_nearby': 0,
              'skipped_invalid': 0, 'links_file': str(stem.with_suffix('.txt'))}
    seen, repeated = set(), 0
    try:
        for page in range(1, max_pages + 1):
            if stop.is_set():
                report['reason'] = 'cancelled'
                break
            # 1.1 参数来自正常匿名网页请求；不填用户定位或认证信息。
            payload = {'districtId': district, 'cityId': 0, 'locationDistrictId': 0, 'locationCityId': 0,
                       'geoPoint': {'latitude': 0, 'longitude': 0, 'coordinate': ''},
                       'keyWordSearch': None, 'filteredItems': [], 'sort': 1, 'pageIndex': page,
                       'pageSize': 10, 'channel': 'H5', 'statistics': 'AllPoiTag,Zone,PrefectureDistrict,StoreService,DiscountInfoType',
                       'returnData': 'ShoppingPoi,AdvertisePoi,NoResultRecommendPoi', 'pageId': 1, 'distance': 0, 'head': HEAD}
            response = fetch(page) if fetch else post_json(ENDPOINT, payload)
            status = response.get('ResponseStatus') or {}
            items = response.get('shoppingPoiList')
            if status.get('Ack') != 'Success' or status.get('Errors') or not isinstance(items, list):
                raise ValueError('购物目录响应未确认成功，不能当作末页')
            # 1.2 服务端会回显地区编号与名称；与请求不一致时立即停止，不能把别的城市当成本地区结果。
            echoed = response.get('districtId')
            if type(echoed) is int and echoed != district:
                raise ValueError(f'购物目录返回的地区编号 {echoed} 与请求的 {district} 不一致，已停止')
            name = response.get('districtName')
            if isinstance(name, str) and 0 < len(name.strip()) <= 40:
                report['district_name'] = name.strip()
                kind = response.get('districtType')
                report['district_type'] = kind if isinstance(kind, str) and len(kind) <= 20 else None
            report['pages'] += 1
            report['listing_total'] = response.get('total')
            before = len(seen)
            # /* ===== 步骤2：筛选目录卡片；数据源：服务端列表；操作：1) 过滤广告/邻城 2) 业务ID去重 ===== */
            logger.info('开始处理购物目录第%s页', page)
            for item in items:
                if item.get('isAdvertiesment'):
                    report['skipped_ads'] += 1
                    continue
                try:
                    identity, poi, item_district = int(item['businessId']), int(item['poiId']), int(item['districtId'])
                    if min(identity, poi, item_district) <= 0:
                        raise ValueError('无效对象身份')
                except (KeyError, TypeError, ValueError):
                    report['skipped_invalid'] += 1
                    continue
                if district and item_district != district:
                    report['skipped_nearby'] += 1
                    continue
                if identity in seen:
                    continue
                seen.add(identity)
                report['objects'].append({'kind': 'shopping', 'resource_id': identity, 'poi_id': poi,
                    'district_id': item_district, 'name': item.get('chineseName'),
                    'comment_count': item.get('commentCount'), 'requested_page': page,
                    'url': f'https://you.ctrip.com/shopping/district{item_district}/{identity}.html',
                    'listing_fields': sanitize(item)})
                if len(seen) >= max_objects:
                    report['reason'] = 'object_budget_reached'
                    break
            repeated = repeated + 1 if len(seen) == before else 0
            save_report(stem, report)
            logger.info('购物目录第%s页处理完成，累计%s对象', page, len(seen))
            if report['reason'] == 'object_budget_reached':
                break
            if not items:
                report['reason'] = 'empty_page'
                break
            if repeated >= 2:
                report['reason'] = 'repeated_pages'
                break
            if page < max_pages and stop.wait(delay):
                report['reason'] = 'cancelled'
                break
    except Exception as exc:
        report.update(reason='error', error=str(exc))
    finally:
        # 2.1 服务端未回显页码，保留请求记录，不承诺无遗漏或全量。
        try:
            save_report(stem, report)
        except OSError as exc:
            report['export_error'] = str(exc)
            logger.warning('购物目录报告写入失败，已发现清单仍返回：%s', exc)
        logger.info('购物目录发现结束：%s', report['reason'])
    return report
