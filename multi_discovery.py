"""按地区依次发现点评对象，保留每个地区的结果和停止原因。"""
import json
import hashlib
import logging
import uuid
from pathlib import Path

from network import canonical_url

logger = logging.getLogger(__name__)


def resume_details(path):
    # /* ===== 步骤1：读取续跑配置；数据源：地区任务报告；操作：1) 校验格式 2) 保留原地区和预算 ===== */
    logger.info('开始读取地区续跑配置')
    try:
        data=json.loads(Path(path).read_text(encoding='utf-8'))
        entries=data['entries']
        pages=data['max_pages']
        objects=data['max_objects']
        if data.get('schema')!=1 or not isinstance(entries,list) or not entries or not all(isinstance(x,str) and x for x in entries):
            raise ValueError('地区清单无效')
        if type(pages) is not int or type(objects) is not int or pages<1 or objects<1:
            raise ValueError('地区预算无效')
        if any(x.isdigit() for x in entries) and not all(x.isdigit() for x in entries):
            raise ValueError('购物地区ID与其他地区不能混用')
    except (OSError,KeyError,TypeError,json.JSONDecodeError) as exc:
        raise ValueError('地区续跑报告无效或损坏') from exc
    logger.info('地区续跑配置读取完成')
    return entries,pages,objects


def discover_many(entries, output, max_pages, max_objects, stop, finder, *, resume_report=None, emit=None):
    # /* ===== 步骤1：建立地区任务；数据源：用户地区清单；操作：1) 去重 2) 创建独立报告 ===== */
    logger.info('开始多地区目录发现')
    entries = list(dict.fromkeys(str(item).strip() for item in entries if str(item).strip()))
    emit=emit or logger.info
    if not entries or max_pages < 1 or max_objects < 1:
        raise ValueError('需要至少一个地区，页数和对象上限必须为正数')
    # 1.1 恢复只复用相同清单和预算下完整保存的地区快照，不宣称平台全量。
    def digest(value):
        return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()
    previous=None
    finished={}
    if resume_report is not None:
        try:
            previous=json.loads(Path(resume_report).read_text(encoding='utf-8'))
            if (previous.get('schema')!=1 or previous.get('entries')!=entries or previous.get('max_pages')!=max_pages
                    or previous.get('max_objects')!=max_objects or digest(previous['objects'])!=previous.get('objects_sha256')):
                raise ValueError('地区清单、预算或对象快照不一致')
            for row in previous['regions']:
                snapshot={k:row[k] for k in ('entry','reason','error','objects')}
                if row['entry'] not in entries or digest(snapshot)!=row.get('snapshot_sha256'):
                    raise ValueError('地区快照校验失败')
                if not row['error'] and row['reason'] in {'page_budget_reached','object_budget_reached','listing_end','empty_page','repeated_pages'}:
                    finished[row['entry']]=row
            for obj in previous['objects']:
                if canonical_url(obj['url'])!=obj['url']:
                    raise ValueError('地区对象链接无效')
        except (OSError,KeyError,TypeError,json.JSONDecodeError) as exc:
            raise ValueError('地区续跑报告无效或损坏') from exc
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    stem = output / ('discovery-regions-' + uuid.uuid4().hex)
    report = {'schema':1,'entries':entries,'max_pages':max_pages,'max_objects':max_objects,'resumed_regions':0,
              'reason':'running', 'error':'', 'complete':False, 'regions':[], 'objects':list(previous['objects']) if previous else [],
              'links_file':str(stem.with_suffix('.txt')), 'report_file':str(stem.with_suffix('.json'))}
    seen={item['url'] for item in report['objects']}
    # 1.1 每次替换完整文件，异常不会覆盖前一地区已写出的清单。
    def checkpoint():
        report['objects_sha256']=digest(report['objects'])
        for path, content in ((Path(report['links_file']), ''.join(item['url']+'\n' for item in report['objects'])),
                              (Path(report['report_file']), json.dumps(report, ensure_ascii=False, indent=2))):
            temp=path.with_suffix(path.suffix+'.tmp')
            temp.write_text(content,encoding='utf-8')
            temp.replace(path)
    checkpoint()
    logger.info('多地区任务建立完成：%s个地区',len(entries))
    # /* ===== 步骤2：逐地区发现；数据源：已有目录适配器；操作：1) 合并对象 2) 失败保留已有结果 ===== */
    logger.info('开始逐地区读取目录')
    try:
        for position,entry in enumerate(entries,1):
            if stop.is_set():
                report['reason']='cancelled'
                break
            emit(f'地区 {position}/{len(entries)}：{entry}')
            # 2.1 只跳过本次预算内已结束且快照完整的地区；错误地区重新发现。
            if entry in finished:
                report['regions'].append(finished[entry])
                report['resumed_regions']+=1
                checkpoint()
                emit('已复用这个地区的记录')
                continue
            result=finder(entry,output,max_pages,max_objects,stop)
            # 2.1 对象按规范链接去重，各地区的原始报告仍单独保留。
            for item in result.get('objects',[]):
                url=canonical_url(item['url'])
                if url not in seen:
                    seen.add(url)
                    report['objects'].append({**item,'url':url})
            snapshot={'entry':entry,'reason':result.get('reason'),'error':result.get('error',''),
                      'objects':result.get('objects',[])}
            report['regions'].append({**snapshot,'snapshot_sha256':digest(snapshot),
                                      'objects_found':len(snapshot['objects']),'links_file':result.get('links_file')})
            checkpoint()
            emit(f"本地区结束：{result.get('reason')}，发现 {len(snapshot['objects'])} 个对象")
            if result.get('reason') == 'error' or result.get('error'):
                report.update(reason='error',error=result.get('error') or '地区目录失败')
                break
            if result.get('reason') == 'cancelled' or stop.is_set():
                report['reason']='cancelled'
                break
        if report['reason']=='running':
            report['reason']='queue_exhausted'
    except Exception as exc:
        report.update(reason='error',error=str(exc))
    finally:
        checkpoint()
        logger.info('多地区目录发现结束：%s',report['reason'])
    return report
