"""受限 App 响应格式与离线导入；未知接口不保存内容。"""
import hashlib
import errno
import json
import logging
import os
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from hotel_browser import hotel_batch
from network import collect_network, sanitize

logger = logging.getLogger(__name__)
FIELDS = ('hotelName', 'totalCount', 'groupList', 'aiSummaryEntities', 'selectedCommentPhotosEntities',
          'commentRating', 'commentTagList', 'roomList', 'travelTypeList', 'filteredOutCommentInfo', 'staticInfo')
PRIVATE = {'phone', 'mobile', 'email', 'realname', 'idcard', 'idnumber', 'passport', 'password'}


@contextmanager
def capture_object_lock(folder, identity, stop):
    # /* ===== 步骤1：锁定酒店导入；数据源：输出目录；操作：1) 跨进程锁 2) 异常或退出时释放 ===== */
    logger.info('开始锁定酒店抓包导入')
    # 1.1 系统文件锁随进程退出自动释放，不依赖锁文件是否仍存在。
    lock_path = folder / f'.hotel-{identity}-app-capture.lock'
    with lock_path.open('a+b') as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b'\0')
            lock.flush()
        deadline = time.monotonic() + 30
        locked = False
        while not locked:
            if stop is not None and stop.is_set():
                raise InterruptedError('已取消等待酒店抓包导入锁')
            try:
                lock.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) and getattr(exc, 'winerror', None) not in (33, 36):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError('等待同一酒店抓包导入超时')
                if stop is not None:
                    stop.wait(.1)
                else:
                    time.sleep(.1)
        try:
            yield
        finally:
            # 1.2 仅释放本次句柄上的锁，保留锁文件供其他进程复用。
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    logger.info('酒店抓包导入锁已释放')


def save_capture_history(folder, identity, *, source=None, target=None):
    # /* ===== 步骤1：保存独立快照；数据源：既有对象或本次响应；操作：1) 写临时文件 2) 完成后原子发布 ===== */
    logger.info('开始保存酒店抓包历史')
    # 1.1 唯一文件名避免并发覆盖；先保存原字节以兼容旧格式。
    name = f'hotel-{identity}-app-capture-history-{uuid.uuid4().hex}.json'
    final = folder / name
    temporary = folder / (name + '.tmp')
    try:
        with temporary.open('xb') as stream:
            if source is not None:
                with source.open('rb') as previous:
                    shutil.copyfileobj(previous, stream)
            else:
                target['capture_history_file'] = name
                stream.write(json.dumps(target, ensure_ascii=False, indent=2).encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, final)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    logger.info('酒店抓包历史保存完成：%s', name)
    return final


def save_capture_batches(folder, identity, records):
    # /* ===== 步骤1：保存可恢复批次；数据源：已校验响应；操作：1) 限定字段并清洗 2) 原子发布 ===== */
    logger.info('开始保存酒店抓包批次')
    name = f'hotel-{identity}-app-capture-batches-{uuid.uuid4().hex}.jsonl'
    final = folder / name
    temporary = folder / (name + '.tmp')
    try:
        with temporary.open('xb') as stream:
            # 1.1 从已校验记录重建受限格式，摘要覆盖清洗后的内容。
            for record, _ in records:
                payload = clean({key: record['payload'][key] for key in FIELDS if key in record['payload']})
                paging = record.get('paging')
                paging = paging if isinstance(paging, dict) else {}
                safe = {'schema': 1, 'kind': 'hotel_reviews', 'source_channel': 'app_proxy',
                        'captured_at': record['captured_at'], 'hotel_id': identity,
                        'paging': {key: paging[key] for key in ('pageIndex', 'pageSize')
                                   if type(paging.get(key)) is int and paging[key] > 0},
                        'payload': payload,
                        'payload_sha256': hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
                stream.write((json.dumps(safe, ensure_ascii=False) + '\n').encode('utf-8'))
            # 1.2 完成落盘再发布，失败时清除临时文件。
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, final)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    logger.info('酒店抓包批次保存完成：%s', name)
    return final


