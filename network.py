"""公开点评响应采集；不使用登录凭据，不绕过验证。"""
import csv
import json
import logging
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from threading import Event
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)
ENDPOINT = 'https://m.ctrip.com/restapi/soa2/13444/json/getCommentCollapseList'
HEAD = {'cver': '1.0', 'lang': '01', 'sid': '8888', 'syscode': '09',
        'extension': [{'name': 'tecode', 'value': 'h5'}]}
PRIVATE_KEYS = {'clientauth', 'auth', 'token', 'accesstoken', 'refreshtoken', 'cookie',
                'authorization', 'ip', 'clientinfo', 'uid', 'userid', 'cid', 'ctok', 'xsid',
                'signature', 'trace', 'ubtdata', 'authinfo'}


def sanitize(value):
    # /* ===== 步骤1：清理响应；数据源：嵌套 JSON；操作：1) 删除凭据与内部标识 2) 保留公开字段 ===== */
    logger.debug('开始清理响应')
    # 1.1 不把账号令牌、精确 IP 和内部用户 ID 写入研究数据。
    if isinstance(value, dict):
        value = {k: sanitize(v) for k, v in value.items() if k.lower() not in PRIVATE_KEYS}
    elif isinstance(value, list):
        value = [sanitize(v) for v in value]
    logger.debug('响应清理完成')
    return value


class NextData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        self.inside = tag == 'script' and dict(attrs).get('id') == '__NEXT_DATA__'

    def handle_endtag(self, tag):
        if tag == 'script':
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


def canonical_url(url):
    # /* ===== 步骤2：核验入口；数据源：用户链接；操作：1) 限定公开详情页 2) 归一化网页地址 ===== */
    logger.info('开始核验对象链接')
    parsed = urlparse(url.strip())
    hotel_path = re.fullmatch(r'/hotels/(\d+)\.html', parsed.path) if parsed.hostname == 'hotels.ctrip.com' else None
    mobile_hotel_path = re.fullmatch(r'/html5/hotel/hoteldetail/(\d+)\.html', parsed.path) if parsed.hostname == 'm.ctrip.com' else None
    if parsed.scheme == 'https' and (hotel_path or mobile_hotel_path):
        logger.info('酒店链接核验完成')
        return 'https://hotels.ctrip.com/hotels/' + (hotel_path or mobile_hotel_path).group(1) + '.html'
    # 2.1 不允许任意地址通过这个入口访问本机或其他服务。
    if parsed.scheme != 'https' or parsed.hostname not in {'you.ctrip.com', 'gs.ctrip.com'}:
        raise ValueError('请粘贴携程 HTTPS 酒店、景点、餐厅或购物详情链接。')
    path = parsed.path.replace('/html5/you/', '/')
    path = path.replace('/foods/fooddetail/', '/food/').replace('/shops/', '/shopping/')
    if not re.fullmatch(r'/(sight|food|shopping|yougourmet/restdetail)/[a-zA-Z0-9]+/\d+\.html', path):
        raise ValueError('需要具体对象详情链接，不能使用城市列表或分享短链接。')
    logger.info('对象链接核验完成')
    return 'https://you.ctrip.com' + path


def parse_target(url, html):
    # /* ===== 步骤3：解析对象；数据源：官方页面状态；操作：1) 验证业务 ID 2) 保存对象详情 ===== */
    logger.info('开始解析对象详情')
    url = canonical_url(url)
    parser = NextData()
    parser.feed(html)
    if not parser.parts:
        raise ValueError('页面没有可用的公开对象状态，可能需要验证；已停止。')
    state = json.loads(''.join(parser.parts))['props']['pageProps']['initialState']
    # 3.1 资源类型必须来自页面，不猜测 POI ID 与业务 ID 的对应关系。
    info = state.get('poiCommentInfo') or state.get('commentInfo', {}).get('poiInfo')
    details = state.get('poiDetail') or state.get('restaurantDetail')
    if not info or not details:
        raise ValueError('该页面的点评适配尚未验证，已停止；请使用手机模式补采。')
    resource_id = int(info['resourceId'])
    route_id = int(url.rsplit('/', 1)[-1].split('.')[0])
    restaurant_poi_route = '/yougourmet/restdetail/' in url
    # 3.1.1 新餐饮链接使用POI，点评仍使用独立业务ID。
    if restaurant_poi_route:
        if (info.get('poiId') != route_id or details.get('poiId') != route_id
                or state.get('poiId') != route_id or int(info['resourceType']) != 12):
            raise ValueError('餐饮POI与详情或点评映射不一致')
    elif resource_id != route_id:
        raise ValueError('链接与响应对象 ID 不一致，拒绝混写。')
    kind = 'restaurant' if restaurant_poi_route else {'sight': 'sight', 'food': 'restaurant', 'shopping': 'shopping'}[url.split('/')[3]]
    target = {'kind': kind, 'url': url, 'resource_id': resource_id,
              'resource_type': int(info['resourceType']), 'poi_id': info.get('poiId'),
              'name': info.get('name') or details.get('poiName') or details.get('name'),
              'display_total': info.get('commentCount', details.get('commentCount')),
              'details': sanitize(details), 'comment_info': sanitize(info),
              'ai_summary': {'status': 'not_captured', 'content': None}}
    logger.info('对象详情解析完成：%s', target['name'])
    return target


