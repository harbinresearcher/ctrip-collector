"""酒店浏览器响应采集。只保存当前酒店点评，不记录认证请求。"""
import logging
import os
import time
from pathlib import Path
from threading import Event
from urllib.parse import urlparse

from network import canonical_url, collect_network, load_target, sanitize

logger = logging.getLogger(__name__)
STATE = '() => window.__NEXT_DATA__?.props?.pageProps?.initialState?.preloadedCommentData?.data'
SCROLL = """() => {
    const el = [...document.querySelectorAll('div')].find(e =>
        e.scrollHeight > e.clientHeight + 100 && ['auto','scroll'].includes(getComputedStyle(e).overflowY));
    if (!el) return false;
    el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight - 100);
    el.scrollTop = el.scrollHeight;
    return true;
}"""


def hotel_batch(data, identity):
    # /* ===== 步骤1：校验点评批次；数据源：页面状态或点评响应；操作：1) 验证结构和酒店 2) 保留公开原字段 ===== */
    logger.info('开始校验酒店点评批次')
    if not isinstance(data, dict) or not isinstance(data.get('totalCount'), int) or not isinstance(data.get('groupList'), list):
        raise ValueError('酒店点评响应结构未知，不能视为空页')
    records = []
    # 1.1 稳定评论 ID 与酒店 ID 都必须存在，防止其他页面混入。
    for group in data['groupList']:
        if not isinstance(group.get('commentList'), list):
            raise ValueError('酒店点评分组缺少列表')
        for original in group['commentList']:
            if not original.get('id') or str(original.get('hotelId')) != str(identity):
                raise ValueError('酒店点评缺少身份或属于其他对象')
            record = sanitize(original)
            record.update(commentId=original['id'], score=original.get('rating'),
                          scores=sanitize(original.get('ratingInfo')), publishTime=original.get('createDate'),
                          images=sanitize(original.get('imageList', [])), videos=sanitize(original.get('videoList', [])),
                          replyInfo=sanitize(original.get('feedbackList', [])),
                          ipLocatedName=original.get('ipLocation'), source_schema='hotel_browser_response')
            records.append(record)
    logger.info('酒店点评批次校验完成：%s 条', len(records))
    return {'code': 200, 'result': {'items': records, 'totalCount': data['totalCount']}}


