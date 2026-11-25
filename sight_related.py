"""发现景点页面所列的景点、餐厅和购物候选。"""
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from network import NextData, canonical_url

logger = logging.getLogger(__name__)
RELATIONS = {'sightRecommend': 'sight', 'restaurantRecommend': 'restaurant',
             'nearbySight': 'sight', 'nearbyRestaurant': 'restaurant', 'nearbyShop': 'shopping'}
KINDS = {'sight': 'sight', 'food': 'restaurant', 'shopping': 'shopping'}


class SameSightRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 1.1 跳转前核对完整对象链接，拒绝其他对象和外部站点。
        if canonical_url(newurl) != canonical_url(req.full_url):
            raise ValueError('景点页面跳转至其他对象')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def extract_sight_related(url, state, captured_at):
    # /* ===== 步骤1：核验来源；数据源：公开景点页面；操作：1) 核对多处对象身份 2) 标记各推荐列表 ===== */
    logger.info('开始核验景点推荐来源')
    url = canonical_url(url)
    match = re.fullmatch(r'https://you\.ctrip\.com/sight/[a-zA-Z0-9]+/([1-9][0-9]*)\.html', url)
    if not match or not isinstance(state, dict):
        raise ValueError('需要景点详情页和公开页面状态')
    detail, info = state.get('poiDetail'), state.get('poiCommentInfo')
    if not isinstance(detail, dict) or not isinstance(info, dict):
        raise ValueError('景点页面缺少身份信息')
    identity = int(match.group(1))
    status = detail.get('ResponseStatus')
    if (not isinstance(status, dict) or status.get('Ack') != 'Success' or status.get('Errors')
            or detail.get('result') != 0 or type(detail.get('businessId')) is not int
            or detail['businessId'] != identity or type(info.get('resourceId')) is not int
            or info['resourceId'] != identity or type(detail.get('poiId')) is not int
            or detail['poiId'] <= 0 or type(info.get('poiId')) is not int
            or info['poiId'] != detail['poiId'] or type(detail.get('poiType')) is not int
            or type(info.get('resourceType')) is not int):
        raise ValueError('景点链接、详情或点评身份不一致')
    try:
        timestamp = datetime.fromisoformat(captured_at)
        if timestamp.utcoffset() is None:
            raise ValueError('missing timezone')
    except (TypeError, ValueError):
        raise ValueError('采集时间缺少有效时区') from None
    # 1.2 独立记录每组列表；缺失和格式错误均不当成空列表。
    groups = {}
    for key in ('sightRecommend', 'restaurantRecommend'):
        section = detail.get(key)
        groups[key] = ('missing', []) if key not in detail else (
            ('available', section['itemList']) if isinstance(section, dict)
            and isinstance(section.get('itemList'), list) else ('invalid', []))
    modules = detail.get('subModuleList')
    for key in ('nearbySight', 'nearbyRestaurant', 'nearbyShop'):
        if 'subModuleList' not in detail:
            groups[key] = ('missing', [])
        elif not isinstance(modules, list):
            groups[key] = ('invalid', [])
        else:
            found = [m for m in modules if isinstance(m, dict) and m.get('subType') == key]
            groups[key] = (('missing', []) if not found else
                           ('available', found[0]['itemList']) if len(found) == 1
                           and isinstance(found[0].get('itemList'), list) else ('invalid', []))
    states = {key: value[0] for key, value in groups.items()}
    if 'available' not in states.values():
        raise ValueError('页面没有可用的推荐列表')
    # 1.1.1 详情 POI 类型与点评资源类型分别留存，不能假定两者相同。
    report = {'source': {'kind': 'sight', 'url': url, 'resource_id': identity,
                         'poi_id': detail['poiId'], 'detail_poi_type': detail['poiType'],
                         'resource_type': info['resourceType']},
              'source_method': 'public_sight_page_recommendations',
              'captured_at': captured_at, 'source_channel': 'public_sight_detail',
              'complete': False, 'recommendations_status': 'available' if all(v == 'available' for v in states.values()) else 'partial',
              'list_status': states, 'objects': [], 'skipped_invalid': 0, 'skipped_self': 0, 'skipped_conflict': 0,
              'limitations': ['页面推荐和附近列表不代表全部对象。',
                              '推荐链接和 POI 仅来自来源页面，候选详情身份尚未核验。',
                              '价格为页面显示值，币种和条件未核实。']}
    logger.info('景点推荐来源核验完成')

    # /* ===== 步骤2：提取关联对象；数据源：五类页面列表；操作：1) 验证链接类型 2) 合并关系并保留观察 ===== */
    logger.info('开始提取景点推荐对象')
    seen = {}
    for relation, (_, rows) in groups.items():
        for row in rows:
            # 2.1 仅接受来源页明确给出的携程对象链接，资源 ID 不与 POI 混淆。
            try:
                if not isinstance(row, dict) or type(row.get('poiId')) is not int or row['poiId'] < 1:
                    raise ValueError('推荐缺少 POI')
                if not isinstance(row.get('jumpUrl'), str):
                    raise ValueError('推荐缺少链接')
                candidate = canonical_url(row['jumpUrl'])
                link = re.fullmatch(r'https://you\.ctrip\.com/(sight|food|shopping)/[a-zA-Z0-9]+/([1-9][0-9]*)\.html', candidate)
                if not link or KINDS[link.group(1)] != RELATIONS[relation]:
                    raise ValueError('推荐对象类型或链接不符')
                kind, resource_id = KINDS[link.group(1)], int(link.group(2))
            except ValueError:
                report['skipped_invalid'] += 1
                continue
            if kind == 'sight' and (resource_id == identity or row['poiId'] == detail['poiId']):
                report['skipped_self'] += 1
                continue
            key = (kind, resource_id)
            item = seen.get(key)
            if item is not None and item['poi_id'] != row['poiId']:
                report['skipped_conflict'] += 1
                continue
            if item is None:
                item = {'kind': kind, 'resource_id': resource_id, 'poi_id': row['poiId'],
                        'url': candidate, 'identity_status': 'page_link_only', 'observations': []}
                seen[key] = item
                report['objects'].append(item)
            # 2.2 白名单保存页面展示字段，关系间的信息不相互覆盖。
            observation = {'relation': relation, 'url': candidate}
            for field in ('name', 'score', 'commentCount', 'distanceDesc', 'price'):
                if type(row.get(field)) in (str, int, float):
                    observation[field] = row[field]
            if isinstance(row.get('tags'), list) and all(isinstance(tag, str) for tag in row['tags']):
                observation['tags'] = row['tags']
            image = row.get('image')
            if isinstance(image, str):
                try:
                    parts = urlsplit(image)
                    host = (parts.hostname or '').lower()
                    if parts.scheme == 'https' and (host == 'c-ctrip.com' or host.endswith('.c-ctrip.com')):
                        observation['image'] = parts._replace(query='', fragment='').geturl()
                except ValueError:
                    # 2.3 图片地址损坏时仍保留候选对象及其余可用字段。
                    logger.info('推荐对象图片地址无效')
            item['observations'].append(observation)
            if 'name' not in item and isinstance(row.get('name'), str):
                item['name'] = row['name']
    logger.info('景点推荐对象提取完成：%s 个', len(report['objects']))
    return report


