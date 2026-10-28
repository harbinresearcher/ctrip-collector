"""酒店目录：首屏和正常分页共用身份校验，不保存账号及会员报价。"""
import hashlib
import copy
import json
import logging
import re
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from urllib.parse import urlparse

from network import sanitize

logger = logging.getLogger(__name__)


def hotel_directory_batch(data, request, city, index):
    # /* ===== 步骤1：校验酒店列表；数据源：页面首屏或分页响应；操作：1) 城市和页码一致 2) 过滤广告邻城 ===== */
    logger.info('开始校验酒店目录第%s页', index)
    # 1.1 不保存请求头；查询条件仅计算摘要用于检测运行期间的筛选变化。
    destination = request.get('destination') or {}
    requested_page = (request.get('paging') or {}).get('pageIndex')
    returned_page = (data.get('pagingInfo') or {}).get('pageIndex')
    if (destination.get('geo', {}).get('cityId') != city or destination.get('type') != 1
            or type(requested_page) is not int or type(returned_page) is not int
            or requested_page != index or returned_page != index):
        raise ValueError('酒店目录城市或页码不匹配')
    items = data.get('hotelList')
    if not isinstance(items, list):
        raise ValueError('酒店目录缺少列表，不能当作末页')
    scope = copy.deepcopy({k: request.get(k) for k in ('destination', 'date', 'filters', 'roomQuantity')})
    # 1.1.1 首屏省略默认排序，分页补齐；仅规范已观察到的默认值和展示标签。
    dates = scope.get('date') or {}
    if dates.get('flexibleDateOptions') == {}:
        dates.pop('flexibleDateOptions')
    if (dates.get('dateInfo') or {}).get('offset') == 0:
        dates['dateInfo'].pop('offset')
    filters = [{k: v for k, v in f.items() if k not in {'title', 'filterId'}} for f in (scope.get('filters') or [])]
    for f in filters:
        if f.get('type') == '29' and f.get('subType') == '2':
            f.pop('subType')
    if not any(f.get('type') == '17' for f in filters):
        filters.append({'type': '17', 'value': '1'})
    scope['filters'] = sorted(filters, key=lambda f: json.dumps(f, sort_keys=True))
    result = {'objects': [], 'raw_count': len(items), 'skipped_ads': 0, 'skipped_nearby': 0,
              'scope_hash': hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()}
    for item in items:
        # 1.2 广告不是自然排序结果；对象详情只保留公开业务字段。
        info = item.get('hotelInfo') or {}
        if (info.get('advertiseInfo') or {}).get('isAdHotel'):
            result['skipped_ads'] += 1
            continue
        position = info.get('positionInfo') or {}
        if position.get('cityId') is None:
            raise ValueError('酒店卡片缺少城市身份')
        if position['cityId'] != city:
            result['skipped_nearby'] += 1
            continue
        identity = int((info.get('summary') or {}).get('hotelId', 0))
        name = (info.get('nameInfo') or {}).get('name')
        if identity <= 0 or not isinstance(name, str) or not name.strip():
            raise ValueError('酒店卡片缺少有效ID或名称')
        public = {k: sanitize(info[k]) for k in ('summary', 'nameInfo', 'hotelCategory', 'hotelImages',
                  'hotelStar', 'commentInfo', 'positionInfo') if k in info}
        result['objects'].append({'kind': 'hotel', 'resource_id': identity, 'name': name,
            'city_id': city, 'requested_page': index, 'url': f'https://hotels.ctrip.com/hotels/{identity}.html',
            'listing_fields': public})
    logger.info('酒店目录第%s页校验完成：%s个对象', index, len(result['objects']))
    return result


def hotel_initial(html, city):
    # /* ===== 步骤2：读取首屏目录；数据源：Flight JSON字面量；操作：1) 拼接分段 2) 验证初始请求与列表 ===== */
    logger.info('开始解析酒店目录首屏')
    decoder = json.JSONDecoder()
    chunks = []
    # 2.1 只解码JSON，禁止执行网页脚本。
    for match in re.finditer(r'self\.__next_f\.push\(', html):
        try:
            value, _ = decoder.raw_decode(html[match.end():])
        except ValueError:
            continue
        if isinstance(value, list) and len(value) > 1 and value[0] == 1 and isinstance(value[1], str):
            chunks.append(value[1])

    def find(value):
        if isinstance(value, dict):
            if 'initListData' in value and 'initListRequest' in value:
                return value
            children = value.values()
        elif isinstance(value, list):
            children = value
        else:
            return None
        for child in children:
            found = find(child)
            if found is not None:
                return found
        return None

    for line in ''.join(chunks).splitlines():
        if not re.match(r'J[0-9a-f]+:', line):
            continue
        try:
            props = find(json.loads(line.split(':', 1)[1]))
        except ValueError:
            continue
        if props is not None:
            result = hotel_directory_batch(props['initListData'], props['initListRequest'], city, 1)
            logger.info('酒店目录首屏解析完成')
            return result
    logger.info('尚未找到酒店目录首屏')
    return None