def load_target(url):
    # /* ===== 步骤4：读取详情页；数据源：公开网页；操作：1) 有界请求 2) 提取结构化状态 ===== */
    logger.info('开始读取详情页')
    url = canonical_url(url)
    if '/shopping/' in url:
        business_id = int(url.rsplit('/', 1)[-1].split('.')[0])
        overview = post_json('https://m.ctrip.com/restapi/soa2/11557/SearchPoiOverview',
                             {'BusinessId': business_id, 'Scene': 'basic', 'PlateForm': 2, 'head': HEAD})
        info = overview.get('PoiOverviewInfo')
        if not info:
            raise ValueError('购物对象不存在或接口没有返回详情')
        comments = post_json('https://m.ctrip.com/restapi/soa2/11557/GetTalentNoteAndCommentList',
                             {'PoiID': info['PoiId'], 'head': HEAD})
        target = shopping_target(url, overview, comments)
        logger.info('购物详情读取完成')
        return target
    # 4.1 无登录状态，不读取个人浏览器 Cookie。
    with urlopen(url, timeout=30) as response:
        html = response.read(12_000_000).decode('utf-8')
    if urlparse(url).hostname == 'hotels.ctrip.com':
        from hotel_public import parse_public_hotel
        target = parse_public_hotel(url, html)
    else:
        target = parse_target(url, html)
    logger.info('详情页读取完成')
    return target


def shopping_target(url, overview, comments):
    # /* ===== 步骤4.1：解析购物对象；数据源：官方详情及点评入口；操作：1) 核对身份 2) 留存详情 ===== */
    logger.info('开始解析购物对象')
    url = canonical_url(url)
    info = overview.get('PoiOverviewInfo') or {}
    # 4.1.1 从返回的点评链接读取业务类型，不根据 POI 类型猜测。
    query = parse_qs(urlparse(comments.get('commentDetail', {}).get('moreUrl', '')).query)
    business_id = int(url.rsplit('/', 1)[-1].split('.')[0])
    if overview.get('ResultCode') != 0 or info.get('BusinessId') != business_id or query.get('businessId') != [str(business_id)]:
        raise ValueError('购物对象与点评入口不匹配')
    target = {'kind': 'shopping', 'url': url, 'resource_id': business_id,
              'resource_type': int(query['businessType'][0]), 'poi_id': info['PoiId'],
              'name': info['PoiName'], 'display_total': info.get('CommentCount'),
              'details': sanitize(info), 'ai_summary': {'status': 'not_captured', 'content': None}}
    logger.info('购物对象解析完成')
    return target


def post_json(url, payload):
    # /* ===== 步骤4.2：读取公开响应；数据源：固定服务地址；操作：1) JSON 请求 2) 有界超时 ===== */
    logger.info('开始读取公开响应')
    # 4.2.1 请求无账号凭据，失败直接交给上层报告。
    request = Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=30) as response:
        data = json.load(response)
    logger.info('公开响应读取完成')
    return data


