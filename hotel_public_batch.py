"""批量读取酒店公开详情页及其评论样本，不使用浏览器登录。"""
import json
import logging
import uuid
from pathlib import Path
from threading import Event

from network import canonical_url, collect_network, load_target

logger = logging.getLogger(__name__)


def collect_public_hotels(urls, output, limit=500, stop=None, emit=None):
    # /* ===== 步骤1：核验酒店清单；数据源：酒店公开详情链接；操作：1) 限定类型 2) 去重并创建报告 ===== */
    logger.info('开始公开酒店样本采集')
    urls=list(dict.fromkeys(canonical_url(url) for url in urls))
    if not urls or limit < 1 or any(not url.startswith('https://hotels.ctrip.com/hotels/') for url in urls):
        raise ValueError('公开酒店样本需要酒店详情链接及正数目标')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    stop=stop or Event();emit=emit or logger.info
    path=output/('hotel-public-batch-'+uuid.uuid4().hex+'.json')
    report={'reason':'running','complete':False,'results':[],'error':'','report_file':str(path)}
    def save():
        temp=path.with_suffix('.tmp')
        temp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        temp.replace(path)
    save()
    logger.info('公开酒店清单核验完成')
    # /* ===== 步骤2：读取公开样本；数据源：酒店详情页；操作：1) 保留来源和样本边界 2) 中断保留结果 ===== */
    logger.info('开始逐酒店读取样本')
    try:
        for index,url in enumerate(urls,1):
            if stop.is_set():
                report['reason']='cancelled'
                break
            emit(f'酒店 {index}/{len(urls)}：读取公开详情与样本')
            target=load_target(url)
            # 2.1 仅接受已验证的公开页快照，不借此隐式发起登录或猜测分页。
            if target.get('source_method')!='hotel_public_html_snapshot':
                raise ValueError('酒店页面不是可识别的公开快照')
            result=collect_network(target,output,limit,stop,emit,delay=0)
            report['results'].append(result)
            save()
            if result.get('error') or result.get('export_error') or result['reason']=='error':
                report.update(reason='error',error=result.get('error') or result.get('export_error') or '酒店样本读取失败')
                break
            if result['reason']=='cancelled' or stop.is_set():
                report['reason']='cancelled'
                break
            if index < len(urls) and stop.wait(1.5):
                report['reason']='cancelled'
                break
        if report['reason']=='running':report['reason']='queue_exhausted'
    except Exception as exc:
        report.update(reason='error',error=str(exc))
    finally:
        save()
        logger.info('公开酒店样本采集结束：%s',report['reason'])
    return report
