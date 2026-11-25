"""Save selected public POI fields separately from review pagination."""
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path

from shopping_snapshot import clean_sample


def number(value):
    return value if type(value) is int or (type(value) is float and math.isfinite(value)) else None


def caption(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def listing(value):
    return value if isinstance(value, list) else []


def review_tags(value):
    return [{'label': label, 'display_count': number(entry.get('commentCount'))}
            for entry in listing(value) if isinstance(entry, dict) and (label := caption(entry.get('tagContent')))]


def opening_rules(value):
    rules = []
    for entry in listing(value):
        if not isinstance(entry, dict):
            continue
        period = caption(entry.get('dateDesc'))
        hours = [label for day in listing(entry.get('openWeekRuleInfo'))
                 if isinstance(day, dict) and (label := caption(day.get('weekDesc')))]
        if period and hours:
            rules.append({'period_text': period, 'hours_texts': hours})
    return rules


def project_profile(target, url):
    kind = target.get('kind')
    expected = 'shopping' if '/shopping/' in url else 'sight' if '/sight/' in url else 'restaurant'
    if kind != expected or target.get('url') != url:
        raise ValueError('返回的公开详情与所选对象不一致')
    details = target.get('details') or {}
    profile = {'kind': kind, 'url': url, 'name': caption(target.get('name')),
               'resource_id': target['resource_id'], 'poi_id': target.get('poi_id'),
               'resource_type': target.get('resource_type'),
               'captured_at': datetime.now(timezone.utc).isoformat(), 'complete': False,
               'display_total': number(target.get('display_total'))}
    if kind == 'shopping':
        snapshot = target.get('shopping_review_overview') or {}
        profile.update(overview_status=target.get('shopping_review_overview_status', 'unavailable'),
                       average_score=number(snapshot.get('average_score')),
                       overview_display_total=number(snapshot.get('display_total')),
                       score_dimensions_status=snapshot.get('score_dimensions_status', 'missing'),
                       comment_samples_status=snapshot.get('comment_samples_status', 'missing'),
                       comment_samples=clean_sample(listing(snapshot.get('comment_samples'))))
        dimensions = snapshot.get('score_dimensions')
    elif kind == 'sight':
        info = target.get('comment_info') or {}
        opening = details.get('openInfo')
        opening = opening if isinstance(opening, dict) else {}
        profile.update(average_score=number(details.get('commentScore')),
                       address=caption(details.get('address')),
                       district_name=caption(details.get('districtName')),
                       ranking_label=caption(details.get('rankingDesc')),
                       opening_text=caption(opening.get('openTime')),
                       opening_status_text=caption(opening.get('openStatus')),
                       next_opening_text=caption(opening.get('latelyOpenTime')),
                       visit_duration_text=caption(opening.get('playSpendTime')),
                       opening_time_rules=opening_rules(opening.get('openDateRuleInfo')),
                       admission_policies=[{'group': group, 'description': description}
                                           for rule in listing(details.get('policyInfoList'))
                                           if (isinstance(rule, dict) and (group := caption(rule.get('name')))
                                               and (description := caption(rule.get('policyDesc'))))],
                       review_filters=review_tags(info.get('commentHotTags')),
                       review_topic_tags=review_tags(info.get('subCommentHotTags')),
                       review_topic_scope='public_page_tag_labels_only')
        dimensions = info.get('scores')
    else:
        info = target.get('comment_info') or {}
        profile.update(average_score=number(details.get('commentScore')),
                       opening_text=caption(details.get('openTime')),
                       address=caption(details.get('address')),
                       introduction=caption(details.get('introduction')),
                       average_price_display=number(details.get('averagePrice')),
                       cuisines=[label for entry in listing(details.get('cuisines'))
                                 if isinstance(entry, dict) and (label := caption(entry.get('name')))],
                       featured_dishes=[label for entry in listing(details.get('foods')) if (label := caption(entry))],
                       review_filters=review_tags(info.get('commentHotTags')))
        dimensions = info.get('scores')
    profile['score_dimensions'] = [{'label': label, 'score': score}
                                   for entry in listing(dimensions) if isinstance(entry, dict)
                                   and (label := caption(entry.get('name'))) and (score := number(entry.get('score'))) is not None]
    return profile


def collect_public_profiles(urls, output, stop, emit, loader):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    path = output / f'public-poi-profile-{uuid.uuid4().hex}.json'
    report = {'source_method': 'public_poi_profile', 'profiles': [], 'failed': [],
              'remaining': len(urls), 'complete': False, 'report_file': str(path),
              'limitations': ['来自公开资料页及点评概览，不代表 App 当前页面或完整点评。',
                              '页面价格和开放说明保留其公开展示含义，未核对实际菜单、币种或库存。',
                              '点评话题仅为页面标签及显示数量，未取得标签关联明细或完整 AI 总结。']}
    for url in urls:
        if stop.is_set():
            break
        try:
            profile = project_profile(loader(url), url)
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            report['failed'].append({'url': url, 'error': str(exc)})
            emit('一项公开资料未能读取：' + url)
        else:
            report['profiles'].append(profile)
            emit('已读取公开资料：' + (profile['name'] or url))
        report['remaining'] -= 1
        if report['remaining'] and stop.wait(.8):
            break
    report['reason'] = ('cancelled' if stop.is_set() else 'partial' if report['failed'] and report['profiles']
                        else 'error' if report['failed'] else 'profiles_read')
    if report['reason'] == 'error':
        report['error'] = '未能读取任何对象的公开资料，请查看失败记录。'
    stage = path.with_suffix('.part')
    try:
        stage.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        stage.replace(path)
    finally:
        stage.unlink(missing_ok=True)
    return report
