"""按地区依次发现点评对象，保留每个地区的结果和停止原因。"""
import json
import logging
import uuid
from pathlib import Path

from network import canonical_url

logger = logging.getLogger(__name__)


def discover_many(entries, output, max_pages, max_objects, stop, finder):
    # /* ===== 步骤1：建立地区任务；数据源：用户地区清单；操作：1) 去重 2) 创建独立报告 ===== */
    logger.info('开始多地区目录发现')
    entries = list(dict.fromkeys(str(item).strip() for item in entries if str(item).strip()))
    if not entries or max_pages < 1 or max_objects < 1:
        raise ValueError('需要至少一个地区，页数和对象上限必须为正数')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    stem = output / ('discovery-regions-' + uuid.uuid4().hex)
    report = {'reason':'queue_exhausted', 'error':'', 'complete':False, 'regions':[], 'objects':[],
              'links_file':str(stem.with_suffix('.txt')), 'report_file':str(stem.with_suffix('.json'))}
    seen=set()
    # 1.1 每次替换完整文件，异常不会覆盖前一地区已写出的清单。
    def checkpoint():
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
        for entry in entries:
            if stop.is_set():
                report['reason']='cancelled'
                break
            result=finder(entry,output,max_pages,max_objects,stop)
            # 2.1 对象按规范链接去重，各地区的原始报告仍单独保留。
            for item in result.get('objects',[]):
                url=canonical_url(item['url'])
                if url not in seen:
                    seen.add(url)
                    report['objects'].append({**item,'url':url})
            report['regions'].append({'entry':entry,'reason':result.get('reason'),
                                      'error':result.get('error',''),'objects_found':len(result.get('objects',[])),
                                      'links_file':result.get('links_file')})
            checkpoint()
            if result.get('reason') == 'error' or result.get('error'):
                report.update(reason='error',error=result.get('error') or '地区目录失败')
                break
            if result.get('reason') == 'cancelled' or stop.is_set():
                report['reason']='cancelled'
                break
    except Exception as exc:
        report.update(reason='error',error=str(exc))
    finally:
        checkpoint()
        logger.info('多地区目录发现结束：%s',report['reason'])
    return report
