"""保留购物对象公开点评概览中的评分维度和评论样本。"""
import logging
import math
import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from network import canonical_url, sanitize

logger = logging.getLogger(__name__)
PRIVATE_FIELDS = {'author', 'nickname', 'usernick', 'avatar', 'userimage', 'phone',
                  'mobile', 'email', 'realname', 'idcard', 'idnumber', 'passport'}


def clean_sample(value):
    # /* ===== 步骤1：清理页面样本；数据源：公开点评字段；操作：1) 删除个人字段 2) 保留可研究内容 ===== */
    logger.info('开始清理购物点评样本')
    # 1.1 凭据及内部标识沿用采集器的清理规则，作者信息不写入该快照。
    def remove_private(item):
        # 1.2 只清理一次凭据，再递归删除作者字段，避免重复遍历完整子树。
        if isinstance(item, dict):
            return {key: remove_private(child) for key, child in item.items() if key.lower() not in PRIVATE_FIELDS}
        if isinstance(item, list):
            return [remove_private(child) for child in item]
        return item

    result = remove_private(sanitize(value))
    logger.info('购物点评样本清理完成')
    return result


def extract_shopping_review_snapshot(url, overview, comments, requested_poi_id, captured_at):
    # /* ===== 步骤1：核验购物对象；数据源：概览与点评入口；操作：1) 核对业务和 POI 2) 确认响应成功 ===== */
    logger.info('开始核验购物点评快照')
    url = canonical_url(url)
    match = re.fullmatch(r'https://you\.ctrip\.com/shopping/[a-zA-Z0-9]+/([1-9][0-9]*)\.html', url)
    info = overview.get('PoiOverviewInfo') if isinstance(overview, dict) else None
    status = comments.get('ResponseStatus') if isinstance(comments, dict) else None
    if not match or not isinstance(info, dict) or not isinstance(status, dict):
        raise ValueError('购物概览或点评响应不完整')
    business_id = int(match.group(1))
    if (overview.get('ResultCode') != 0 or comments.get('ResultCode') != 0
            or status.get('Ack') != 'Success' or status.get('Errors')
            or type(info.get('BusinessId')) is not int or info['BusinessId'] != business_id
            or type(info.get('PoiId')) is not int or info['PoiId'] < 1
            or type(requested_poi_id) is not int or requested_poi_id != info['PoiId']):
        raise ValueError('购物对象、请求 POI 或接口状态不一致')
    try:
        timestamp = datetime.fromisoformat(captured_at)
        if timestamp.utcoffset() is None:
            raise ValueError('missing timezone')
    except (TypeError, ValueError):
        raise ValueError('采集时间缺少有效时区') from None
    detail = comments.get('commentDetail')
    if not isinstance(detail, dict):
        raise ValueError('购物点评概览不可用')
    total = detail.get('totalCount')
    if total is not None and (type(total) is not int or total < 0):
        raise ValueError('购物点评显示数量无效')
    entry = detail.get('moreUrl')
    if entry is not None and not isinstance(entry, str):
        raise ValueError('购物点评入口格式无效')
    query = parse_qs(urlparse(entry or '').query)
    resource_type = None
    if query:
        raw_type = query.get('businessType', [])
        if query.get('businessId') != [str(business_id)] or len(raw_type) != 1 or not re.fullmatch(r'[1-9][0-9]*', raw_type[0]):
            raise ValueError('购物点评入口与业务对象不一致')
        resource_type = int(raw_type[0])
    elif total != 0:
        raise ValueError('未取得购物点评业务入口')
    logger.info('购物点评快照身份核验完成')
    # /* ===== 步骤2：留存评分及样本；数据源：点评概览；操作：1) 标明字段状态 2) 清理个人信息 ===== */
    logger.info('开始提取购物点评概览')
    def field_list(key):
        # 2.1 缺失、空值、类型错误分别记状态，不将其当成完整空列表。
        if key not in detail:
            return 'missing', []
        value = detail[key]
        if value is None:
            return 'null', []
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            return 'invalid', []
        return 'available', clean_sample(value)

    score_status, scores = field_list('scoreList')
    sample_status, samples = field_list('commentList')
    # 2.2 缺失、空值和异常均保留状态；不把无法识别的结构当成分数。
    avg = detail.get('avgScore')
    avg_status = ('missing' if 'avgScore' not in detail else 'null' if avg is None else
                  'available' if type(avg) is int or (type(avg) is float and math.isfinite(avg)) else 'invalid')
    result = {'kind':'shopping','url':url,'resource_id':business_id,'resource_type':resource_type,
              'poi_id':requested_poi_id,'captured_at':captured_at,
              'source_method':'public_shopping_review_overview','display_total':total,
              'average_score':avg if avg_status == 'available' else None, 'average_score_status':avg_status,
              'score_dimensions_status':score_status,
              'score_dimensions':scores, 'comment_samples_status':sample_status, 'comment_samples':samples,
              'complete':False,
              'identity_verification':'request_poi_and_comment_entry' if resource_type else 'request_poi_no_comment_entry',
              'limitations':['概览中的评论为页面样本，不代表全部点评。',
                             '作者等个人字段已从样本中移除。']}
    logger.info('购物点评概览提取完成：%s 条样本', len(samples))
    return result
