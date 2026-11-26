"""Inspect saved Android results without joining operator-declared IDs to web objects."""
import json
import logging
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing

logger = logging.getLogger(__name__)
AI_FILE = re.compile(r'hotel-(\d{1,20})-\d{8}T\d{12}Z\.json')
HOTEL_KEY = re.compile(r'hotel-0*([1-9][0-9]{0,19})')
ASSET_FILE = re.compile(r'([a-f0-9]{64})\.(?:jpg|jpeg|png|webp|avif|gif)')
DISPLAY_FILE = re.compile(r'display-([a-f0-9]{64})\.png')
GALLERY_STATUSES = frozenset({'saved', 'display_saved', 'no_match', 'ambiguous', 'oversize', 'unavailable', 'error'})
RUN_STATUSES = frozenset({'running', 'cancelled', 'error', 'partial', 'expanded_visible',
                          'page_limit', 'end_reached', 'page_not_advanced',
                          'page_changed_unexpectedly', 'repeated_screen'})
# 推荐菜扫描的停止原因取值；只记录固定词表，不复制菜名。
DISH_SCAN_REASONS = frozenset({'expected_count_reached', 'missing_carousel', 'no_new_dishes',
                               'screen_limit', 'context_changed', 'unverified_transition',
                               'count_changed', 'device_error', 'cancelled'})
DISH_SCAN_METHODS = frozenset({'menu', 'carousel'})


def local_file(path):
    return (not path.is_symlink() and not path.is_junction() and path.is_file()
            and path.resolve().parent == path.parent)


def list_field(record, field):
    value = record.get(field, [])
    if not isinstance(value, list):
        raise ValueError('无效的手机结果列表')
    return value


def dish_scan_summary(value):
    # /* ===== 步骤1：保留推荐菜扫描结论；数据源：详情报告；操作：1) 只认固定词表 2) 不复制菜名 ===== */
    # 1.1 缺字段表示这次任务没有选读清单；无效结构按不可读记录，不能当成已完成。
    if value is None:
        return None
    if not isinstance(value, dict) or value.get('reason') not in DISH_SCAN_REASONS:
        return {'status': 'unreadable'}
    screens = value.get('screens')
    return {'status': value['reason'],
            'method': value.get('method') if value.get('method') in DISH_SCAN_METHODS else None,
            'screens': screens if type(screens) is int and 0 <= screens <= 10000 else None,
            'complete': value.get('reason') == 'expected_count_reached' and value.get('complete') is True}


