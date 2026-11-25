"""Adapt filtered App capture imports to the workbench task contract."""
import logging
import re
from pathlib import Path

from capture_io import import_capture

logger = logging.getLogger(__name__)


def prepare_capture_job(request, output):
    # /* ===== 步骤1：核验导入参数；数据源：工作台表单；操作：1) 固定导入目录 2) 校验文件名和预算 ===== */
    logger.info('开始核验受限响应导入参数')
    root = Path(output).resolve()
    filename = request.get('capture_file')
    limit = request.get('limit', 500)
    refresh = request.get('refresh', False)
    # 1.1 外部筛选文件限定captures目录；本程序归档限定输出目录直属的规范名称。
    if request.get('mode') != 'capture_import' or not isinstance(filename, str):
        raise ValueError('请填写 captures 目录内的受限响应 JSONL 文件名')
    archived = filename.startswith('archive:')
    name = filename[len('archive:'):] if archived else filename
    pattern = (r'hotel-[1-9][0-9]*-app-capture-batches-[0-9a-f]{32}\.jsonl' if archived
               else r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.jsonl')
    if not re.fullmatch(pattern, name):
        raise ValueError('受限响应文件名无效')
    if type(limit) is not int or not 1 <= limit <= 100000:
        raise ValueError('目标评论条数须为1至100000的整数')
    if type(refresh) is not bool:
        raise ValueError('刷新选项必须为布尔值')

    def checked_source():
        # /* ===== 步骤1：核验来源路径；数据源：固定目录；操作：1) 拒绝链接 2) 限制直属文件 ===== */
        logger.info('开始核验受限响应来源')
        directory = root if archived else root / 'captures'
        source = directory / name
        # 1.1 同时拒绝目录链接与文件链接，解析后仍须在指定目录。
        if (directory.is_symlink() or directory.is_junction() or not directory.is_dir()
                or (not archived and directory.resolve().parent != root)
                or source.is_symlink() or source.is_junction() or not source.is_file()
                or source.resolve().parent != directory.resolve()):
            raise ValueError('受限响应文件不存在或不属于 captures 目录')
        logger.info('受限响应来源核验完成')
        return source.resolve()

    checked_source()
    logger.info('受限响应导入参数核验完成')

    def job(stop, emit):
        # /* ===== 步骤2：导入受限响应；数据源：已校验JSONL；操作：1) 传递取消事件 2) 保留结果与错误 ===== */
        logger.info('开始工作台受限响应导入')
        report = {'reason': 'capture_imported', 'error': '', 'results': [],
                  'complete': False, 'source_method': 'app_proxy_capture'}
        if stop.is_set():
            report['reason'] = 'cancelled'
            logger.info('受限响应导入启动前已取消')
            return report
        # 2.1 排队期间可能替换文件，实际读取前再次校验。
        source = checked_source()
        emit('正在导入已筛选的 App 响应；此步骤不会抓包')
        try:
            # 2.2 每家酒店结束时保存报告，后续异常不会丢失已完成结果。
            options = {'refresh': True} if refresh else {}
            results = import_capture(source, root, limit, stop, emit, on_result=report['results'].append, **options)
            report['results'] = results
        except Exception as exc:
            # 2.3 即使同时收到取消信号，也要优先显示实际失败。
            report.update(reason='error', error=str(exc) or type(exc).__name__)
            logger.info('工作台受限响应导入失败：%s', report['error'])
            return report
        # 2.4 导出错误优先于停止状态，并保留已保存对象的详细报告。
        failure = next((item for item in results if item.get('reason') == 'error'
                        or item.get('error') or item.get('export_error')), None)
        if failure:
            report.update(reason='error', error=failure.get('error') or failure.get('export_error') or '导入失败')
        elif stop.is_set() or any(item.get('reason') == 'cancelled' for item in results):
            report['reason'] = 'cancelled'
        emit('受限响应导入结束，请核对对象报告和数据来源')
        logger.info('工作台受限响应导入结束：%s', report['reason'])
        return report

    return job
