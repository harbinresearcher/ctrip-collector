"""酒店公开 HTML 快照：只解析 JSON 字面量，不执行网页脚本。"""
import json
import logging
import re

logger = logging.getLogger(__name__)


def _find_props(value):
    # 1.1 在已解码 JSON 内寻找酒店状态，不读取全局 JS 或认证存储。
    if isinstance(value, dict):
        if isinstance(value.get('hotelDetailResponse'), dict):
            return value
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = _find_props(child)
        if found is not None:
            return found
    return None


def parse_public_hotel(url, html):
    # /* ===== 步骤1：解码公开页面；数据源：HTML 内 Flight 字面量；操作：1) 拼接字符串 2) 解析 JSON 记录 ===== */
    logger.info('开始解析酒店公开页面')
    from network import NextData, canonical_url, sanitize
    url = canonical_url(url)
    if not re.fullmatch(r'https://hotels\.ctrip\.com/hotels/\d+\.html', url):
        raise ValueError('不是已验证的酒店详情地址')
    parser = NextData()
    parser.feed(html)
    props = _find_props(json.loads(''.join(parser.parts))) if parser.parts else None
    if props is None:
        chunks = []
        decoder = json.JSONDecoder()
        # 1.2 raw_decode 只接收 JSON，不使用 eval 执行远端文本。
        for match in re.finditer(r'self\.__next_f\.push\(', html):
            try:
                value, _ = decoder.raw_decode(html[match.end():])
            except ValueError:
                continue
            if isinstance(value, list) and len(value) > 1 and value[0] == 1 and isinstance(value[1], str):
                chunks.append(value[1])
        for line in ''.join(chunks).splitlines():
            if not re.match(r'J[0-9a-f]+:', line):
                continue
            try:
                props = _find_props(json.loads(line.split(':', 1)[1]))
            except ValueError:
                continue
            if props is not None:
                break
    if props is None:
        raise ValueError('没有可识别的酒店公开状态；已停止，未执行页面脚本')
    logger.info('酒店公开状态解码完成')
    # /* ===== 步骤2：保存公开字段；数据源：酒店详情与页面样本；操作：1) 校验对象 2) 排除认证 3) 区分样本与分页 ===== */
    logger.info('开始整理酒店公开字段')
    detail = props['hotelDetailResponse']
    identity = int(url.rsplit('/', 1)[-1].split('.')[0])
    base = detail['hotelBaseInfo']
    if int(base['masterHotelId']) != identity:
        raise ValueError('酒店链接和页面对象 ID 不一致')
    comments = props.get('hotelCommentResponse') or {}
    records = []
    # 2.1 列表原字段保留，增加统一列；入住日期不冒充发布日期。
    for group in comments.get('groupList', []):
        for original in group.get('commentList', []):
            if not original.get('id') or str(original.get('hotelId', identity)) != str(identity):
                raise ValueError('页面样本评论缺 ID 或属于其他酒店')
            record = sanitize(original)
            record.update({'commentId': original['id'], 'score': original.get('rating'),
                           'scores': sanitize(original.get('ratingInfo')), 'publishTime': original.get('createDate'),
                           'images': sanitize(original.get('imageList', [])), 'videos': sanitize(original.get('videoList', [])),
                           'replyInfo': sanitize(original.get('feedbackList', [])),
                           'ipLocatedName': original.get('ipLocation'), 'source_schema': 'hotel_public_html'})
            records.append(record)
    public_keys = ('hotelBaseInfo', 'hotelPositionInfo', 'hotelDescriptionInfo', 'hotelImagePop',
                   'hotelTopImage', 'hotelFacilityBelt', 'hotelFacilityPop', 'hotelFacilityPopV2',
                   'hotelPolicyInfo', 'hotelRoomBedInfo', 'hotelComment')
    target = {'kind': 'hotel', 'url': url, 'resource_id': identity, 'resource_type': None, 'poi_id': None,
              'name': base['nameInfo']['name'], 'display_total': comments.get('totalCount'),
              'details': {key: sanitize(detail[key]) for key in public_keys if key in detail},
              'comment_info': sanitize({k: comments[k] for k in ('commentRating', 'totalCountForPage', 'commentTagList',
                                        'travelTypeList', 'roomList', 'filteredOutCommentInfo', 'staticInfo') if k in comments}),
              'source_method': 'hotel_public_html_snapshot', 'snapshot_comments': records,
              'ai_summary': {'status': 'not_captured', 'content': None}}
    logger.info('酒店公开字段整理完成：%s，页面样本 %s 条', target['name'], len(records))
    return target