def discover_hotels_page(page, url, output, max_pages=5, max_objects=50, stop=None,
                         login_timeout=300, page_timeout=30):
    # /* ===== 步骤3：建立酒店目录任务；数据源：城市入口；操作：1) 限制预算 2) 监听正常翻页 ===== */
    logger.info('开始酒店目录任务')
    match = re.fullmatch(r'https://hotels\.ctrip\.com/hotel/([a-zA-Z]+)([1-9]\d*)/?', url.strip())
    if not match or max_pages < 1 or max_objects < 1:
        raise ValueError('需要酒店城市入口，如 https://hotels.ctrip.com/hotel/shanghai2，预算必须为正数')
    city = int(match.group(2))
    stop = stop or Event()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    stem = output / f'discovery-hotel-{city}-{now.strftime("%Y%m%dT%H%M%S%fZ")}'
    report = {'kind': 'hotel', 'city_id': city, 'source_url': url, 'source_method': 'hotel_browser_directory',
              'captured_at': now.isoformat(), 'objects': [], 'pages': 0, 'complete': False,
              'reason': 'page_budget_reached', 'error': '', 'skipped_ads': 0, 'skipped_nearby': 0,
              'links_file': str(stem.with_suffix('.txt')),
              'limitations': ['目录受查询日期、排序及平台返回范围影响，不代表全城市酒店。',
                              '广告与邻城卡片排除；会员房价不属于本次点评对象目录。']}
    batches, failures, seen = {}, [], set()
    scope = None
    repeated = 0

    def save():
        # 3.1 每个已验证页面都保存JSON和可直接用于评论采集的链接清单。
        stem.with_suffix('.txt').write_text(''.join(o['url'] + '\n' for o in report['objects']), encoding='utf-8')
        stem.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    def receive(response):
        parsed = urlparse(response.url)
        if parsed.hostname != 'm.ctrip.com' or parsed.path != '/restapi/soa2/34951/fetchHotelList' or stop.is_set():
            return
        try:
            # 3.2 失败状态、异城和页码不符均不能当作空页。
            data = response.json()
            status = data.get('ResponseStatus') or {}
            if response.status != 200 or status.get('Ack') != 'Success' or status.get('Errors'):
                raise ValueError('酒店目录接口未确认成功')
            request = response.request.post_data_json or {}
            index = (request.get('paging') or {}).get('pageIndex')
            if type(index) is not int or index < 1:
                raise ValueError('酒店目录请求缺少有效页码')
            batch = hotel_directory_batch(data.get('data') or {}, request, city, index)
            if scope is not None and batch['scope_hash'] != scope:
                raise ValueError('酒店目录查询条件发生变化，已停止混合数据')
            if index > report['pages'] and index <= max_pages:
                batches[index] = batch
        except Exception as exc:
            failures.append(str(exc))

    page.on('response', receive)
    logger.info('酒店目录任务建立完成')
    # /* ===== 步骤4：连续读取酒店页；数据源：首屏和网络事件；操作：1) 正常滚动 2) 去重与逐页保存 ===== */
    logger.info('开始读取酒店目录页面')
    try:
        if stop.is_set():
            report['reason'] = 'cancelled'
            return report
        page.goto(f'https://hotels.ctrip.com/hotels/list/?cityName={match.group(1)}&cityId={city}&v2_mod=17&v2_version=E',
                  wait_until='domcontentloaded', timeout=45000)
        deadline = time.monotonic() + login_timeout
        while not stop.is_set():
            initial = hotel_initial(page.content(), city) if urlparse(page.url).hostname == 'hotels.ctrip.com' else None
            if initial is not None:
                batches[1] = initial
                scope = initial['scope_hash']
                report['query_scope_hash'] = scope
                break
            if failures:
                raise ValueError(failures[0])
            if time.monotonic() >= deadline:
                raise ValueError('未获得酒店目录首屏，请在专用浏览器完成登录后重试')
            page.wait_for_timeout(500)
        for index in range(1, max_pages + 1):
            deadline = time.monotonic() + page_timeout
            while index not in batches and not stop.is_set():
                if failures:
                    raise ValueError(failures[0])
                if time.monotonic() >= deadline:
                    raise ValueError('酒店目录下一页未返回；已保存清单，不能判断末页')
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                page.wait_for_timeout(1500)
            if stop.is_set():
                report['reason'] = 'cancelled'
                break
            if failures:
                raise ValueError(failures[0])
            batch = batches.pop(index)
            if batch['scope_hash'] != scope:
                raise ValueError('酒店目录查询条件发生变化，已停止混合数据')
            before = len(seen)
            report['pages'] += 1
            for key in ('skipped_ads', 'skipped_nearby'):
                report[key] += batch[key]
            for obj in batch['objects']:
                if obj['resource_id'] in seen:
                    continue
                seen.add(obj['resource_id'])
                report['objects'].append(obj)
                if len(seen) >= max_objects:
                    report['reason'] = 'object_budget_reached'
                    break
            repeated = repeated + 1 if len(seen) == before else 0
            save()
            logger.info('酒店目录第%s页保存完成，累计%s家', index, len(seen))
            if report['reason'] == 'object_budget_reached':
                break
            if batch['raw_count'] == 0:
                report['reason'] = 'empty_page'
                break
            if repeated >= 2:
                report['reason'] = 'repeated_pages'
                break
    except Exception as exc:
        report.update(reason='error', error=str(exc))
    finally:
        page.remove_listener('response', receive)
        save()
        logger.info('酒店目录读取结束：%s', report['reason'])
    return report


def discover_hotels(url, output, max_pages=5, max_objects=50, stop=None):
    # /* ===== 步骤5：管理独立浏览器；数据源：本项目登录目录；操作：1) 打开Edge 2) 结束关闭 ===== */
    logger.info('开始打开酒店目录浏览器')
    from playwright.sync_api import sync_playwright
    # 5.1 不复制登录资料到输出目录，目录任务结束后释放浏览器供评论任务使用。
    profile = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CtripCollector/browser-profile'
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(str(profile), channel='msedge', headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            return discover_hotels_page(page, url, output, max_pages, max_objects, stop)
        finally:
            context.close()
            logger.info('酒店目录浏览器已关闭')
