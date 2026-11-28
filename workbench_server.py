"""Loopback-only workbench API backed by actual collection tasks."""
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import webbrowser
from urllib.parse import urlsplit, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zipfile import BadZipFile, ZipFile
from object_identity import list_object_refs
from mobile_sight_queue import list_sight_queues
from sight_queue_ai_export import build_sight_queue_ai_export
from workbench_jobs import make_job
from workbench_sight_gap_plan import build_sight_gap_plan
from app_page_fields import list_raw_page_runs
from screenshot_ocr import MAX_IMAGE_BYTES, recognize_screenshot
from workbench_tasks import TaskManager
from workbench_preview import read_preview, read_mobile_preview, read_scoped_mobile_preview, open_network_observations
from mobile_observations import read_observations
from mobile_review_recovery import open_saved_review_sample, saved_review_runs
from workbench_result_files import (open_hotel_report, open_task_csv, open_review_thumbnails,
                                    open_restaurant_review_gallery, open_sight_gallery, open_sight_photo_gallery, open_sight_replay_csv,
                                    open_output_file)
from hotel_review_bundle import open_hotel_review_bundle


class LoopbackServer(ThreadingHTTPServer):
    # /* ===== 步骤1：独占监听端口；数据源：本机端口；操作：1) 关闭地址复用 2) Windows 用独占标志 ===== */
    # 1.1 Windows 的 SO_REUSEADDR 允许后启动的进程抢占同一端口，先启动的工作台会静默
    #     收不到任何请求；这里改成独占监听，第二次启动直接报端口被占用。
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
from runner import Adb, AndroidReadinessError
from sight_photo_gallery import MAX_REPORT_BYTES, SOURCE as SIGHT_PHOTO_GALLERY_SOURCE

logger = logging.getLogger(__name__)
# 1.0 本地库正被采集任务写入时，读接口给固定说明而不是断开连接。
BUSY_REPLY = '本地数据库正被写入，请稍后再试'
SIGHT_GALLERY_REPORT = re.compile(r'sight-photo-gallery-([a-f0-9]{32})\.json')


def sight_gallery_resume_option(output, path, run_id, modified):
    archive = path.with_suffix('.zip')
    try:
        with open_output_file(output, path) as stream:
            data = stream.read(MAX_REPORT_BYTES + 1)
        if len(data) > MAX_REPORT_BYTES:
            return None
        report = json.loads(data)
        if (not isinstance(report, dict) or report.get('run_id') != run_id or report.get('kind') != 'sight'
                or report.get('source') != 'ctrip_android_ui' or report.get('source_method') != SIGHT_PHOTO_GALLERY_SOURCE
                or report.get('archive') != archive.name):
            return None
        name, pages, total = report.get('name'), report.get('pages'), report.get('gallery_total')
        if not isinstance(name, str) or not name.strip() or not isinstance(pages, list) or not pages:
            return None
        last_page = pages[-1].get('page') if isinstance(pages[-1], dict) else None
        if (type(last_page) is not int or type(total) is not int or not 1 <= last_page < total
                or report.get('last_page', last_page) != last_page):
            return None
        with open_output_file(output, archive):
            return {'run_id': run_id, 'name': name, 'last_page': last_page, 'modified': modified}
    except (OSError, ValueError, RecursionError):
        return None


def android_readiness(adb_path, adb_serial):
    if not adb_path:
        return {'state': 'not_configured', 'device': None}
    adb = Adb(adb_path, serial=adb_serial, command_timeout=3)
    try:
        adb.connect()
        adb.ensure_foreground()
        state = 'foreground'
    except AndroidReadinessError as exc:
        state = exc.state
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        state = 'check_failed'
    serial = adb.serial
    kind = 'emulator' if re.fullmatch(r'emulator-[0-9]+', serial or '') else 'android' if serial else None
    return {'state': state, 'device': kind}


