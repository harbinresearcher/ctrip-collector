"""统计本地已采评论和字段覆盖，不把对象数量当平台全量。"""
import json
import logging
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from android_coverage import inspect_android_coverage, local_file

logger = logging.getLogger(__name__)
FIELDS={'text':('content','originContent','translateContent'), 'images':('images','imageCuttingsList'),
        'videos':('videos',), 'replies':('replyInfo','replyContent'), 'score':('score','scores'),
        'publish_time':('publishTime',)}


def write_coverage_report(folder, *, include_android=False):
    # /* ===== 步骤1：读取本地对象；数据源：对象文件和评论库；操作：1) 区分来源 2) 不访问外部服务 ===== */
    logger.info('开始统计本地覆盖')
    folder=Path(folder).resolve()
    database=folder/'network.sqlite3'
    network_available = local_file(database)
    android = inspect_android_coverage(folder) if include_android else None
    if android and not (android['review_database'] != 'missing' or android['invalid_artifacts']
                        or any(android[key] for key in ('ai_summaries', 'gallery_runs', 'sight_details',
                                                       'sight_policies', 'sight_guides', 'city_recommendations',
                                                       'hotel_details', 'sight_ai_summaries', 'hotel_lists', 'page_field_runs',
                                                       'screenshot_ocr_runs', 'restaurant_lists', 'restaurant_details',
                                                       'shopping_lists', 'shopping_details'))):
        android = None
    if not network_available and not android:
        raise ValueError('输出目录没有网络评论数据库或已有手机采集结果')
    objects={}
    def get(key):
        if key not in objects:
            objects[key]={'object_key':key,'review_count':0,'invalid_rows':0,'sources':[],
                          'fields':{name:{'present':0,'nonempty':0,'missing':0} for name in FIELDS},
                          'complete':False}
        return objects[key]
    invalid_objects=0
    # 1.0 最新对象和历史抓包快照分别列出；历史快照不参与评论行数统计。
    paths = list(folder.glob('*-object.json')) + [path for path in folder.glob('hotel-*-app-capture-history-*.json')
                                                  if re.fullmatch(r'hotel-[1-9][0-9]*-app-capture-history-[0-9a-f]{32}\.json', path.name)]
    for path in sorted(paths):
        try:
            if not local_file(path):
                raise ValueError('invalid object file')
            data=json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data,dict) or not data.get('kind') or data.get('resource_id') is None:
                raise ValueError('invalid object')
            is_history = '-app-capture-history-' in path.name
            if is_history and (data.get('kind') != 'hotel' or str(data['resource_id']) != path.name.split('-')[1]
                               or data.get('source_method') != 'app_proxy_capture'):
                raise ValueError('history identity mismatch')
            # 1.1 来源分开列出，已有AI字段状态不等于完整内容。
            key=f"{data['kind']}-{data['resource_id']}"
            obj=get(key)
            obj['sources'].append({'file':path.name,'source_method':data.get('source_method'),
                                   'snapshot_role':'capture_history' if is_history else 'current',
                                   'display_total':data.get('display_total'),
                                   'details_status':'nonempty' if data.get('details') else 'empty_or_missing',
                                   'ai_summary_status':(data.get('ai_summary') or {}).get('status','not_captured'),
                                   'ai_photos_status':(data.get('ai_photos') or {}).get('status','not_captured')})
        except (OSError,ValueError,TypeError,AttributeError):
            invalid_objects+=1
    logger.info('对象来源读取完成')
    # /* ===== 步骤2：统计评论字段；数据源：SQLite快照；操作：1) 区分缺失和非空 2) 保留错误行数量 ===== */
    logger.info('开始统计评论字段')
    if network_available:
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
            count_fields(db.execute('SELECT object_key,data FROM network_reviews ORDER BY object_key,rowid'), get)
    # 2.2 手机数据的手填身份和网页对象身份是不同依据，来源和计数始终分开。
    report_path=folder/('coverage-'+uuid.uuid4().hex+'.json')
    report={'scope':'local_network_database_only','complete':False,'invalid_object_files':invalid_objects,
            'objects':[objects[key] for key in sorted(objects)],'report_file':str(report_path)}
    if android is not None:
        report.update(scope='local_network_and_android_artifacts', android=android,
                      network_database='read' if network_available else 'missing')
    temp=report_path.with_suffix('.tmp')
    temp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(report_path)
    logger.info('本地覆盖统计完成：%s个网页对象',len(objects))
    return report


def count_fields(rows, get):
    for key, raw in rows:
        obj=get(key)
        obj['review_count']+=1
        try:
            data=json.loads(raw)
            if not isinstance(data,dict): raise ValueError('invalid review')
        except (TypeError,ValueError):
            obj['invalid_rows']+=1
            continue
        # 2.1 存在但空值与缺少字段分别计数；数字零仍是有效值。
        for name,aliases in FIELDS.items():
            values=[data[key] for key in aliases if key in data]
            field=obj['fields'][name]
            if values:
                field['present']+=1
                if any(value is not None and value != '' and value != [] and value != {} for value in values):
                    field['nonempty']+=1
            else:
                field['missing']+=1
