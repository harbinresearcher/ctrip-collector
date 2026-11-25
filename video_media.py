"""下载已保存评论中明确返回的 MP4 地址，保留评论关联。"""
import hashlib
import json
import logging
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from threading import Event
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

logger = logging.getLogger(__name__)
MAX_VIDEO_BYTES = 100 * 1024 * 1024
CHUNK_BYTES = 128 * 1024
VIDEO_HOSTS = {'video.c-ctrip.com', 'video-preview.ctrip.com'}


def validate_video_url(url):
    # /* ===== 步骤1：校验视频地址；数据源：评论返回的播放地址；操作：1) 限制已观察域名 2) 拒绝认证和非标准端口 ===== */
    logger.info('开始核验评论视频地址')
    if not isinstance(url, str):
        raise ValueError('视频地址无效')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in VIDEO_HOSTS
            or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)
            or not parsed.path.lower().endswith('.mp4')):
        raise ValueError('视频地址不属于已观察的携程 MP4 地址')
    logger.info('评论视频地址核验完成')
    return url


class CheckedVideoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # /* ===== 步骤1：校验跳转；数据源：视频响应；操作：1) 目标必须受限 2) 请求前复核 ===== */
        logger.info('开始核验视频跳转')
        validate_video_url(newurl)
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        logger.info('视频跳转核验完成')
        return result