def create_server(output, adb_path=None, port=8765, *, adb_serial=None):
    # /* ===== 步骤1：建立本地服务；数据源：本机参数；操作：1) 单任务管理 2) 固定输出目录 ===== */
    logger.info('开始建立工作台服务')
    manager = TaskManager()
    output = Path(output).resolve()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, value, *, filename=None):
            # /* ===== 步骤1：输出接口结果；数据源：任务状态；操作：1) UTF8编码 2) 禁止缓存 ===== */
            logger.info('开始发送接口响应')
            body = json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if filename is not None:
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)
            logger.info('接口响应发送完成')

        def local_request(self):
            # /* ===== 步骤1：核验访问来源；数据源：HTTP头；操作：1) 限定Host 2) 拒绝外站Origin ===== */
            logger.info('开始核验本地请求')
            host = f'127.0.0.1:{self.server.server_port}'
            valid = (self.headers.get('Host') == host and self.headers.get('Origin') in (None, 'http://' + host)
                     and self.headers.get('Sec-Fetch-Site') not in ('cross-site', 'same-site'))
            logger.info('本地请求核验完成：%s', valid)
            return valid

        def do_GET(self):
            # /* ===== 步骤1：读取任务；数据源：管理器快照；操作：1) 核验来源 2) 返回真实状态 ===== */
            logger.info('开始读取工作台状态')
            if not self.local_request():
                self.reply(403, {'error': '仅允许本地工作台访问'})
            elif self.path == '/favicon.ico':
                # 1.0 浏览器每次打开都会要图标；明确回空响应，避免控制台与日志里的 404 噪音。
                self.send_response(204)
                self.send_header('Content-Length', '0')
                self.end_headers()
            elif self.path == '/':
                # 1.1 只提供固定工作台页面，不开放任意本地文件路径。
                body = Path(__file__).with_name('workbench.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Frame-Options', 'DENY')
                self.end_headers()
                self.wfile.write(body)
            elif self.path == '/api/state':
                self.reply(200, manager.snapshot())
            elif self.path == '/api/android-readiness':
                self.reply(200, android_readiness(adb_path, adb_serial))
            elif urlsplit(self.path).path == '/api/object-options':
                try:
                    params = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if params.keys() - {'kind', 'q'} or any(len(values) != 1 for values in params.values()):
                        raise ValueError('对象目录查询条件无效')
                    kind = params.get('kind', [None])[0]
                    query = params.get('q', [''])[0]
                    self.reply(200, list_object_refs(output, kind=kind, query=query))
                except ValueError as exc:
                    self.reply(400, {'error': str(exc)})
                except OSError:
                    self.reply(500, {'error': '无法读取本地对象目录'})
            elif self.path == '/api/local-files':
                # 1.2 只检查输出目录直属的规范报告和评论文件。
                reports, media_sources, capture_files, graph_reports = [], [], [], []
                sight_gallery_reports = []
                if output.is_dir():
                    for entry in output.iterdir():
                        if entry.is_symlink() or not entry.is_file() or entry.resolve().parent != output:
                            continue
                        if re.fullmatch(r'discovery-regions-[0-9a-f]{32}\.json', entry.name):
                            reports.append({'name': entry.name, 'modified': entry.stat().st_mtime})
                        elif re.fullmatch(r'related-graph-[0-9a-f]{32}\.json', entry.name) and not entry.is_junction():
                            graph_reports.append({'name': entry.name, 'modified': entry.stat().st_mtime})
                        elif (match := SIGHT_GALLERY_REPORT.fullmatch(entry.name)) and not entry.is_junction():
                            sight_gallery_reports.append((entry, match[1], entry.stat().st_mtime))
                        elif re.fullmatch(r'(hotel|sight|restaurant|shopping)-[1-9][0-9]*\.jsonl', entry.name):
                            media_sources.append({'name': entry.name, 'modified': entry.stat().st_mtime})
                        elif (re.fullmatch(r'hotel-[1-9][0-9]*-app-capture-batches-[0-9a-f]{32}\.jsonl', entry.name)
                              and not entry.is_junction()):
                            # 1.2.0 仅列出程序生成的批次归档，供后续提高目标条数时继续导入。
                            capture_files.append({'name': 'archive:' + entry.name, 'modified': entry.stat().st_mtime})
                reports.sort(key=lambda x: x['modified'], reverse=True)
                media_sources.sort(key=lambda x: x['modified'], reverse=True)
                graph_reports.sort(key=lambda x: x['modified'], reverse=True)
                # 1.2.1 只列出固定 captures 目录的受限响应文件，不读取或上传内容。
                captures = output / 'captures'
                if (not captures.is_symlink() and not captures.is_junction() and captures.is_dir()
                        and captures.resolve().parent == output):
                    for entry in captures.iterdir():
                        if (re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.jsonl', entry.name)
                                and not entry.is_symlink() and entry.is_file() and entry.resolve().parent == captures.resolve()):
                            capture_files.append({'name': entry.name, 'modified': entry.stat().st_mtime})
                capture_files.sort(key=lambda x: x['modified'], reverse=True)
                sight_gallery_reports.sort(key=lambda item: item[2], reverse=True)
                gallery_runs = [option for path, run_id, modified in sight_gallery_reports[:200]
                                if (option := sight_gallery_resume_option(output, path, run_id, modified)) is not None]
                self.reply(200, {'reports': reports[:200], 'media_sources': media_sources[:200],
                                 'capture_files': capture_files[:200], 'graph_reports': graph_reports[:200],
                                 'sight_gallery_runs': gallery_runs})
            elif self.path == '/api/raw-page-runs':
                try:
                    self.reply(200, {'runs': list_raw_page_runs(output)})
                except OSError:
                    self.reply(500, {'error': '无法列出已保存的 App 页面任务'})
            elif self.path == '/api/sight-review-queues':
                self.reply(200, {'queues': list_sight_queues(output)})
            elif self.path == '/api/sight-gap-plan':
                try:
                    self.reply(200, build_sight_gap_plan(output))
                except ValueError as exc:
                    self.reply(400, {'error': str(exc)})
                except (OSError, sqlite3.Error):
                    self.reply(500, {'error': '无法读取本地景点待补清单'})
            elif urlsplit(self.path).path == '/api/sight-queue-ai':
                params = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                queues = params.get('queue', [])
                try:
                    if params.keys() != {'queue'} or len(queues) != 1:
                        raise ValueError('景点点评队列编号无效')
                    bundle = build_sight_queue_ai_export(output, queues[0])
                except (ValueError, OSError):
                    self.reply(404, {'error': '找不到此队列可导出的景点 AI 总结'})
                else:
                    self.reply(200, bundle, filename=f'sight-queue-ai-{bundle["queue_id"]}.json')
            elif self.path == '/api/saved-review-runs':
                try:
                    self.reply(200, {'runs': saved_review_runs(output)})
                except sqlite3.Error:
                    self.reply(500, {'error': '无法读取本地点评任务'})
            elif urlsplit(self.path).path in {'/api/saved-review-sample', '/api/sight-review-replay'}:
                parsed = urlsplit(self.path)
                run_id = parse_qs(parsed.query).get('run', [''])[0]
                replay = parsed.path == '/api/sight-review-replay'
                try:
                    stream = (open_sight_replay_csv(output, manager.snapshot(), run_id) if replay
                              else open_saved_review_sample(output, run_id))
                except (ValueError, LookupError, FileNotFoundError) as exc:
                    self.reply(404, {'error': str(exc)})
                    return
                except (sqlite3.Error, OSError):
                    self.reply(500, {'error': '下载离线重读结果失败' if replay else '恢复点评样本失败'})
                    return
                with stream:
                    size = stream.seek(0, os.SEEK_END)
                    stream.seek(0)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    name = f'sight-review-replay-{run_id}.csv' if replay else f'review-sample-{run_id}.csv'
                    self.send_header('Content-Disposition', f'attachment; filename="{name}"')
                    self.send_header('Content-Length', str(size))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()
                    shutil.copyfileobj(stream, self.wfile, length=65536)
            elif self.path in {'/api/task-csv', '/api/task-sample-csv'}:
                # 1.3 下载当前任务报告指向的手机评论文件，不接受客户端路径。
                sample = self.path == '/api/task-sample-csv'
                try:
                    with open_task_csv(output, manager.snapshot(), sample=sample) as stream:
                        size = os.fstat(stream.fileno()).st_size
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/octet-stream')
                        name = 'mobile-review-sample.csv' if sample else 'mobile-reviews.csv'
                        self.send_header('Content-Disposition', f'attachment; filename="{name}"')
                        self.send_header('Content-Length', str(size))
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        self.end_headers()
                        shutil.copyfileobj(stream, self.wfile, length=65536)
                except (ValueError, OSError) as exc:
                    self.reply(404, {'error': str(exc)})
                    return
            elif urlsplit(self.path).path == '/api/hotel-review-bundle':
                run = parse_qs(urlsplit(self.path).query).get('run', [''])[0]
                snapshot = manager.snapshot()
                if not run or (snapshot['result'] or {}).get('run_id') != run:
                    self.reply(404, {'error': '当前任务没有这份点评样本'})
                    return
                try:
                    stream = open_hotel_review_bundle(output, snapshot)
                except LookupError as exc:
                    self.reply(404, {'error': str(exc)})
                    return
                except (ValueError, OSError):
                    self.reply(404, {'error': '这份酒店点评或 AI 总结暂不可用'})
                    return
                with stream:
                    size = stream.seek(0, os.SEEK_END)
                    stream.seek(0)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/zip')
                    self.send_header('Content-Disposition', 'attachment; filename="hotel-review-research.zip"')
                    self.send_header('Content-Length', str(size))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()
                    shutil.copyfileobj(stream, self.wfile, length=65536)
            elif urlsplit(self.path).path == '/api/sight-gallery':
                query = parse_qs(urlsplit(self.path).query)
                report_id = query.get('run', [''])[0]
                tile = query.get('tile', [None])[0]
                try:
                    with open_sight_gallery(output, manager.snapshot(), report_id) as stream:
                        if tile is None:
                            mime = 'application/zip'
                            size = os.fstat(stream.fileno()).st_size
                        elif re.fullmatch(r'[1-9][0-9]?', tile):
                            mime = 'image/png'
                            with ZipFile(stream) as archive:
                                entry = archive.getinfo(f'thumbnails/{int(tile):02d}.png')
                                if entry.file_size > 4 * 1024 * 1024:
                                    raise ValueError('裁图超过预览大小')
                                picture = archive.read(entry)
                                if not picture.startswith(b'\x89PNG\r\n\x1a\n'):
                                    raise ValueError('裁图格式无效')
                            size = len(picture)
                        else:
                            raise ValueError('裁图序号无效')
                        self.send_response(200)
                        self.send_header('Content-Type', mime)
                        if tile is None:
                            self.send_header('Content-Disposition', 'attachment; filename="sight-gallery-thumbnails.zip"')
                        self.send_header('Content-Length', str(size))
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        self.end_headers()
                        if tile is None:
                            shutil.copyfileobj(stream, self.wfile, length=65536)
                        else:
                            self.wfile.write(picture)
                except (ValueError, OSError, BadZipFile, KeyError):
                    self.reply(404, {'error': '这份景点晒图暂不可用'})
            elif urlsplit(self.path).path == '/api/sight-photo-gallery':
                run_id = parse_qs(urlsplit(self.path).query).get('run', [''])[0]
                try:
                    stream = open_sight_photo_gallery(output, manager.snapshot(), run_id)
                except (ValueError, OSError):
                    self.reply(404, {'error': '这份景点图集暂不可用'})
                    return
                with stream:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/zip')
                    self.send_header('Content-Disposition', 'attachment; filename="sight-photo-gallery.zip"')
                    self.send_header('Content-Length', str(os.fstat(stream.fileno()).st_size))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()
                    shutil.copyfileobj(stream, self.wfile, length=65536)
            elif urlsplit(self.path).path in {'/api/review-thumbnails', '/api/restaurant-review-gallery'}:
                request_url = urlsplit(self.path)
                gallery = request_url.path == '/api/restaurant-review-gallery'
                runs = parse_qs(request_url.query, keep_blank_values=gallery).get('run', [''])
                open_zip = open_restaurant_review_gallery if gallery else open_review_thumbnails
                run = runs[0] if not gallery or len(runs) == 1 else ''
                try:
                    stream, filename = open_zip(output, manager.snapshot(), run)
                except (ValueError, OSError) as exc:
                    self.reply(404, {'error': str(exc) if isinstance(exc, ValueError) else '点评图片文件不可用'})
                    return
                with stream:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/zip')
                    self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
                    self.send_header('Content-Length', str(os.fstat(stream.fileno()).st_size))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()
                    shutil.copyfileobj(stream, self.wfile, length=65536)
            elif urlsplit(self.path).path == '/api/hotel-report':
                query = parse_qs(urlsplit(self.path).query)
                name = query.get('file', [''])[0]
                try:
                    stream = open_hotel_report(output, name)
                except (ValueError, OSError):
                    self.reply(404, {'error': '这份酒店房型报告不可用'})
                    return
                with stream:
                    size = os.fstat(stream.fileno()).st_size
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Disposition', f'attachment; filename="{name}"')
                    self.send_header('Content-Length', str(size))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()
                    shutil.copyfileobj(stream, self.wfile, length=65536)
            elif urlsplit(self.path).path == '/api/preview':
                try:
                    cursor = parse_qs(urlsplit(self.path).query).get('before', [None])[0]
                    self.reply(200, read_preview(output, before=int(cursor) if cursor is not None else None))
                except ValueError as exc:
                    self.reply(400, {'error': str(exc)})
                except sqlite3.Error:
                    self.reply(503, {'error': BUSY_REPLY})
            elif urlsplit(self.path).path == '/api/mobile-preview':
                try:
                    cursor = parse_qs(urlsplit(self.path).query).get('before', [None])[0]
                    self.reply(200, read_mobile_preview(output, before=int(cursor) if cursor is not None else None))
                except ValueError as exc:
                    self.reply(400, {'error': str(exc)})
                except sqlite3.Error:
                    self.reply(503, {'error': BUSY_REPLY})
            elif urlsplit(self.path).path == '/api/scoped-mobile-preview':
                try:
                    cursor = parse_qs(urlsplit(self.path).query).get('before', [None])[0]
                    self.reply(200, read_scoped_mobile_preview(output, before=int(cursor) if cursor is not None else None))
                except ValueError as exc:
                    self.reply(400, {'error': str(exc)})
                except sqlite3.Error:
                    self.reply(503, {'error': BUSY_REPLY})
            elif urlsplit(self.path).path == '/api/mobile-observations':
                # 1.4 只读取所选评论的观察摘要，不接受文件路径或返回原始界面。
                try:
                    query = parse_qs(urlsplit(self.path).query)
                    key = query.get('review', [''])[0]
                    cursor = query.get('before', [None])[0]
                    self.reply(200, read_observations(output, key, before=int(cursor) if cursor is not None else None))
                except ValueError as exc:
                    self.reply(400, {'error': str(exc)})
                except sqlite3.Error:
                    self.reply(503, {'error': BUSY_REPLY})
                except (FileNotFoundError, LookupError) as exc:
                    self.reply(404, {'error': str(exc)})
            elif urlsplit(self.path).path == '/api/export':
                # 1.2 只下载采集器生成的规范对象文件；大文件分块发送。
                query = parse_qs(urlsplit(self.path).query)
                key = query.get('object', [''])[0]
                kind = query.get('format', [''])[0]
                if kind == 'observations':
                    # 1.2.1 观察文件只能从当前目录对应对象读取，打开后复核句柄。
                    if not re.fullmatch(r'(hotel|sight|restaurant|shopping)-[1-9][0-9]*', key):
                        self.reply(400, {'error': '导出对象无效'})
                        return
                    try:
                        stream = open_network_observations(output, key)
                    except (ValueError, OSError):
                        self.reply(404, {'error': '观察文件不可用'})
                        return
                    with stream:
                        size = os.fstat(stream.fileno()).st_size
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/octet-stream')
                        self.send_header('Content-Disposition', f'attachment; filename="{key}-observations.jsonl"')
                        self.send_header('Content-Length', str(size))
                        self.send_header('Cache-Control', 'no-store')
                        self.send_header('X-Content-Type-Options', 'nosniff')
                        self.end_headers()
                        shutil.copyfileobj(stream, self.wfile, length=65536)
                    return
                if not re.fullmatch(r'(hotel|sight|restaurant|shopping)-[0-9]+', key) or kind not in {'csv', 'jsonl'}:
                    self.reply(400, {'error': '导出对象或格式无效'})
                    return
                path = (output / f'{key}.{kind}').resolve()
                if path.parent != output or not path.is_file():
                    self.reply(404, {'error': '导出文件尚未生成'})
                    return
                with path.open('rb') as stream:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Disposition', f'attachment; filename="{path.name}"')
                    self.send_header('Content-Length', str(path.stat().st_size))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Content-Type-Options', 'nosniff')
                    self.end_headers()
                    shutil.copyfileobj(stream, self.wfile, length=65536)
            else:
                self.reply(404, {'error': '页面尚未接入'})
            logger.info('工作台状态读取结束')

        def do_POST(self):
            # /* ===== 步骤1：接收任务操作；数据源：JSON表单；操作：1) 有界读取 2) 参数验证后启动 ===== */
            logger.info('开始处理任务操作')
            if not self.local_request():
                self.reply(403, {'error': '拒绝外部来源控制采集'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if self.path == '/api/screenshot-ocr':
                    if not 0 < length <= MAX_IMAGE_BYTES or self.headers.get_content_type() not in {'image/png', 'image/jpeg'}:
                        raise ValueError('请选择不超过 8 MB 的 PNG 或 JPEG 图片')
                    image = self.rfile.read(length)
                    if len(image) != length:
                        raise ValueError('截图接收不完整')
                    manager.start(lambda stop, emit: recognize_screenshot(image, output, stop, emit))
                    self.reply(200, manager.snapshot())
                    return
                if not 0 < length <= 65536 or self.headers.get_content_type() != 'application/json':
                    raise ValueError('需要不超过64KB的JSON请求')
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict):
                    raise ValueError('请求必须是JSON对象')
                # 1.1 输出路径和ADB由启动参数提供，不允许网页传入任意命令。
                if self.path == '/api/start':
                    manager.start(make_job(request, output, adb_path, adb_serial=adb_serial))
                elif self.path == '/api/stop':
                    manager.cancel()
                else:
                    self.reply(404, {'error': '未知操作'})
                    return
                self.reply(200, manager.snapshot())
            except (ValueError, TypeError) as exc:
                self.reply(400, {'error': str(exc)})
            except RuntimeError as exc:
                self.reply(409, {'error': str(exc)})
            logger.info('任务操作处理结束')

    try:
        server = LoopbackServer(('127.0.0.1', port), Handler)
    except OSError as exc:
        # 1.2 端口被占用时给一句能照做的说明，而不是抛原始 WinError。
        raise ValueError(f'端口 {port} 已被占用，请换一个端口，或用 --port 0 自动选择') from exc
    server.manager = manager
    logger.info('工作台服务建立完成')
    return server


def serve_workbench(output, adb_path=None, port=8765, *, adb_serial=None, open_browser=False):
    logger.info('开始运行工作台服务')
    server = create_server(output, adb_path, port, adb_serial=adb_serial)
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        print(url, flush=True)
        if open_browser:
            try:
                opened = webbrowser.open(url, new=2)
            except (webbrowser.Error, OSError):
                opened = False
            if not opened:
                logger.warning('请在浏览器中打开 %s', url)
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('收到关闭请求，等待正在进行的任务保存')
    finally:
        try:
            server.manager.close()
        finally:
            server.server_close()
    logger.info('工作台服务结束')


if __name__ == '__main__':
    import argparse

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--adb')
    parser.add_argument('--adb-serial', help='固定 Android 设备编号，例如 emulator-5554')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if args.adb_serial and not args.adb:
        parser.error('--adb-serial 需要同时指定 --adb')
    serve_workbench(args.output, args.adb, args.port, adb_serial=args.adb_serial)