def clean(value):
    # 1.1 已知响应仍递归删除账号字段；不接收原请求正文。
    if isinstance(value, dict):
        return {key: clean(child) for key, child in sanitize(value).items() if key.lower() not in PRIVATE}
    if isinstance(value, list):
        return [clean(child) for child in value]
    return value


def has_content(value):
    # 1.1 空列表和嵌套空字典不构成有效内容，数字零可表示真实统计值。
    if isinstance(value, dict):
        return any(has_content(child) for child in value.values())
    if isinstance(value, list):
        return any(has_content(child) for child in value)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def has_summary(value):
    # 1.2 只把带内容的总结条目列表计为已采集。
    return (isinstance(value, dict) and isinstance(value.get('summaryItemList'), list)
            and has_content(value['summaryItemList']))


def capture_record(url, request, response, status):
    # /* ===== 步骤1：筛选流量；数据源：内存中的业务响应；操作：1) 域名边界 2) 已知结构与身份 3) 未知仅记接口名 ===== */
    logger.info('开始筛选业务响应')
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if not any(host == base or host.endswith('.' + base) for base in ('ctrip.com', 'trip.com')):
        return None
    match = re.fullmatch(r'/restapi/soa2/(\d+)/(?:json/)?([A-Za-z][A-Za-z0-9_]*)(?:\.json)?', parsed.path)
    if not match:
        return None
    # 1.2 不写完整 URL、查询串、请求头、响应头或请求正文。
    record = {'schema': 1, 'kind': 'endpoint_metadata', 'source_channel': 'app_proxy',
              'captured_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
              'host': host, 'service': match[1], 'action': match[2], 'status': status}
    if match.groups() == ('34308', 'getHotelCommentInfo') and status == 200:
        try:
            identity = int(request['hotelId'])
            if identity < 1:
                raise ValueError('酒店 ID 无效')
            data = response['data']
            hotel_batch(data, identity)
            payload = clean({key: data[key] for key in FIELDS if key in data})
            options = request.get('commentFilterOptions') or {}
            # 1.3 只记录页码和数量；任意筛选内容不写入探测记录。
            paging = {key: options[key] for key in ('pageIndex', 'pageSize')
                      if isinstance(options.get(key), int) and options[key] > 0}
            record.update(kind='hotel_reviews', hotel_id=identity, paging=paging, payload=payload,
                          payload_sha256=hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        except (KeyError, TypeError, ValueError, AttributeError):
            record['reason'] = 'unrecognized_or_wrong_identity'
    logger.info('业务响应筛选完成：%s', record['kind'])
    return record


def import_capture(source, output, limit=500, stop=None, emit=None, on_result=None, *, refresh=False):
    # /* ===== 步骤2：导入已筛选响应；数据源：本工具 JSONL；操作：1) 全文件先验证 2) 按酒店分组 3) 共用稳定 ID 去重 ===== */
    logger.info('开始导入 App 响应')
    if limit < 1:
        raise ValueError('目标条数必须大于零')
    # 2.0 刷新仅接受明确布尔值，不能让字符串或整数静默改变采集范围。
    if type(refresh) is not bool:
        raise ValueError('刷新选项必须为布尔值')
    groups = {}
    if stop is not None and stop.is_set():
        return []
    # 2.1 限制行长；旧骨架的原始抓包格式不直接导入。
    with Path(source).open(encoding='utf-8-sig') as file:
        while line := file.readline(5_000_001):
            if stop is not None and stop.is_set():
                return []
            if len(line) > 5_000_000:
                raise ValueError('响应记录过大，拒绝导入')
            record = json.loads(line)
            if record.get('schema') != 1 or record.get('kind') not in ('endpoint_metadata', 'hotel_reviews'):
                raise ValueError('不是受限响应格式，不能直接导入原始抓包文件')
            if record['kind'] == 'endpoint_metadata':
                continue
            identity = int(record['hotel_id'])
            if identity < 1:
                raise ValueError('酒店 ID 无效')
            datetime.fromisoformat(record['captured_at'])
            payload = record['payload']
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if digest != record.get('payload_sha256'):
                raise ValueError('响应摘要不一致')
            batch = hotel_batch(clean(payload), identity)
            for item in batch['result']['items']:
                item['source_schema'] = 'app_proxy_capture'
                item['captured_at'] = record['captured_at']
            groups.setdefault(identity, []).append((record, batch))
    if not groups:
        raise ValueError('文件只有接口名称，没有可导入的已验证酒店点评响应')
    results = []
    for identity, records in groups.items():
        if stop is not None and stop.is_set():
            break
        # 2.2 选取已捕获 AI 内容，不用后续空字段覆盖较完整首批。
        data = records[0][0]['payload']
        # 2.2.0 优先展示实际有总结的批次；空列表不能遮住后续有效内容。
        ai = next((r['payload']['aiSummaryEntities'] for r, _ in records
                   if has_summary(r['payload'].get('aiSummaryEntities'))), {})
        # 2.2.0 各批次AI总结可能不同，保留每次捕获时间与清洗后内容。
        summaries = [{'captured_at': r['captured_at'], 'content': clean(r['payload']['aiSummaryEntities'])}
                     for r, _ in records if 'aiSummaryEntities' in r['payload']]
        # 2.2.1 不同批次可能带不同图集元数据；逐批留存，不将元数据当已下载图片。
        photos = [{'captured_at': r['captured_at'], 'content': clean(r['payload']['selectedCommentPhotosEntities'])}
                  for r, _ in records if 'selectedCommentPhotosEntities' in r['payload']]
        target = {'kind': 'hotel', 'resource_id': identity, 'name': data.get('hotelName') or str(identity),
                  'url': f'https://hotels.ctrip.com/hotels/{identity}.html', 'display_total': data['totalCount'],
                  'source_method': 'app_proxy_capture', 'page_size': None,
                  'details': clean({key: data[key] for key in FIELDS if key in data and key != 'groupList'}),
                  'ai_summary': {'status': 'response_captured' if any(has_summary(row['content']) for row in summaries) else 'not_captured',
                                 'content': clean(ai), 'response_observations': summaries},
                  'ai_photos': {'status': 'response_captured' if any(has_content(row['content']) for row in photos) else 'not_captured',
                                'content': next((row['content'] for row in photos if has_content(row['content'])), None),
                                'response_observations': photos, 'files_downloaded': False},
                  'capture_pages': [{key: r.get('paging', {})[key] for key in ('pageIndex', 'pageSize')
                                     if isinstance(r.get('paging', {}).get(key), int) and r['paging'][key] > 0}
                                    for r, _ in records],
                  'captured_at': [r['captured_at'] for r, _ in records]}
        def fetch(index):
            return records[index - 1][1]
        # 2.3 同酒店导入串行，先保存旧文件和新观察，之后才允许更新最新对象。
        folder = Path(output)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            with capture_object_lock(folder, identity, stop):
                # 2.3.1 保存全部已校验批次；未入库的数据不能算作已采。
                archive = save_capture_batches(folder, identity, records)
                previous = folder / f'hotel-{identity}-app-capture-object.json'
                if previous.exists():
                    save_capture_history(folder, identity, source=previous)
                history = save_capture_history(folder, identity, target=target)
                # 2.3.2 显式刷新以本次响应的不同评论计数；默认保留原有累计计数行为。
                options = {'refresh': True} if refresh else {}
                report = collect_network(target, folder, limit, stop, emit, fetch=fetch, delay=0, max_pages=len(records), **options)
                report['object_history'] = str(history)
                report['capture_archive'] = str(archive.resolve())
                report['capture_batch_count'] = len(records)
                report['unread_batch_count'] = max(0, len(records) - report['pages'])
                report['last_batch_may_be_truncated'] = bool(report['pages'] and report['reason'] in ('target_reached', 'error', 'cancelled'))
                results.append(report)
                if on_result is not None:
                    on_result(report)
                if report.get('reason') in ('error', 'cancelled') or report.get('error') or report.get('export_error'):
                    break
        except InterruptedError:
            if stop is None or not stop.is_set():
                raise
            break
    logger.info('App 响应导入完成：%s 个对象', len(results))
    return results