def collect_hotel_page(page, url, output, limit=500, stop=None, emit=None, login_timeout=300):
    # /* ===== 步骤2：等待可见点评；数据源：独立浏览器；操作：1) 用户自行登录 2) 接收当前酒店正常翻页响应 ===== */
    logger.info('开始酒店浏览器采集')
    from playwright.sync_api import Error as BrowserError
    stop = stop or Event()
    emit = emit or logger.info
    url = canonical_url(url)
    if urlparse(url).hostname != 'hotels.ctrip.com':
        raise ValueError('酒店浏览器模式需要酒店详情链接')
    identity = int(url.rsplit('/', 1)[-1].split('.')[0])
    batches = {}
    filters = {}
    failures = []

    def receive(response):
        # 2.1 只读取指定接口的酒店 ID、页码和响应，不保存请求头或认证字段。
        parsed = urlparse(response.url)
        if parsed.hostname != 'm.ctrip.com' or parsed.path != '/restapi/soa2/34308/getHotelCommentInfo':
            return
        try:
            request = response.request.post_data_json or {}
            if str(request.get('hotelId')) != str(identity):
                return
            options = request.get('commentFilterOptions') or {}
            index = options.get('pageIndex')
            if not isinstance(index, int) or index < 1:
                raise ValueError('酒店响应缺少有效页码')
            # 2.1.1 仅接收默认全部点评条件，运行中手动改筛选不能混入原样本。
            expected = {'pageSize': 10, 'keyWord': '', 'commonStatisticList': ['1'],
                        'rooms': [], 'travelTypes': [], 'filterDateTypeList': [], 'orderTypes': [], 'repeatComment': 1}
            if any(options.get(key) != value for key, value in expected.items()):
                raise ValueError('酒店点评筛选条件发生变化')
            filters[index] = sanitize(options)
            batches[index] = hotel_batch(response.json().get('data'), identity)
        except Exception:
            failures.append('酒店翻页响应无效或需要重新验证；已停止')

    page.on('response', receive)
    try:
        page.goto(f'https://m.ctrip.com/h5/xHtlCommentList/list?id={identity}', wait_until='domcontentloaded', timeout=60000)
        emit('如浏览器提示登录，请在浏览器内完成。登录状态只留在本机专用目录。')
        deadline = time.monotonic() + login_timeout
        initial = None
        # 2.2 等待中仍响应停止，登录超时不生成空数据成功报告。
        while time.monotonic() < deadline and not stop.is_set():
            try:
                initial = page.evaluate(STATE)
            except BrowserError as exc:
                # 2.2.1 登录回跳恰好销毁旧页面上下文时，等待新页面，其他错误仍抛出。
                if 'Execution context was destroyed' not in str(exc):
                    raise
                page.wait_for_timeout(500)
                continue
            if isinstance(initial, dict) and isinstance(initial.get('groupList'), list) and (initial['groupList'] or initial.get('totalCount') == 0):
                break
            page.wait_for_timeout(500)
        if stop.is_set():
            raise ValueError('用户已停止酒店登录等待')
        if not isinstance(initial, dict) or not isinstance(initial.get('groupList'), list) or (not initial['groupList'] and initial.get('totalCount') != 0):
            raise ValueError('未获得酒店点评状态，请完成登录后重试')
        batches[1] = hotel_batch(initial, identity)
        ai = initial.get('aiSummaryEntities') or {}
        target = {'kind': 'hotel', 'url': url, 'resource_id': identity, 'name': initial.get('hotelName') or str(identity),
                  'display_total': initial['totalCount'], 'source_method': 'hotel_browser_response', 'page_size': 10,
                  'details': sanitize({key: initial[key] for key in ('staticInfo', 'commentRating', 'roomList',
                    'commentTagList', 'travelTypeList', 'filteredOutCommentInfo', 'totalCountForPage') if key in initial}),
                  'ai_summary': {'status': 'response_captured' if ai.get('summaryItemList') else 'not_captured', 'content': sanitize(ai)},
                  'ai_photos': {'status': 'response_captured' if initial.get('selectedCommentPhotosEntities') else 'not_captured',
                                'content': sanitize(initial.get('selectedCommentPhotosEntities'))}}
        # 2.3 公开详情单独补充地址、设施、政策；获取失败显式记录，不影响已读点评保存。
        try:
            public = load_target(url)
            target['public_details'] = public['details']
            target['public_details_status'] = 'captured'
        except Exception:
            target['public_details_status'] = 'unavailable'
        logger.info('酒店首批点评与 AI 字段读取完成')

        def fetch(index):
            # /* ===== 步骤3：触发正常翻页；数据源：浏览器网络事件；操作：1) 滚动网页 2) 校验连续页码 3) 有界等待 ===== */
            logger.info('开始等待酒店第 %s 页', index)
            deadline = time.monotonic() + 30
            while index not in batches:
                if stop.is_set():
                    return {'code': 200, 'result': {'items': [], 'totalCount': initial['totalCount']}}
                if failures:
                    raise ValueError(failures[0])
                if time.monotonic() >= deadline:
                    raise ValueError('网页未返回下一页；已保存已有数据，不能判断已采完')
                page.evaluate(SCROLL)
                page.wait_for_timeout(700)
            logger.info('酒店第 %s 页接收完成', index)
            return batches.pop(index)

        result = collect_network(target, output, limit, stop, emit, fetch=fetch, delay=0)
        # 3.1 筛选元数据独立留存，不保存包含认证头的原请求。
        import json
        Path(output, f'hotel-{identity}-filters.json').write_text(json.dumps(filters, ensure_ascii=False, indent=2), encoding='utf-8')
        return result
    finally:
        page.remove_listener('response', receive)
        logger.info('酒店浏览器采集结束')


def collect_hotel(url, output, limit=500, stop=None, emit=None):
    # /* ===== 步骤4：管理专用浏览器；数据源：本机 Edge；操作：1) 独立登录目录 2) 结束关闭浏览器 ===== */
    logger.info('开始打开酒店专用浏览器')
    from playwright.sync_api import sync_playwright
    # 4.1 账号状态不放进输出目录或交付包，不读取日常浏览器资料。
    profile = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CtripCollector' / 'browser-profile'
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), channel='msedge', headless=False)
        try:
            return collect_hotel_page(context.pages[0] if context.pages else context.new_page(), url, output, limit, stop, emit)
        finally:
            context.close()
            logger.info('酒店专用浏览器已关闭')
