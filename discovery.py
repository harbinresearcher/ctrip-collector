"""有界城市景点目录发现；不把搜索结果数量当作城市全量。"""
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from urllib.request import urlopen

from network import NextData, canonical_url, sanitize

logger = logging.getLogger(__name__)


def discover_restaurants(url, output, max_pages=5, max_objects=50, stop=None, fetch=None, delay=1.5):
    # /* ===== 步骤1：校验餐饮目录；数据源：目录链接；操作：1) 地区限定 2) 有界发现 ===== */
    logger.info('开始校验餐饮目录')
    match = re.fullmatch(r'https://you\.ctrip\.com/yougourmet/restaurantlist/([a-zA-Z]*)(\d+)\.html', url.strip())
    if not match or max_pages < 1 or max_objects < 1:
        raise ValueError('需要餐饮城市目录，如 https://you.ctrip.com/yougourmet/restaurantlist/fusong2479.html')
    district = int(match.group(2))
    city = match.group(1) + match.group(2)
    stop = stop or Event()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    stem = output / ('discovery-restaurant-' + str(district) + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    report = {'kind': 'restaurant', 'district_id': district, 'source_url': url, 'objects': [],
              'pages': 0, 'listing_total': None, 'complete': False, 'reason': 'page_budget_reached',
              'error': '', 'skipped_invalid': 0, 'links_file': str(stem.with_suffix('.txt'))}
    seen, repeated = set(), 0
    logger.info('餐饮目录校验完成')
    # /* ===== 步骤2：读取目录；数据源：公开页面状态；操作：1) 校验分页 2) POI去重并留来源 ===== */
    logger.info('开始读取餐饮目录')
    try:
        for index in range(1, max_pages + 1):
            if stop.is_set():
                report['reason'] = 'cancelled'
                break
            page_url = f'https://you.ctrip.com/yougourmet/restaurantlist/{city}/list-p{index}.html'
            if fetch:
                html = fetch(index)
            else:
                with urlopen(page_url, timeout=30) as response:
                    html = response.read(12_000_000).decode('utf-8')
            parser = NextData()
            parser.feed(html)
            state = json.loads(''.join(parser.parts))['props']['pageProps']['initialState']
            # 2.1 校验页面路由；官方推荐列表不是正式目录数据。
            if int(state['districtId']) != district or int(state['urlParam'].get('p', 1)) != index:
                raise ValueError('餐饮目录地区或页码不匹配')
            listing = state['listPageData']
            # 2.1.1 失败或缺失的接口状态不能被解释为正常末页。
            status = listing.get('ResponseStatus') or {}
            if status.get('Ack') != 'Success' or status.get('Errors'):
                raise ValueError('餐饮目录接口未确认成功，不能判定末页')
            items = listing['restaurants']
            if not isinstance(items, list):
                raise ValueError('餐饮目录结构变化')
            report['pages'] += 1
            report['listing_total'] = listing.get('total')
            before = len(seen)
            for item in items:
                try:
                    identity = int(item['poiId'])
                    if identity <= 0:
                        raise ValueError('无效POI')
                except (KeyError, TypeError, ValueError):
                    report['skipped_invalid'] += 1
                    continue
                if identity in seen:
                    continue
                seen.add(identity)
                report['objects'].append({'kind': 'restaurant', 'poi_id': identity,
                    'name': item.get('name'), 'comment_count': item.get('commentCount'),
                    'district_id': district, 'district_source': 'listing_scope',
                    'url': f'https://you.ctrip.com/yougourmet/restdetail/{city}/{identity}.html',
                    'discovered_on': page_url, 'listing_fields': sanitize(item)})
                if len(seen) >= max_objects:
                    report['reason'] = 'object_budget_reached'
                    break
            repeated = repeated + 1 if len(seen) == before else 0
            # 2.2 总量仅作参考，不据此声称全量。
            stem.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            if report['reason'] == 'object_budget_reached':
                break
            if not items:
                report['reason'] = 'listing_end'
                break
            if repeated >= 2:
                report['reason'] = 'repeated_pages'
                break
            if index < max_pages and stop.wait(delay):
                report['reason'] = 'cancelled'
                break
    except Exception as exc:
        report['reason'], report['error'] = 'error', str(exc)
    finally:
        stem.with_suffix('.txt').write_text(''.join(x['url'] + '\n' for x in report['objects']), encoding='utf-8')
        stem.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        logger.info('餐饮目录读取结束：%s', report['reason'])
    return report


def discover_objects(url, *args, **kwargs):
    # /* ===== 步骤1：选择目录适配；数据源：入口链接；操作：1) 餐饮路由 2) 景点保留原校验 ===== */
    logger.info('开始选择目录适配')
    collector = discover_restaurants if '/yougourmet/restaurantlist/' in url else discover_sights
    logger.info('目录适配选择完成')
    return collector(url, *args, **kwargs)


def discover_sights(url, output, max_pages=5, max_objects=50, stop=None, fetch=None, delay=0.8):
    # /* ===== 步骤1：校验目录入口；数据源：用户链接；操作：1) 限定已验证路由 2) 设置页数和对象预算 ===== */
    logger.info('开始校验城市目录')
    match = re.fullmatch(r'https://you\.ctrip\.com/sight/([a-zA-Z]*)(\d+)/s0-p1\.html', url.strip())
    if not match or max_pages < 1 or max_objects < 1:
        raise ValueError('需要城市景点第一页链接，如 https://you.ctrip.com/sight/fusong2479/s0-p1.html；预算必须为正数。')
    district = int(match.group(2))
    prefix = url.strip().rsplit('s0-p1.html', 1)[0]
    stop = stop or Event()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    stem = output / f'discovery-sight-{district}-{stamp}'
    report = {'district_id': district, 'source_url': url, 'objects': [], 'pages': 0,
              'skipped_nearby': 0, 'skipped_invalid': 0, 'complete': False,
              'reason': 'page_budget_reached', 'error': '', 'listing_total': None,
              'max_pages': max_pages, 'max_objects': max_objects,
              'links_file': str(stem.with_suffix('.txt'))}
    seen, repeated = set(), 0
    logger.info('城市目录校验完成')
    # /* ===== 步骤2：发现点评对象；数据源：列表结构化状态；操作：1) 校验页码 2) 排除邻城 3) 逐页留清单 ===== */
    logger.info('开始读取城市目录')
    try:
        for index in range(1, max_pages + 1):
            if stop.is_set():
                report['reason'] = 'cancelled'
                break
            page_url = prefix + f's0-p{index}.html'
            # 2.1 不从页面底部推荐链接挖对象，只使用正式列表卡片。
            if fetch:
                html = fetch(index)
            else:
                with urlopen(page_url, timeout=30) as response:
                    html = response.read(12_000_000).decode('utf-8')
            parser = NextData()
            parser.feed(html)
            state = json.loads(''.join(parser.parts))['props']['pageProps']['initialState']
            listing = state['listInitData']
            if int(state['urlParams']['page']) != index or int(listing['districtId']) != district:
                raise ValueError('返回页码或地区不符，拒绝继续遍历')
            report['pages'] += 1
            report['listing_total'] = listing.get('totalCount')
            items = listing['attractionList']
            if not isinstance(items, list):
                raise ValueError('目录结构变化，已停止')
            before = len(seen)
            # 2.2 保留有点评和暂无点评对象，后者可由调用方选择是否采集。
            for item in items:
                card = item.get('card') or {}
                if card.get('districtId') != district:
                    report['skipped_nearby'] += 1
                    continue
                try:
                    identity = int(card['businessId'])
                    detail = canonical_url(card['detailUrl'])
                    if int(detail.rsplit('/', 1)[-1].split('.')[0]) != identity:
                        raise ValueError('目录 ID 与链接不符')
                except (KeyError, TypeError, ValueError):
                    report['skipped_invalid'] += 1
                    continue
                if identity in seen:
                    continue
                seen.add(identity)
                report['objects'].append({'kind': 'sight', 'resource_id': identity,
                                          'poi_id': card.get('poiId'), 'name': card.get('poiName'),
                                          'district_id': district, 'comment_count': card.get('commentCount'),
                                          'url': detail, 'discovered_on': page_url})
                if len(seen) >= max_objects:
                    report['reason'] = 'object_budget_reached'
                    break
            repeated = repeated + 1 if before == len(seen) else 0
            logger.info('目录第 %s 页，发现 %s 个本地对象', index, len(seen))
            stem.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            if report['reason'] == 'object_budget_reached':
                break
            if not items or listing.get('hasMore') is False:
                report['reason'] = 'listing_end'
                break
            if repeated >= 2:
                report['reason'] = 'repeated_pages'
                break
            if stop.wait(delay):
                report['reason'] = 'cancelled'
                break
    except Exception as exc:
        report['reason'], report['error'] = 'error', str(exc)
    finally:
        # 2.3 目录抓取结束与全站覆盖是两个概念，始终记录 complete=false。
        stem.with_suffix('.txt').write_text(''.join(obj['url'] + '\n' for obj in report['objects']), encoding='utf-8')
        stem.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        logger.info('城市目录读取结束：%s', report['reason'])
    return report
