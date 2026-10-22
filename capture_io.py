"""受限 App 响应格式与离线导入；未知接口不保存内容。"""
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from hotel_browser import hotel_batch
from network import collect_network, sanitize

logger = logging.getLogger(__name__)
FIELDS = ('hotelName', 'totalCount', 'groupList', 'aiSummaryEntities', 'selectedCommentPhotosEntities',
          'commentRating', 'commentTagList', 'roomList', 'travelTypeList', 'filteredOutCommentInfo', 'staticInfo')
PRIVATE = {'phone', 'mobile', 'email', 'realname', 'idcard', 'idnumber', 'passport', 'password'}


def clean(value):
    # 1.1 已知响应仍递归删除账号字段；不接收原请求正文。
    if isinstance(value, dict):
        return {key: clean(child) for key, child in sanitize(value).items() if key.lower() not in PRIVATE}
    if isinstance(value, list):
        return [clean(child) for child in value]
    return value


def capture_record(url, request, response, status):
    # /* ===== 步骤1：筛选流量；数据源：内存中的业务响应；操作：1) 域名边界 2) 已知结构与身份 3) 未知仅记接口名 ===== */
    logger.info('开始筛选业务响应')
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if not any(host == base or host.endswith('.' + base) for base in ('ctrip.com', 'trip.com')):
        return None
    match = re.fullmatch(r'/restapi/soa2/(\d+)/(?:json/)?([A-Za-z][A-Za-z0-9_]*)(?:\.json)?', parsed.path)
    if not match:
        return None
    # 1.2 不写完整 URL、查询串、请求头、响应头或请求正文。
    record = {'schema': 1, 'kind': 'endpoint_metadata', 'source_channel': 'app_proxy',
              'captured_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
              'host': host, 'service': match[1], 'action': match[2], 'status': status}
    if match.groups() == ('34308', 'getHotelCommentInfo') and status == 200:
        try:
            identity = int(request['hotelId'])
            if identity < 1:
                raise ValueError('酒店 ID 无效')
            data = response['data']
            hotel_batch(data, identity)
            payload = clean({key: data[key] for key in FIELDS if key in data})
            options = request.get('commentFilterOptions') or {}
            # 1.3 只记录页码和数量；任意筛选内容不写入探测记录。
            paging = {key: options[key] for key in ('pageIndex', 'pageSize')
                      if isinstance(options.get(key), int) and options[key] > 0}
            record.update(kind='hotel_reviews', hotel_id=identity, paging=paging, payload=payload,
                          payload_sha256=hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        except (KeyError, TypeError, ValueError, AttributeError):
            record['reason'] = 'unrecognized_or_wrong_identity'
    logger.info('业务响应筛选完成：%s', record['kind'])
    return record


def import_capture(source, output, limit=500, stop=None, emit=None):
    # /* ===== 步骤2：导入已筛选响应；数据源：本工具 JSONL；操作：1) 全文件先验证 2) 按酒店分组 3) 共用稳定 ID 去重 ===== */
    logger.info('开始导入 App 响应')
    if limit < 1:
        raise ValueError('目标条数必须大于零')
    groups = {}
    if stop is not None and stop.is_set():
        return []
    # 2.1 限制行长；旧骨架的原始抓包格式不直接导入。
    with Path(source).open(encoding='utf-8-sig') as file:
        while line := file.readline(5_000_001):
            if stop is not None and stop.is_set():
                return []
            if len(line) > 5_000_000:
                raise ValueError('响应记录过大，拒绝导入')
            record = json.loads(line)
            if record.get('schema') != 1 or record.get('kind') not in ('endpoint_metadata', 'hotel_reviews'):
                raise ValueError('不是受限响应格式，不能直接导入原始抓包文件')
            if record['kind'] == 'endpoint_metadata':
                continue
            identity = int(record['hotel_id'])
            if identity < 1:
                raise ValueError('酒店 ID 无效')
            datetime.fromisoformat(record['captured_at'])
            payload = record['payload']
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if digest != record.get('payload_sha256'):
                raise ValueError('响应摘要不一致')
            batch = hotel_batch(clean(payload), identity)
            for item in batch['result']['items']:
                item['source_schema'] = 'app_proxy_capture'
                item['captured_at'] = record['captured_at']
            groups.setdefault(identity, []).append((record, batch))
    if not groups:
        raise ValueError('文件只有接口名称，没有可导入的已验证酒店点评响应')
    results = []
    for identity, records in groups.items():
        if stop is not None and stop.is_set():
            break
        # 2.2 选取已捕获 AI 内容，不用后续空字段覆盖较完整首批。
        data = records[0][0]['payload']
        ai = next((r['payload']['aiSummaryEntities'] for r, _ in records if r['payload'].get('aiSummaryEntities')), {})
        target = {'kind': 'hotel', 'resource_id': identity, 'name': data.get('hotelName') or str(identity),
                  'url': f'https://hotels.ctrip.com/hotels/{identity}.html', 'display_total': data['totalCount'],
                  'source_method': 'app_proxy_capture', 'page_size': None,
                  'details': clean({key: data[key] for key in FIELDS if key in data and key != 'groupList'}),
                  'ai_summary': {'status': 'response_captured' if ai.get('summaryItemList') else 'not_captured', 'content': clean(ai)},
                  'capture_pages': [{key: r.get('paging', {})[key] for key in ('pageIndex', 'pageSize')
                                     if isinstance(r.get('paging', {}).get(key), int) and r['paging'][key] > 0}
                                    for r, _ in records],
                  'captured_at': [r['captured_at'] for r, _ in records]}
        def fetch(index):
            return records[index - 1][1]
        results.append(collect_network(target, output, limit, stop, emit, fetch=fetch, delay=0, max_pages=len(records)))
    logger.info('App 响应导入完成：%s 个对象', len(results))
    return results
