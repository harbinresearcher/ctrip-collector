"""Adapt exported review image downloads to the local workbench."""
import logging
import re
from pathlib import Path

from media import download_media
from video_media import download_videos

logger = logging.getLogger(__name__)


def prepare_media_job(request, output):
    # /* ===== 步骤1：核验来源；数据源：工作台表单；操作：1) 限定对象文件 2) 校验图片预算 ===== */
    logger.info('开始校验评论图片采集参数')
    folder = Path(output).resolve()
    filename = request.get('source_file')
    limit = request.get('media_limit', 20)
    # 1.1 名称只能对应已支持的对象JSONL，不接收路径或其他文件类型。
    mode = request.get('mode')
    if mode not in {'media', 'video_media'} or not isinstance(filename, str) or not re.fullmatch(r'(hotel|sight|restaurant|shopping)-[1-9][0-9]*\.jsonl', filename):
        raise ValueError('请选择输出目录中有效的对象评论文件')
    if type(limit) is not int or not 1 <= limit <= 100000:
        raise ValueError('图片数量上限须为1至100000的整数')

    def checked_source():
        # /* ===== 步骤1：核验来源文件；数据源：固定输出目录；操作：1) 禁止符号链接 2) 限定直属文件 ===== */
        logger.info('开始核验评论来源文件')
        candidate = folder / filename
        # 1.1 解析后仍须位于原输出目录；直接符号链接即使指向内部也拒绝。
        if candidate.is_symlink() or not candidate.is_file() or candidate.resolve().parent != folder:
            raise ValueError('评论文件不存在或不属于当前输出目录')
        logger.info('评论来源文件核验完成')
        return candidate.resolve()

    checked_source()
    logger.info('评论图片采集参数校验完成')

    def job(stop, emit):
        # /* ===== 步骤2：下载评论图片；数据源：已核验JSONL；操作：1) 传递停止事件 2) 返回原报告 ===== */
        logger.info('开始执行评论图片采集')
        if stop.is_set():
            logger.info('图片任务启动前已取消')
            return {'reason': 'cancelled', 'complete': False}
        # 2.1 排队期间文件可能变更，在实际启动前再次核验。
        source = checked_source()
        # 2.2 视频与图片复用同一来源校验，各自保留清单和文件。
        video = mode == 'video_media'
        emit('正在读取评论视频；只下载平台返回的版本' if video else '正在读取评论图片；只下载平台返回的版本')
        result = (download_videos if video else download_media)(source, folder, limit=limit, stop=stop)
        emit('媒体任务已结束，请查看报告中的下载和复用数量')
        logger.info('评论图片采集任务结束')
        return result

    return job