def fetch_page(target, page_index):
    # /* ===== 步骤5：请求一页点评；数据源：公开接口；操作：1) 固定排序及大小 2) 不自动绕过错误 ===== */
    logger.info('开始请求第 %s 页', page_index)
    if target.get('source_method') == 'hotel_public_html_snapshot':
        if page_index != 1:
            raise ValueError('公开页面样本不支持翻页')
        logger.info('读取已下载酒店页面样本，不发起分页请求')
        return {'code': 200, 'result': {'items': target['snapshot_comments'], 'totalCount': target['display_total']}}
    # 5.1 sortType=3 原样记录；不把接口排序误称为无偏抽样。
    payload = {'arg': {'channelType': 7, 'collapseType': 1, 'commentTagId': 0,
                       'pageIndex': page_index, 'pageSize': 20, 'resourceId': target['resource_id'],
                       'resourceType': target['resource_type'], 'sortType': 3,
                       'sourceType': 1, 'starType': 0}, 'head': HEAD}
    request = Request(ENDPOINT, data=json.dumps(payload).encode(),
                      headers={'Content-Type': 'application/json'})
    with urlopen(request, timeout=30) as response:
        data = json.load(response)
    logger.info('第 %s 页请求完成', page_index)
    return data


def export_records(db, object_key, folder):
    # /* ===== 步骤6：导出研究数据；数据源：SQLite；操作：1) 嵌套字段写 JSONL 2) CSV 防公式执行 ===== */
    logger.info('开始导出点评')
    jsonl = folder / f'{object_key}.jsonl'
    csv_path = folder / f'{object_key}.csv'
    suffix = '.' + uuid.uuid4().hex + '.tmp'
    jsonl_temp = jsonl.with_name(jsonl.name + suffix)
    csv_temp = csv_path.with_name(csv_path.name + suffix)
    fields = ['commentId', 'content', 'originContent', 'translateContent', 'languageType',
              'score', 'scores', 'publishTime', 'userInfo', 'images', 'videos', 'replyContent',
              'replyInfo', 'replyCount', 'usefulCount', 'ipLocatedName', 'touristTypeDisplay',
              'resourceId', 'resourceType', 'businessId', 'businessType', 'sourceType']
    # 6.1 主文件每行保留完整的已清理响应，不丢嵌套媒体及回复。
    rows = db.execute('SELECT data, fetched_at FROM network_reviews WHERE object_key=? ORDER BY rowid', (object_key,))
    with jsonl_temp.open('w', encoding='utf-8') as jf, csv_temp.open('w', encoding='utf-8-sig', newline='') as cf:
        writer = csv.writer(cf)
        writer.writerow(['object_key', 'fetched_at', *fields, 'full_json'])
        for raw, fetched_at in rows:
            data = json.loads(raw)
            jf.write(json.dumps({'object_key': object_key, 'fetched_at': fetched_at, 'data': data}, ensure_ascii=False) + '\n')
            values = [object_key, fetched_at, *[data.get(field) for field in fields], data]
            cells = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else '' if v is None else str(v) for v in values]
            writer.writerow(["'" + v if v.lstrip().startswith(('=', '+', '-', '@')) else v for v in cells])
    # 6.2 临时文件全部写完才替换；被占用时旧导出仍在。
    csv_temp.replace(csv_path)
    jsonl_temp.replace(jsonl)
    logger.info('点评导出完成')
    return str(jsonl), str(csv_path)


