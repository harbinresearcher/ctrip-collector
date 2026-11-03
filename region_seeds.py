"""从携程公开首页提取待核验目的地，不宣称覆盖全部地区。"""
import json
import logging
import re
import uuid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)
SOURCE = 'https://you.ctrip.com/'
HOTEL_SOURCE = 'https://hotels.ctrip.com/jiudian/'


class DestinationLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.regions = {}

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        href = dict(attrs).get('href', '')
        try:
            parsed = urlsplit(href)
            if parsed.scheme and (parsed.scheme != 'https' or parsed.netloc != 'you.ctrip.com'):
                return
            if not parsed.scheme and parsed.netloc:
                return
            match = re.fullmatch(r'/place/([a-zA-Z]*)(\d+)\.html', parsed.path)
            if match and not parsed.query and not parsed.fragment and int(match.group(2)) > 0:
                self.regions.setdefault(int(match.group(2)), match.group(1) + match.group(2))
        except ValueError:
            return


class HotelCityLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.regions = {}

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        try:
            parsed = urlsplit(dict(attrs).get('href', ''))
            if parsed.scheme and (parsed.scheme != 'https' or parsed.netloc != 'hotels.ctrip.com'):
                return
            if not parsed.scheme and parsed.netloc:
                return
            match = re.fullmatch(r'/hotel/([a-zA-Z]+)([1-9]\d*)/?', parsed.path)
            if match and not parsed.query and not parsed.fragment:
                self.regions.setdefault(int(match.group(2)), match.group(1) + match.group(2))
        except ValueError:
            return


def discover_regions(output, kind, max_regions=50, *, html=None):
    # /* ===== 步骤1：读取公开首页；数据源：携程目的地链接；操作：1) 限制响应大小 2) 验证地区格式 ===== */
    logger.info('开始读取目的地入口')
    if kind not in ('hotel', 'sight', 'restaurant', 'shopping') or max_regions < 1:
        raise ValueError('地区类型或数量无效')
    # 1.1 只读取公开首页，来源列表并不代表全球所有地区。
    if html is None:
        request=Request(HOTEL_SOURCE if kind == 'hotel' else SOURCE,headers={'User-Agent':'Mozilla/5.0'})
        with urlopen(request,timeout=30) as response:
            raw=response.read(12_000_001)
        if len(raw)>12_000_000:
            raise ValueError('目的地首页超过大小限制')
        html=raw.decode('utf-8')
    parser=HotelCityLinks() if kind == 'hotel' else DestinationLinks()
    parser.feed(html)
    if not parser.regions:
        raise ValueError('首页没有可识别目的地，不能当作空结果')
    logger.info('目的地入口读取完成：%s个',len(parser.regions))
    # /* ===== 步骤2：保存候选清单；数据源：页面地区ID；操作：1) 生成相应业务入口 2) 标明尚未核验 ===== */
    logger.info('开始保存目的地候选')
    regions=list(parser.regions.items())[:max_regions]
    # 2.1 生成的业务路径需要后续目录适配器逐地区核验，不声明对象已经采集。
    if kind == 'hotel':
        entries=[f'https://hotels.ctrip.com/hotel/{slug}' for _,slug in regions]
    elif kind == 'sight':
        entries=[f'https://you.ctrip.com/sight/{slug}/s0-p1.html' for _,slug in regions]
    elif kind == 'restaurant':
        entries=[f'https://you.ctrip.com/yougourmet/restaurantlist/{slug}.html' for _,slug in regions]
    else:
        entries=[str(identity) for identity,_ in regions]
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    stem=output/('region-seeds-'+uuid.uuid4().hex)
    report={'source_url':HOTEL_SOURCE if kind == 'hotel' else SOURCE,'kind':kind,'total_visible':len(parser.regions),'entries':entries,
            'entries_verified':False,'complete':False,
            'reason':'region_budget_reached' if len(entries)<len(parser.regions) else 'homepage_exhausted',
            'links_file':str(stem.with_suffix('.txt')),'report_file':str(stem.with_suffix('.json'))}
    Path(report['links_file']).write_text(''.join(entry+'\n' for entry in entries),encoding='utf-8')
    Path(report['report_file']).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    logger.info('目的地候选保存完成：%s个',len(entries))
    return report