def summarize_artifact(path, source, record, gallery_groups=None):
    """Return coverage only; omit commentary, summary text and image matching details."""
    if source == 'ai_summaries':
        identity = int(AI_FILE.fullmatch(path.name)[1])
        if (not identity or type(record.get('resource_id')) is not int or record['resource_id'] != identity
                or record.get('kind') != 'hotel' or record.get('source') != 'ctrip_android_ui'):
            raise ValueError('手机总结与文件编号不一致')
        photos = record.get('ai_selected_images', {})
        if not isinstance(photos, dict):
            raise ValueError('无效的手机图集元数据')
        sections = list_field(record, 'sections')
        return {'declared_object_key': f'hotel-{identity}', 'identity_verification': 'operator_declared_unverified',
                'section_count': len(sections),
                'rating_dimension_count': len(list_field(record, 'rating_dimensions')),
                'bound_count_section_count': sum(isinstance(section, dict) and bool(section.get('linked_count'))
                                                 for section in sections)
                if record.get('linked_review_scope') == 'display_counts_only' else 0,
                'expanded_boundary_visible': record.get('expanded_boundary_visible') is True,
                'ai_selected_count': photos.get('selected_count') if type(photos.get('selected_count')) is int else None}
    if source == 'gallery_runs':
        key = HOTEL_KEY.fullmatch(str(record.get('object_key', '')))
        if not key or record.get('category') != 'AI优选':
            raise ValueError('无效的手机图集对象')
        statuses, seen, assets = Counter(), set(), set()
        display_assets = 0
        display_total = None
        verified_files = []
        for page in list_field(record, 'pages'):
            index, total, result = page.get('index'), page.get('total'), page.get('result')
            if type(index) is not int or type(total) is not int or not 1 <= index <= total or index in seen:
                raise ValueError('无效的手机图集页码')
            if display_total is not None and total != display_total:
                raise ValueError('同一次图集采集包含两个显示总数')
            display_total = total
            seen.add(index)
            status = result.get('status')
            statuses[status if isinstance(status, str) and status in GALLERY_STATUSES else 'other'] += 1
            if status in ('saved', 'display_saved'):
                filename = result.get('file')
                pattern = DISPLAY_FILE if status == 'display_saved' else ASSET_FILE
                asset_name = pattern.fullmatch(filename) if isinstance(filename, str) else None
                if (asset_name and asset_name[1] == result.get('sha256')
                        and result.get('object_key') == record['object_key'] and index == result.get('gallery_index')):
                    asset = path.with_name(filename)
                    if local_file(asset) and type(result.get('bytes')) is int and asset.stat().st_size == result['bytes'] > 0:
                        assets.add(index)
                        display_assets += int(status == 'display_saved')
                        verified_files.append((index, status, asset_name[1]))
        if gallery_groups is not None and display_total is not None:
            group = gallery_groups.setdefault(('hotel-' + key[1], display_total),
                                              {'runs': 0, 'seen': set(), 'files': defaultdict(set)})
            group['runs'] += 1
            group['seen'].update(seen)
            for index, status, digest in verified_files:
                group['files'][index, status].add(digest)
        return {'declared_object_key': 'hotel-' + key[1], 'identity_verification': 'operator_declared_unverified',
                'page_statuses': dict(statuses), 'saved_assets_present': len(assets),
                'display_assets_present': display_assets, 'asset_check': 'name_and_size_only'}
    if source == 'screenshot_ocr_runs' and record.get('source_method') == 'local_screenshot_ocr':
        return {'line_count': len(list_field(record, 'lines'))}
    if source == 'page_field_runs' and record.get('source_method') == 'android_selected_page_fields':
        return {'name': record.get('name', ''), 'screen_count': len(list_field(record, 'screens')),
                'requested_field_count': len(list_field(record, 'field_labels'))}
    if source == 'city_recommendations' and record.get('source_method') == 'android_city_mustsee_snapshot':
        city = record.get('city')
        if not isinstance(city, str) or not 1 <= len(city) <= 40:
            raise ValueError('无效的手机推荐城市')
        return {'city': city, 'candidate_count': len(list_field(record, 'objects'))}
    if source == 'hotel_lists' and record.get('source_method') == 'android_hotel_list_snapshot':
        query = record.get('query')
        if not isinstance(query, dict) or not isinstance(query.get('location'), str) or not query['location'].strip():
            raise ValueError('无效的手机酒店搜索地区')
        return {'location': query['location'], 'screen_count': len(list_field(record, 'screens')),
                'display_record_count': len(list_field(record, 'hotels'))}
    if source in {'restaurant_lists', 'shopping_lists'}:
        kind, field = ('restaurant', 'restaurants') if source == 'restaurant_lists' else ('shopping', 'shops')
        if (record.get('source_method'), record.get('source'), record.get('kind')) != (
                f'android_{kind}_list_snapshot', 'ctrip_android_ui', kind):
            raise ValueError('无效的手机列表来源')
        city = record.get('city')
        if not isinstance(city, str) or not city.strip():
            raise ValueError('无效的手机列表城市')
        return {'city': city, 'screen_count': len(list_field(record, 'screens')),
                'display_record_count': len(list_field(record, field))}
    name = record.get('name')
    if not isinstance(name, str) or not name.strip():
        raise ValueError('无效的手机结果对象名称')
    if source == 'restaurant_details' and (record.get('source_method'), record.get('source'), record.get('kind')) == (
            'android_restaurant_detail_snapshot', 'ctrip_android_ui', 'restaurant'):
        fields = ('score_display', 'review_count_display', 'per_person_price_display', 'cuisine_display',
                  'opening_hours_display', 'opening_status_display', 'address_display', 'nearby_display',
                  'ranking_display', 'features', 'booking_preview')
        dish_count = record.get('dish_count_display')
        return {'name': name, 'identity_verification': 'operator_name_exact_match',
                'fields_present': [field for field in fields if record.get(field)],
                'recommended_dish_count': len(list_field(record, 'recommended_dishes')),
                'dish_count_display': dish_count if type(dish_count) is int and dish_count >= 0 else None,
                'recommendation_scan': dish_scan_summary(record.get('recommendation_scan'))}
    if source == 'shopping_details' and (record.get('source_method'), record.get('source'), record.get('kind')) == (
            'android_shopping_detail_snapshot', 'ctrip_android_ui', 'shopping'):
        fields = ('english_name', 'tagline_display', 'score_display', 'review_count_display',
                  'opening_hours_display', 'opening_status_display', 'basic_info_display',
                  'address_display', 'nearby_display', 'category_labels', 'badges', 'offers')
        return {'name': name, 'identity_verification': 'operator_name_exact_match',
                'fields_present': [field for field in fields if record.get(field)],
                'category_count': len(list_field(record, 'category_labels')),
                'offer_count': len(list_field(record, 'offers')),
                'review_entry_visible': record.get('review_entry_visible') is True}
    if source == 'hotel_details' and record.get('source_method') == 'android_hotel_detail_snapshot':
        time = record.get('captured_at')
        captured_at = time if isinstance(time, str) and re.fullmatch(
            r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?\+08:00', time) else None
        return {'name': name, 'room_card_count': len(list_field(record, 'room_cards')),
                'starting_price_visible': bool(record.get('hotel_starting_price')), 'captured_at': captured_at}
    if source == 'sight_details' and record.get('source_channel') == 'android_ui_snapshot':
        fields = ('opening_texts', 'price_texts', 'ticket_categories', 'booking_conditions_visible')
        return {'name': name, 'identity_verification': 'operator_name_exact_match',
                'fields_present': [field for field in fields if list_field(record, field)],
                'ticket_card_count': len(list_field(record, 'ticket_cards')),
                'header_fields_present': [field for field in ('review_display', 'address_text', 'ranking_labels', 'parent_sight_name')
                                          if record.get(field)],
                'policy_entry_visible': record.get('policy_status') == 'entry_visible_content_not_captured'}
    if source == 'sight_policies' and record.get('source_channel') == 'android_ui':
        return {'name': name, 'identity_verification': 'name_only_no_object_id',
                'row_count': len(list_field(record, 'rows')), 'screen_count': len(list_field(record, 'pages')),
                'supplementary_note_count': len(list_field(record, 'supplementary_notes'))}
    if (source == 'sight_ai_summaries' and record.get('source_method') == 'android_sight_ai_snapshot'
            and record.get('source') == 'ctrip_android_ui' and record.get('kind') == 'sight'
            and record.get('generated_by_ai') is True):
        thumbnails = record.get('gallery_thumbnails') or {}
        expected = path.name.removesuffix('.json').replace('sight-ai-', 'sight-ai-thumbnails-', 1) + '.zip'
        count = thumbnails.get('saved_count')
        present = (thumbnails.get('archive') == expected and type(count) is int
                   and count > 0 and local_file(path.with_name(expected)))
        return {'name': name, 'identity_verification': 'operator_name_exact_match',
                'section_count': len(list_field(record, 'sections')),
                'expand_entry_visible': record.get('expand_entry_visible') is True,
                'display_thumbnails_with_archive': count if present else 0}
    if source == 'sight_guides' and record.get('source_method') == 'android_sight_guide_snapshot':
        return {'name': name, 'identity_verification': 'operator_name_exact_match',
                'opening_period_count': len(list_field(record, 'opening_periods')),
                'booking_note_count': len(list_field(record, 'booking_notes'))}
    raise ValueError('无效的手机结果来源')


def missing_ranges(indices, total):
    """Use ranges so a displayed count never allocates one element per absent page."""
    ranges, previous = [], 0
    for page in sorted(indices):
        if page > previous + 1:
            ranges.append([previous + 1, page - 1])
        previous = page
    if previous < total:
        ranges.append([previous + 1, total])
    return ranges


def summarize_gallery_groups(groups):
    for (key, total), group in sorted(groups.items()):
        files = group['files']
        cached = {page for page, status in files if status == 'saved'}
        display = {page for page, status in files if status == 'display_saved'}
        present = cached | display
        seen = group['seen']
        unsaved = sorted(seen - present)
        ranges = []
        for page in unsaved:
            if ranges and ranges[-1][1] + 1 == page:
                ranges[-1][1] = page
            else:
                ranges.append([page, page])
        yield {'declared_object_key': key, 'display_total': total, 'run_count': group['runs'],
               'cache_pages': sorted(cached), 'display_only_pages': sorted(display - cached),
               'observed_without_file_ranges': ranges, 'unseen_ranges': missing_ranges(seen, total),
               'missing_file_ranges': missing_ranges(present, total),
               'conflicting_file_pages': sorted({page for (page, _), hashes in files.items() if len(hashes) > 1}),
               'reconciliation_scope': 'same_declared_hotel_and_display_total_only',
               'asset_check': 'name_and_size_only', 'complete': False}


def inspect_android_coverage(folder):
    logger.info('开始核对已保存的手机采集结果')
    groups = {'ai_summaries': (folder / 'ai', AI_FILE),
              'gallery_runs': (folder, re.compile(r'gallery-run-[a-f0-9]{32}\.json')),
              'sight_details': (folder, re.compile(r'sight-detail-[a-f0-9]{32}\.json')),
              'sight_policies': (folder, re.compile(r'sight-policy-[a-f0-9]{32}\.json')),
              'sight_ai_summaries': (folder, re.compile(r'sight-ai-[a-f0-9]{32}\.json')),
              'sight_guides': (folder, re.compile(r'sight-guide-[a-f0-9]{32}\.json')),
              'city_recommendations': (folder, re.compile(r'app-city-[a-f0-9]{32}\.json')),
              'screenshot_ocr_runs': (folder, re.compile(r'app-screenshot-ocr-[a-f0-9]{32}\.json')),
              'page_field_runs': (folder, re.compile(r'app-page-fields-[a-f0-9]{32}\.json')),
              'hotel_lists': (folder, re.compile(r'app-hotel-list-[a-f0-9]{32}\.json')),
              'hotel_details': (folder, re.compile(r'app-hotel-[a-f0-9]{32}\.json')),
              'restaurant_lists': (folder, re.compile(r'app-restaurant-list-[a-f0-9]{32}\.json')),
              'restaurant_details': (folder, re.compile(r'app-restaurant-detail-[a-f0-9]{32}\.json')),
              'shopping_lists': (folder, re.compile(r'app-shopping-list-[a-f0-9]{32}\.json')),
              'shopping_details': (folder, re.compile(r'app-shopping-detail-[a-f0-9]{32}\.json'))}
    summary = {source: [] for source in groups}
    gallery_groups = {}
    summary.update(reviews=[], legacy_unlinked_review_count=0, invalid_artifacts=0, review_database='missing')
    database = folder / 'reviews.sqlite3'
    if local_file(database):
        try:
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'reviews_scoped' in tables:
                    for kind, identity, count, expand_count in db.execute("""SELECT kind,object_id,COUNT(*),
                        SUM(CASE WHEN has_expand=1 THEN 1 ELSE 0 END) FROM reviews_scoped
                        GROUP BY kind,object_id ORDER BY kind,object_id"""):
                        if kind in {'hotel', 'sight', 'restaurant', 'shopping'} and re.fullmatch(r'[1-9][0-9]{0,19}', str(identity)):
                            summary['reviews'].append({'declared_object_key': f'{kind}-{identity}', 'review_count': count,
                                'expand_marked_count': expand_count, 'identity_verification': 'operator_declared_unverified'})
                if 'reviews' in tables:
                    summary['legacy_unlinked_review_count'] = db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]
            summary['review_database'] = 'read'
        except sqlite3.Error:
            summary['review_database'] = 'unreadable'
    # Only output folders and known report names are scanned. Never open XML evidence.
    for source, (directory, pattern) in groups.items():
        if not directory.is_dir() or directory.is_symlink() or directory.is_junction():
            continue
        for path in sorted(directory.iterdir()):
            if not pattern.fullmatch(path.name):
                continue
            try:
                if not local_file(path) or path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError('无法读取规范手机报告文件')
                record = json.loads(path.read_text(encoding='utf-8'))
                entry = summarize_artifact(path, source, record, gallery_groups)
                status = record.get('status')
                if isinstance(status, str) and status in RUN_STATUSES:
                    entry['status'] = status
                entry.update(file=path.relative_to(folder).as_posix(), complete=False)
                summary[source].append(entry)
            except (AttributeError, OSError, TypeError, ValueError):
                summary['invalid_artifacts'] += 1
    summary['gallery_page_coverage'] = list(summarize_gallery_groups(gallery_groups))
    logger.info('本地手机采集结果核对完成')
    return summary