def collect_network(target, output, limit=500, stop=None, emit=None, fetch=None, delay=0.8, max_pages=10000):
    # /* ===== 步骤7：分页持久化；数据源：指定对象；操作：1) 按服务端 ID 去重 2) 中止仍留报告 ===== */
    logger.info('开始网络采集')
    if limit < 1 or max_pages < 1:
        raise ValueError('目标条数和页数必须大于零')
    stop = stop or Event()
    emit = emit or logger.info
    fetch = fetch or (lambda index: fetch_page(target, index))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    key = f"{target['kind']}-{target['resource_id']}"
    started = time.monotonic()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    report = {'object_key': key, 'name': target['name'], 'source_url': target['url'],
              'source_method': target.get('source_method', 'public_comment_api'),
              'target': limit, 'display_total': target['display_total'], 'server_counts': {},
              'complete': False, 'reason': 'capture_exhausted' if target.get('source_method') == 'app_proxy_capture' else 'page_budget_reached',
              'error': '', 'export_error': '',
              'pages': 0, 'count': 0, 'new_count': 0, 'jsonl': None, 'csv': None,
              'sort_type': None if target['kind'] == 'hotel' else 3,
              'page_size': target.get('page_size') if target['kind'] == 'hotel' else 20,
              'ai_summary_status': target.get('ai_summary', {}).get('status', 'not_captured'),
              'limitations': ['网页公开响应不等于 App 全量；隐藏、折叠及不同来源计数不得相加冒充总量。',
                              '首批目标条数受平台排序影响，不能保证无抽样偏差。',
                              '图片视频保留返回元数据与链接；未下载媒体文件。']}
    db = sqlite3.connect(output / 'network.sqlite3')
    try:
        # 7.1 保持与旧 UI 数据库隔离，禁止猜测两个来源的评论身份。
        db.execute('CREATE TABLE IF NOT EXISTS network_reviews (object_key TEXT, comment_id TEXT, data TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(object_key,comment_id))')
        db.commit()
        before = db.execute('SELECT COUNT(*) FROM network_reviews WHERE object_key=?', (key,)).fetchone()[0]
        saved_count = before
        # 7.1.2 抓包详情单独留证，避免覆盖浏览器此前保存的完整酒店详情。
        object_name = f'{key}-app-capture-object.json' if report['source_method'] == 'app_proxy_capture' else f'{key}-object.json'
        (output / object_name).write_text(json.dumps(target, ensure_ascii=False, indent=2), encoding='utf-8')
        seen = set()
        repeated = 0
        for index in range(1, max_pages + 1):
            if stop.is_set():
                report['reason'] = 'cancelled'
                break
            if saved_count >= limit:
                report['reason'] = 'target_reached'
                break
            data = fetch(index)
            # 7.1.1 浏览器等待响应期间也可能取消，不把取消误记为空页。
            if stop.is_set():
                report['reason'] = 'cancelled'
                break
            if data.get('code') != 200 or not isinstance(data.get('result'), dict):
                raise ValueError('接口返回错误、验证或未知结构，已停止；不是空数据。')
            result = data['result']
            if 'items' not in result or not isinstance(result.get('totalCount'), int) or (result['items'] is not None and not isinstance(result['items'], list)):
                raise ValueError('接口返回未知分页结构，已停止')
            report['pages'] += 1
            report['server_counts'] = {k: result.get(k) for k in ('totalCount', 'totalHideCount', 'totalCollapseCount')}
            items = result['items'] or []
            if not items:
                # 离线文件还可能有后续有效批次，空批次不等于文件结束。
                if report['source_method'] == 'app_proxy_capture':
                    continue
                report['reason'] = ('public_snapshot_only' if report['source_method'] == 'hotel_public_html_snapshot'
                                    else 'capture_exhausted' if report['source_method'] == 'app_proxy_capture' else 'empty_page')
                break
            fetched_at = datetime.now(timezone.utc).isoformat()
            old_seen = len(seen)
            # 7.2 一页事务，先校验全部 ID，再保存；避免半页无效响应污染数据。
            if any(not item.get('commentId') for item in items):
                raise ValueError('响应缺少稳定评论 ID，已停止')
            with db:
                for item in items:
                    identity = str(item['commentId'])
                    if saved_count >= limit:
                        break
                    seen.add(identity)
                    cursor = db.execute('INSERT OR IGNORE INTO network_reviews VALUES (?,?,?,?)',
                                        (key, identity, json.dumps(sanitize(item), ensure_ascii=False), fetched_at))
                    saved_count += cursor.rowcount
            repeated = repeated + 1 if len(seen) == old_seen else 0
            emit(f"{target['name']}：第 {index} 页，本次读取 {len(seen)} 个不同评论 ID")
            if report['source_method'] == 'hotel_public_html_snapshot':
                report['reason'] = 'public_snapshot_only'
                break
            if saved_count >= limit:
                report['reason'] = 'target_reached'
                break
            if repeated >= 3 and report['source_method'] != 'app_proxy_capture':
                report['reason'] = 'repeated_pages'
                break
            if stop.wait(delay):
                report['reason'] = 'cancelled'
                break
        report['count'] = db.execute('SELECT COUNT(*) FROM network_reviews WHERE object_key=?', (key,)).fetchone()[0]
        report['new_count'] = report['count'] - before
    except Exception as exc:
        report['reason'], report['error'] = 'error', str(exc)
        report['count'] = db.execute('SELECT COUNT(*) FROM network_reviews WHERE object_key=?', (key,)).fetchone()[0]
        report['new_count'] = report['count'] - before
    finally:
        # 7.3 导出被占用时保留已提交数据库和错误报告。
        try:
            report['jsonl'], report['csv'] = export_records(db, key, output)
        except OSError as exc:
            report['export_error'] = str(exc)
        finally:
            db.close()
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (output / f'{key}-report-{stamp}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        logger.info('网络采集结束：%s', report['reason'])
    return report