def discover_sight_related(url, output, stop=None, emit=None):
    # /* ===== 步骤1：读取景点页面；数据源：携程公开详情；操作：1) 校验链接与跳转 2) 解析并限量读取 ===== */
    logger.info('开始发现景点推荐对象')
    url = canonical_url(url)
    if not re.fullmatch(r'https://you\.ctrip\.com/sight/[a-zA-Z0-9]+/[1-9][0-9]*\.html', url):
        raise ValueError('需要景点详情链接')
    stop, emit = stop or Event(), emit or logger.info
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ('related-sight-' + uuid.uuid4().hex + '.json')
    report = {'source': {'kind': 'sight', 'url': url}, 'source_method': 'public_sight_page_recommendations',
              'reason': 'cancelled', 'error': '', 'complete': False, 'objects': [], 'report_file': str(path)}
    try:
        if not stop.is_set():
            emit('正在读取景点页面的推荐对象')
            with build_opener(SameSightRedirect()).open(url, timeout=30) as response:
                if canonical_url(response.geturl()) != url:
                    raise ValueError('景点页面跳转至其他对象')
                raw = response.read(12_000_001)
            if not stop.is_set():
                if len(raw) > 12_000_000:
                    raise ValueError('景点页面超过读取上限')
                parser = NextData()
                parser.feed(raw.decode('utf-8'))
                if not parser.parts:
                    raise ValueError('页面没有公开对象状态')
                state = json.loads(''.join(parser.parts))['props']['pageProps']['initialState']
                captured_at = datetime.now(timezone(timedelta(hours=8))).isoformat()
                report.update(extract_sight_related(url, state, captured_at))
                report['source_raw_hash'] = hashlib.sha256(raw).hexdigest()
                report['reason'] = 'recommendations_partial' if report['recommendations_status'] == 'partial' else 'recommendations_read'
                emit('发现 %s 个景点推荐对象' % len(report['objects']))
    except Exception as exc:
        report.update(reason='error', error=str(exc) or type(exc).__name__)
    logger.info('景点推荐对象发现结束：%s', report['reason'])
    # /* ===== 步骤2：保存报告；数据源：已验证页面；操作：1) 唯一临时文件 2) 原子替换 ===== */
    logger.info('开始保存景点推荐报告')
    temporary = path.with_name(path.name + '.tmp')
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    logger.info('景点推荐报告保存完成')
    return report
