"""下载已采评论明确返回的图片版本，不猜测原图URL。"""
import hashlib
import io
import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from PIL import Image, ImageSequence

logger = logging.getLogger(__name__)
MAX_BYTES = 20 * 1024 * 1024


def validate_image_url(url):
    # /* ===== 步骤1：校验媒体地址；数据源：响应URL；操作：1) 限定图片CDN 2) 拒绝认证与非标准端口 ===== */
    logger.debug('开始校验图片地址')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not re.fullmatch(r'dimg\d+\.c-ctrip\.com', parsed.hostname or '')
            or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)):
        raise ValueError('图片地址不在已验证的携程图片CDN范围')
    logger.debug('图片地址校验完成')
    return url


def image_links(row):
    # /* ===== 步骤2：提取评论图片；数据源：公开图片数组；操作：1) 选择返回大图 2) 不遍历头像或跳转链接 ===== */
    logger.info('开始提取评论图片链接')
    data = row['data']
    for item in data.get('imageCuttingsList') or []:
        if item.get('bigImageUrl'):
            yield item['bigImageUrl']
    for item in data.get('images') or []:
        if item.get('imageSrcUrl'):
            yield item['imageSrcUrl']
    logger.info('评论图片链接提取完成')


class CheckedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # /* ===== 步骤3：验证跳转；数据源：响应Location；操作：1) 跳转前检查 2) 再发请求 ===== */
        logger.info('开始校验图片跳转')
        validate_image_url(newurl)
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        logger.info('图片跳转校验完成')
        return result


def fetch_image(url):
    # /* ===== 步骤4：读取图片正文；数据源：图片CDN；操作：1) 限制大小 2) 限制等待 ===== */
    logger.info('开始下载图片正文')
    validate_image_url(url)
    with build_opener(CheckedRedirect()).open(url, timeout=30) as response:
        payload = response.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError('图片超过20MB限制')
    logger.info('图片正文下载完成')
    return payload


def download_media(source, output, limit=20, stop=None, fetch=None, delay=1.5):
    # /* ===== 步骤5：持久化媒体；数据源：已导出评论JSONL；操作：1) 核验文件 2) 关联评论并支持续跑 ===== */
    logger.info('开始评论图片采集')
    if limit < 1:
        raise ValueError('图片数量上限必须大于零')
    output = Path(output).resolve()
    folder = output / 'media'
    folder.mkdir(parents=True, exist_ok=True)
    stop, fetch = stop or Event(), fetch or fetch_image
    report = {'downloaded': 0, 'reused': 0, 'reason': 'source_exhausted', 'error': '',
              'complete': False, 'source': str(Path(source).resolve()), 'limit': limit,
              'variant': 'returned_image_version', 'original_verified': False}
    seen = set()
    with closing(sqlite3.connect(output / 'media.sqlite3')) as db:
        db.execute('CREATE TABLE IF NOT EXISTS assets (url TEXT PRIMARY KEY, file TEXT, sha256 TEXT, width INTEGER, height INTEGER, format TEXT, captured_at TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS refs (object_key TEXT, comment_id TEXT, url TEXT, PRIMARY KEY(object_key,comment_id,url))')
        db.commit()
        try:
            with Path(source).open(encoding='utf-8-sig') as stream:
                for line in stream:
                    if stop.is_set():
                        report['reason'] = 'cancelled'
                        break
                    row = json.loads(line)
                    comment_id = row['data'].get('commentId') or row['data'].get('id')
                    if not comment_id or not row.get('object_key'):
                        raise ValueError('评论缺少对象或稳定评论ID')
                    for url in image_links(row):
                        validate_image_url(url)
                        if url not in seen and len(seen) >= limit:
                            report['reason'] = 'image_budget_reached'
                            break
                        if stop.is_set():
                            report['reason'] = 'cancelled'
                            break
                        # 5.1 相同URL保留多评论关联，但不重复下载。
                        with db:
                            db.execute('INSERT OR IGNORE INTO refs VALUES (?,?,?)', (row['object_key'], str(comment_id), url))
                        if url in seen:
                            continue
                        seen.add(url)
                        existing = db.execute('SELECT file,sha256 FROM assets WHERE url=?', (url,)).fetchone()
                        valid = False
                        if existing:
                            path = folder / existing[0]
                            if path.parent == folder and path.is_file():
                                with path.open('rb') as saved:
                                    valid = hashlib.file_digest(saved, 'sha256').hexdigest() == existing[1]
                        if valid:
                            report['reused'] += 1
                            continue
                        with db:
                            db.execute('DELETE FROM assets WHERE url=?', (url,))
                        payload = fetch(url)
                        if len(payload) > MAX_BYTES:
                            raise ValueError('图片超过20MB限制')
                        # 5.2 解码头和完整性检查，HTML、损坏文件不作为成功图片。
                        with Image.open(io.BytesIO(payload)) as image:
                            width, height = image.size
                            fmt = image.format
                            if fmt not in {'JPEG', 'PNG', 'WEBP', 'GIF'} or width * height > 50_000_000:
                                raise ValueError('图片格式或像素数量未支持')
                            image.verify()
                        with Image.open(io.BytesIO(payload)) as decoded:
                            pixels = 0
                            for frame in ImageSequence.Iterator(decoded):
                                pixels += frame.width * frame.height
                                if pixels > 50_000_000:
                                    raise ValueError('图片所有帧合计超过5000万像素限制')
                                frame.load()
                        filename = hashlib.sha256(url.encode()).hexdigest() + '.' + fmt.lower()
                        path = folder / filename
                        temporary = path.with_suffix('.part')
                        temporary.write_bytes(payload)
                        temporary.replace(path)
                        with db:
                            db.execute('INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?,?,?)',
                                       (url, filename, hashlib.sha256(payload).hexdigest(), width, height, fmt,
                                        datetime.now(timezone.utc).isoformat()))
                        report['downloaded'] += 1
                        if stop.wait(delay):
                            report['reason'] = 'cancelled'
                            break
                    if report['reason'] in {'image_budget_reached', 'cancelled'}:
                        break
        except Exception as exc:
            report.update(reason='error', error=str(exc))
        # 5.3 清单保留未成功关联，图片不是AI优选集合，也不宣称原图。
        with (output / 'media-manifest.jsonl').open('w', encoding='utf-8') as manifest:
            for item in db.execute('SELECT r.object_key,r.comment_id,r.url,a.file,a.sha256,a.width,a.height,a.format,a.captured_at FROM refs r LEFT JOIN assets a ON r.url=a.url'):
                entry = dict(zip(('object_key', 'comment_id', 'url', 'file', 'sha256', 'width', 'height', 'format', 'captured_at'), item))
                entry.update(variant='returned_image_version', original_verified=False)
                manifest.write(json.dumps(entry, ensure_ascii=False) + '\n')
    (output / 'media-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info('评论图片采集结束：%s', report['reason'])
    return report
