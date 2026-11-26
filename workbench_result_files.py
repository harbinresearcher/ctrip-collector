"""Open known local task artifacts with strict path validation."""
import logging
import os
import re
import stat
from pathlib import Path

from mobile_scoped_store import MOBILE_REVIEW_RUN_ID

logger = logging.getLogger(__name__)
HOTEL_REPORT = re.compile(r'app-hotel-[a-f0-9]{32}\.json')


def open_task_csv(output, snapshot, *, sample=False):
    # /* ===== 步骤1：核验当前任务；数据源：管理器状态快照；操作：1) 限定终态 2) 校验手机导出报告 ===== */
    logger.info('开始核验手机评论导出')
    if not isinstance(snapshot, dict) or snapshot.get('status') not in {'finished', 'cancelled', 'error'}:
        raise ValueError('当前没有已结束的采集任务')
    result = snapshot.get('result')
    if not isinstance(result, dict):
        raise ValueError('当前任务没有可下载报告')
    raw, kind = result.get('sample_csv' if sample else 'csv'), result.get('kind')
    if not isinstance(raw, str) or not raw or kind not in {'hotel', 'sight', 'restaurant', 'shopping'}:
        raise ValueError('当前任务没有手机评论CSV')
    # 1.1 只接受采集器已返回的绝对路径及对应类型的摘要文件名。
    path = Path(raw)
    if sample:
        run_id = result.get('run_id')
        valid = (isinstance(run_id, str) and re.fullmatch(r'[0-9]{8}T[0-9]{6}-[a-f0-9]{6}', run_id)
                 and path.name == f'{kind}-sample-{run_id}.csv')
    else:
        valid = re.fullmatch(rf'{kind}-(?:[0-9a-f]{{12}}|v2-[0-9a-f]{{24}})\.csv', path.name)
    if not path.is_absolute() or not valid:
        raise ValueError('手机评论CSV路径或文件名无效')
    return open_output_file(output, path)


def open_hotel_report(output, name):
    if not isinstance(name, str) or not HOTEL_REPORT.fullmatch(name):
        raise ValueError('酒店房型报告文件名无效')
    root = Path(output).resolve()
    return open_output_file(root, root / name)


def open_review_thumbnails(output, snapshot, run):
    result = snapshot.get('result')
    if (snapshot.get('status') not in {'finished', 'cancelled', 'error'}
            or not isinstance(result, dict) or result.get('run_id') != run
            or not isinstance(run, str) or not MOBILE_REVIEW_RUN_ID.fullmatch(run)):
        raise ValueError('当前任务没有对应的点评图片')
    kind = result.get('kind')
    if kind not in ('hotel', 'restaurant'):
        raise ValueError('当前任务不支持点评图片下载')
    name = f'{kind}-review-thumbnails-{run}.zip'
    metadata = result.get('review_thumbnails')
    if not isinstance(metadata, dict) or metadata.get('archive') != name:
        raise ValueError('当前任务没有保存点评图片')
    root = Path(output).resolve()
    return open_output_file(root, root / name), f'{kind}-review-thumbnails.zip'


def open_restaurant_review_gallery(output, snapshot, run):
    result = snapshot.get('result')
    if (snapshot.get('status') not in {'finished', 'cancelled', 'error'} or not isinstance(result, dict)
            or result.get('kind') != 'restaurant' or result.get('source') != 'ctrip_android_ui'
            or result.get('source_method') != 'android_restaurant_review_gallery'
            or not isinstance(run, str) or not MOBILE_REVIEW_RUN_ID.fullmatch(run)
            or result.get('run_id') != run):
        raise ValueError('当前任务没有对应的餐厅点评图集')
    root = Path(output).resolve()
    basename = f'restaurant-review-gallery-{run}'
    name = f'{basename}.zip'
    if result.get('archive') != name or result.get('file') != str(root / f'{basename}.json'):
        raise ValueError('当前任务没有保存该餐厅点评图集')
    return open_output_file(root, root / name), 'restaurant-review-gallery.zip'


def open_sight_gallery(output, snapshot, report_id):
    result = snapshot.get('result')
    if (snapshot.get('status') != 'finished' or not isinstance(result, dict)
            or result.get('source_method') != 'android_sight_ai_snapshot'
            or not isinstance(report_id, str) or not re.fullmatch(r'[a-f0-9]{32}', report_id)):
        raise ValueError('当前任务没有对应的景点晒图')
    name = f'sight-ai-thumbnails-{report_id}.zip'
    metadata = result.get('gallery_thumbnails') or {}
    if metadata.get('report_id') != report_id or metadata.get('archive') != name:
        raise ValueError('当前任务没有保存该组景点晒图')
    root = Path(output).resolve()
    return open_output_file(root, root / name)


def open_sight_photo_gallery(output, snapshot, run_id):
    result = snapshot.get('result')
    if (snapshot.get('status') not in {'finished', 'cancelled', 'error'} or not isinstance(result, dict)
            or result.get('kind') != 'sight' or result.get('source') != 'ctrip_android_ui'
            or result.get('source_method') != 'android_sight_photo_gallery'
            or not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id)
            or result.get('run_id') != run_id):
        raise ValueError('当前任务没有对应的景点图集')
    root = Path(output).resolve()
    basename = f'sight-photo-gallery-{run_id}'
    name = f'{basename}.zip'
    if result.get('archive') != name or result.get('file') != str(root / f'{basename}.json'):
        raise ValueError('当前任务没有保存该景点图集')
    return open_output_file(root, root / name)


def open_sight_replay_csv(output, snapshot, replay_id):
    result = snapshot.get('result')
    if (snapshot.get('status') not in {'finished', 'cancelled'} or not isinstance(result, dict)
            or result.get('source_method') != 'android_sight_review_evidence_replay'
            or not isinstance(replay_id, str) or not re.fullmatch(r'[a-f0-9]{32}', replay_id)
            or result.get('replay_id') != replay_id):
        raise LookupError('这份离线重读结果不可用')
    root = Path(output).resolve()
    path = root / f'sight-review-replay-{replay_id}.csv'
    if result.get('csv') != str(path):
        raise LookupError('这份离线重读 CSV 不可用')
    return open_output_file(root, path)


def open_output_file(output, path):
    root = Path(output).resolve()
    if path.is_symlink():
        raise ValueError('不允许下载符号链接')
    resolved = path.resolve(strict=True)
    if path.parent.resolve() != root or resolved.parent != root or resolved != path.absolute():
        raise ValueError('结果文件不在输出目录内')
    logger.info('手机评论导出路径核验完成')
    # /* ===== 步骤2：打开已核验文件；数据源：固定输出目录；操作：1) 限定普通文件 2) 返回已打开流 ===== */
    logger.info('开始打开手机评论CSV')
    # 2.1 Windows打开重解析点本身，不跟随末级链接；其他平台使用O_NOFOLLOW。
    if os.name == 'nt':
        import _winapi
        import msvcrt
        handle = _winapi.CreateFile(str(path), 0x80000000, 7, 0, 3, 0x00200000, 0)
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _winapi.CloseHandle(handle)
            raise
    else:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('结果文件不是普通文件')
        stream = os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise
    logger.info('手机评论CSV打开完成')
    return stream