def download_videos(source, output, limit=20, stop=None, opener=None, delay=1.5):
    # /* ===== 步骤1：准备视频采集；数据源：评论 JSONL；操作：1) 限定预算 2) 建立可续跑清单 ===== */
    logger.info('开始准备评论视频采集')
    if type(limit) is not int or not 1 <= limit <= 100000:
        raise ValueError('视频数量上限须为1至100000')
    output = Path(output).resolve()
    folder = output / 'videos'
    folder.mkdir(parents=True, exist_ok=True)
    stop = stop or Event()
    opener = opener or build_opener(CheckedVideoRedirect())
    report = {'reason': 'source_exhausted', 'error': '', 'complete': False, 'downloaded': 0, 'reused': 0,
              'source': str(Path(source).resolve()), 'limit': limit,
              'output_folder': str(folder), 'manifest_file': str(output / 'video-manifest.jsonl'),
              'variant': 'returned_play_url', 'playback_verified': False}
    seen = set()
    logger.info('评论视频采集准备完成')
    # /* ===== 步骤2：读取并下载；数据源：评论 videos.playUrl；操作：1) 按地址去重 2) 流式验证并原子保存 ===== */
    logger.info('开始读取评论视频')
    with closing(sqlite3.connect(output / 'video-media.sqlite3')) as db:
        db.execute('CREATE TABLE IF NOT EXISTS assets (url_hash TEXT PRIMARY KEY, file TEXT, sha256 TEXT, bytes INTEGER, captured_at TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS refs (object_key TEXT, comment_id TEXT, url_hash TEXT, PRIMARY KEY(object_key,comment_id,url_hash))')
        db.commit()
        try:
            with Path(source).open(encoding='utf-8-sig') as stream:
                for line in stream:
                    if stop.is_set():
                        report['reason'] = 'cancelled'
                        break
                    row = json.loads(line)
                    data = row.get('data')
                    if not isinstance(data, dict) or not row.get('object_key') or not (data.get('commentId') or data.get('id')):
                        raise ValueError('评论缺少对象或稳定评论 ID')
                    videos = data.get('videos') or []
                    if not isinstance(videos, list):
                        raise ValueError('评论视频列表格式不正确')
                    for video in videos:
                        if not isinstance(video, dict) or not video.get('playUrl'):
                            continue
                        url = validate_video_url(video['playUrl'])
                        key = hashlib.sha256(url.encode('utf-8')).hexdigest()
                        if stop.is_set():
                            report['reason'] = 'cancelled'
                            break
                        if key not in seen and len(seen) >= limit:
                            # 2.0 预算只限制不同视频，继续扫描以关联已选视频的其他评论。
                            report['reason'] = 'video_budget_reached'
                            continue
                        # 2.1 URL 仅保留摘要，避免在清单中复制临时查询参数。
                        with db:
                            db.execute('INSERT OR IGNORE INTO refs VALUES (?,?,?)',
                                       (row['object_key'], str(data.get('commentId') or data['id']), key))
                        if key in seen:
                            continue
                        seen.add(key)
                        filename = key + '.mp4'
                        final = folder / filename
                        existing = db.execute('SELECT file,sha256 FROM assets WHERE url_hash=?', (key,)).fetchone()
                        if existing and existing[0] == filename and not final.is_symlink() and final.is_file():
                            with final.open('rb') as saved:
                                valid = hashlib.file_digest(saved, 'sha256').hexdigest() == existing[1]
                            if valid:
                                report['reused'] += 1
                                continue
                        with db:
                            db.execute('DELETE FROM assets WHERE url_hash=?', (key,))
                        temporary = folder / (key + '.' + uuid.uuid4().hex + '.part')
                        size, digest, header = 0, hashlib.sha256(), b''
                        try:
                            # 2.2 分块读取并检查大小；错误或取消时清除未完成文件。
                            with opener.open(url, timeout=30) as response, temporary.open('xb') as saved:
                                # 2.2.1 不请求分段；部分响应和无效长度不能作为完整文件。
                                headers = getattr(response, 'headers', {})
                                if getattr(response, 'status', 200) != 200 or headers.get('Content-Range') is not None:
                                    raise ValueError('视频接口未返回完整响应')
                                length = headers.get('Content-Length')
                                if length is not None and (not re.fullmatch(r'[0-9]+', str(length).strip())
                                                           or int(length) > MAX_VIDEO_BYTES):
                                    raise ValueError('视频响应长度无效或超过上限')
                                expected_size = None if length is None else int(length)
                                while True:
                                    if stop.is_set():
                                        raise InterruptedError('视频下载已停止')
                                    chunk = response.read(CHUNK_BYTES)
                                    if not chunk:
                                        break
                                    size += len(chunk)
                                    if size > MAX_VIDEO_BYTES:
                                        raise ValueError('视频超过100MB限制')
                                    if len(header) < 32:
                                        header = (header + chunk)[:32]
                                    digest.update(chunk)
                                    saved.write(chunk)
                            # 2.2.2 连接提前中止即使保留 MP4 标记，也不能视为下载成功。
                            if expected_size is not None and size != expected_size:
                                raise ValueError('视频响应长度与实际字节数不一致')
                            if len(header) < 12 or header[4:8] != b'ftyp':
                                raise ValueError('响应缺少 MP4 容器标记')
                            temporary.replace(final)
                        finally:
                            temporary.unlink(missing_ok=True)
                        with db:
                            db.execute('INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?)',
                                       (key, filename, digest.hexdigest(), size, datetime.now(timezone.utc).isoformat()))
                        report['downloaded'] += 1
                        if stop.wait(delay):
                            report['reason'] = 'cancelled'
                            break
                    if report['reason'] == 'cancelled':
                        break
        except InterruptedError:
            report['reason'] = 'cancelled'
        except HTTPError as exc:
            # 2.3 状态码足以说明失败，避免报告中出现带临时参数的链接。
            report.update(reason='error', error=f'视频请求失败：HTTP {exc.code}')
        except (ValueError, KeyError, TypeError, OSError, HTTPException) as exc:
            report.update(reason='error', error=f'视频采集失败：{type(exc).__name__}')
        logger.info('评论视频读取结束：%s', report['reason'])
        # /* ===== 步骤3：输出媒体清单；数据源：本地关联表；操作：1) 列出成功与未完成 2) 原子替换 ===== */
        logger.info('开始保存视频清单')
        temporary = output / ('video-manifest.' + uuid.uuid4().hex + '.part')
        try:
            with temporary.open('x', encoding='utf-8') as manifest:
                query = 'SELECT r.object_key,r.comment_id,r.url_hash,a.file,a.sha256,a.bytes,a.captured_at FROM refs r LEFT JOIN assets a ON r.url_hash=a.url_hash'
                for item in db.execute(query):
                    entry = dict(zip(('object_key','comment_id','url_hash','file','sha256','bytes','captured_at'), item))
                    entry.update(variant='returned_play_url', playback_verified=False)
                    manifest.write(json.dumps(entry, ensure_ascii=False) + '\n')
            temporary.replace(output / 'video-manifest.jsonl')
        finally:
            temporary.unlink(missing_ok=True)
        logger.info('视频清单保存完成')
    return report
