"""从餐饮详情状态提取页面明确提供的相关对象。"""
import logging
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from urllib.request import urlopen

from network import NextData, canonical_url

logger = logging.getLogger(__name__)


class RelatedListsUnavailable(ValueError):
    def __init__(self, statuses):
        self.statuses = statuses
        super().__init__('相关对象列表均不可用')


def discover_related(url, output, stop=None, emit=None):
    # /* ===== 步骤1：读取餐厅页面；数据源：携程公开详情页；操作：1) 核验来源 2) 有界请求并支持取消 ===== */
    logger.info('开始读取餐厅相关对象')
    url = canonical_url(url)
    if not re.fullmatch(r'https://you\.ctrip\.com/yougourmet/restdetail/[a-zA-Z0-9]+/[1-9][0-9]*\.html', url):
        raise ValueError('需要餐饮 POI 详情页链接')
    stop = stop or Event()
    emit = emit or logger.info
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=True)
    report_path = folder / ('related-restaurant-' + uuid.uuid4().hex + '.json')
    report = {'source': {'kind': 'restaurant', 'url': url}, 'source_method': 'public_restaurant_page_recommendations',
              'reason': 'cancelled', 'error': '', 'complete': False, 'objects': [], 'report_file': str(report_path)}
    # 1.1 请求前检查取消；页面读取有超时，请求期间的取消会在读取返回后生效。
    try:
        if not stop.is_set():
            emit('正在读取餐厅详情页的相关对象')
            with urlopen(url, timeout=30) as response:
                if canonical_url(response.geturl()) != url:
                    raise ValueError('页面跳转到其他对象，已停止')
                raw = response.read(12_000_001)
            if not stop.is_set():
                if len(raw) > 12_000_000:
                    raise ValueError('页面超过读取上限')
                parser = NextData()
                parser.feed(raw.decode('utf-8'))
                if not parser.parts:
                    raise ValueError('页面没有公开对象状态')
                state = json.loads(''.join(parser.parts))['props']['pageProps']['initialState']
                found = extract_related(url, state, datetime.now(timezone.utc).isoformat())
                reason = 'recommendations_partial' if found['recommendations_status'] == 'partial' else 'recommendations_read'
                report.update(found, reason=reason)
                emit(f"发现 {len(report['objects'])} 个页面推荐对象")
    except RelatedListsUnavailable as exc:
        # 1.2 两类列表都不可用时保留各自状态，不能报告为空推荐成功。
        report.update(reason='error', error=str(exc), list_status=exc.statuses, recommendations_status='unavailable')
    except Exception as exc:
        report.update(reason='error', error=str(exc) or type(exc).__name__)
    # /* ===== 步骤2：保存报告；数据源：验证后的候选；操作：1) 唯一文件名 2) 完整写入后替换 ===== */
    logger.info('开始保存相关对象报告')
    temporary = report_path.with_name(report_path.name + '.tmp')
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(report_path)
    finally:
        temporary.unlink(missing_ok=True)
    logger.info('相关对象报告保存完成：%s', report['reason'])
    return report


