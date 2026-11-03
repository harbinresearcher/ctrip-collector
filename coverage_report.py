"""统计本地已采评论和字段覆盖，不把对象数量当平台全量。"""
import json
import logging
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

logger = logging.getLogger(__name__)
FIELDS={'text':('content','originContent','translateContent'), 'images':('images','imageCuttingsList'),
        'videos':('videos',), 'replies':('replyInfo','replyContent'), 'score':('score','scores'),
        'publish_time':('publishTime',)}


def write_coverage_report(folder):
    # /* ===== 步骤1：读取本地对象；数据源：对象文件和评论库；操作：1) 区分来源 2) 不访问外部服务 ===== */
    logger.info('开始统计本地覆盖')
    folder=Path(folder).resolve()
    database=folder/'network.sqlite3'
    if not database.is_file():
        raise ValueError('输出目录没有网络评论数据库')
    objects={}
    def get(key):
        if key not in objects:
            objects[key]={'object_key':key,'review_count':0,'invalid_rows':0,'sources':[],
                          'fields':{name:{'present':0,'nonempty':0,'missing':0} for name in FIELDS},
                          'complete':False}
        return objects[key]
    invalid_objects=0
    for path in sorted(folder.glob('*-object.json')):
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(data,dict) or not data.get('kind') or data.get('resource_id') is None:
                raise ValueError('invalid object')
            # 1.1 来源分开列出，已有AI字段状态不等于完整内容。
            key=f"{data['kind']}-{data['resource_id']}"
            obj=get(key)
            obj['sources'].append({'file':path.name,'source_method':data.get('source_method'),
                                   'display_total':data.get('display_total'),
                                   'details_status':'nonempty' if data.get('details') else 'empty_or_missing',
                                   'ai_summary_status':(data.get('ai_summary') or {}).get('status','not_captured'),
                                   'ai_photos_status':(data.get('ai_photos') or {}).get('status','not_captured')})
        except (OSError,ValueError,TypeError,AttributeError):
            invalid_objects+=1
    logger.info('对象来源读取完成')
    # /* ===== 步骤2：统计评论字段；数据源：SQLite快照；操作：1) 区分缺失和非空 2) 保留错误行数量 ===== */
    logger.info('开始统计评论字段')
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
        for key,raw in db.execute('SELECT object_key,data FROM network_reviews ORDER BY object_key,rowid'):
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
    # 2.2 统计仅反映已有数据库，界面显示总数不能据此认定已采全。
    report_path=folder/('coverage-'+uuid.uuid4().hex+'.json')
    report={'scope':'local_network_database_only','complete':False,'invalid_object_files':invalid_objects,
            'objects':[objects[key] for key in sorted(objects)],'report_file':str(report_path)}
    temp=report_path.with_suffix('.tmp')
    temp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(report_path)
    logger.info('本地覆盖统计完成：%s个对象',len(objects))
    return report