def extract_related(source_url, state, captured_at):
    # /* ===== 步骤1：校验来源；数据源：餐饮详情状态；操作：1) 核验链接和对象 ID 2) 校验候选列表 ===== */
    logger.info('开始校验相关对象来源')
    url = canonical_url(source_url)
    match = re.fullmatch(r'https://you\.ctrip\.com/yougourmet/restdetail/[a-zA-Z0-9]+/([1-9][0-9]*)\.html', url)
    if not match or not isinstance(state, dict):
        raise ValueError('仅支持已验证的餐饮 POI 详情页')
    poi_id = int(match.group(1))
    detail = state.get('restaurantDetail')
    comment = state.get('commentInfo')
    info = comment.get('poiInfo') if isinstance(comment, dict) else None
    if not isinstance(detail, dict) or not isinstance(info, dict):
        raise ValueError('餐饮页面缺少对象身份')
    resource_id = info.get('resourceId')
    if (type(resource_id) is not int or resource_id <= 0 or info.get('resourceType') != 12
            or type(state.get('poiId')) is not int or state['poiId'] != poi_id
            or type(detail.get('poiId')) is not int or detail['poiId'] != poi_id
            or type(info.get('poiId')) is not int or info['poiId'] != poi_id):
        raise ValueError('餐饮页面的 POI 或点评身份不一致')
    if not isinstance(captured_at, str):
        raise ValueError('缺少采集时间')
    timestamp = datetime.fromisoformat(captured_at)
    if timestamp.utcoffset() is None:
        raise ValueError('采集时间必须包含时区')
    # 1.1 两个推荐源各自判断；缺失或格式错误不能阻断另一份有效列表。
    statuses = {key: ('missing' if key not in state else 'available' if isinstance(state[key], list) else 'invalid')
                for key in ('nearbyHotelsList', 'moreRestaurantList')}
    if 'available' not in statuses.values():
        raise RelatedListsUnavailable(statuses)
    hotels = state['nearbyHotelsList'] if statuses['nearbyHotelsList'] == 'available' else []
    restaurants = state['moreRestaurantList'] if statuses['moreRestaurantList'] == 'available' else []
    recommendations_status = 'available' if all(value == 'available' for value in statuses.values()) else 'partial'
    report = {'source': {'kind': 'restaurant', 'url': url, 'poi_id': poi_id,
                         'resource_id': resource_id, 'resource_type': 12},
              'captured_at': captured_at, 'source_method': 'public_restaurant_page_recommendations',
              'complete': False, 'objects': [], 'skipped_invalid': 0,
              'list_status': statuses, 'recommendations_status': recommendations_status,
              'skipped_duplicate': 0, 'skipped_self': 0,
              'limitations': ['页面推荐不是城市目录或全部对象。',
                              '餐厅候选只有 POI，不等于点评业务 ID；缺少详情链接。',
                              '价格是页面显示值，币种和具体适用条件尚未核实。']}
    if recommendations_status == 'partial':
        report['limitations'].append('部分推荐列表缺失或格式不正确；未把不可用列表当作空列表。')
    logger.info('相关对象来源校验完成')
    # /* ===== 步骤2：提取候选；数据源：两个推荐列表；操作：1) 分别校验身份 2) 保留白名单字段 ===== */
    logger.info('开始提取页面相关对象')
    seen = set()
    # 2.1 酒店 ID 只从页面给出的携程酒店链接读取，不把零 POI 当酒店 ID。
    for row in hotels:
        try:
            if not isinstance(row, dict) or not isinstance(row.get('url'), str):
                raise ValueError('无酒店链接')
            hotel_url = canonical_url(row['url'])
            hotel_match = re.fullmatch(r'https://hotels\.ctrip\.com/hotels/([1-9][0-9]*)\.html', hotel_url)
            if not hotel_match:
                raise ValueError('无效酒店链接')
            hotel_id = int(hotel_match.group(1))
        except ValueError:
            report['skipped_invalid'] += 1
            continue
        key = ('hotel', hotel_id)
        if key in seen:
            report['skipped_duplicate'] += 1
            continue
        seen.add(key)
        entry = {'kind': 'hotel', 'relation': 'nearby_hotels', 'hotel_id': hotel_id, 'url': hotel_url}
        for field in ('name', 'distance', 'commentScore', 'commentCount', 'price'):
            value = row.get(field)
            if type(value) in (str, int, float):
                entry[field] = value
        report['objects'].append(entry)
    # 2.2 餐厅推荐只有 POI ID；不凭来源城市或 POI 猜测链接和点评业务 ID。
    for row in restaurants:
        if not isinstance(row, dict) or type(row.get('poiId')) is not int or row['poiId'] <= 0:
            report['skipped_invalid'] += 1
            continue
        identity = row['poiId']
        if identity == poi_id:
            report['skipped_self'] += 1
            continue
        key = ('restaurant', identity)
        if key in seen:
            report['skipped_duplicate'] += 1
            continue
        seen.add(key)
        entry = {'kind': 'restaurant', 'relation': 'more_restaurants', 'poi_id': identity}
        for field in ('name', 'distance', 'commentScore', 'commentCount', 'averagePrice'):
            value = row.get(field)
            if type(value) in (str, int, float):
                entry[field] = value
        report['objects'].append(entry)
    logger.info('相关对象提取完成：%s 个候选', len(report['objects']))
    return report
