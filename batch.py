"""持久批量任务；达到条数目标与全量完成分开记录。"""
from contextlib import closing
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from network import canonical_url

logger = logging.getLogger(__name__)


def export_hashes(result, output):
    # /* ===== 步骤1：核验导出；数据源：采集结果；操作：1) 限定输出目录 2) 计算文件摘要 ===== */
    logger.info('开始核验批任务导出')
    hashes = {}
    for kind in ('csv', 'jsonl'):
        path = Path(result[kind]).resolve()
        if path.parent != output:
            raise ValueError('导出文件不在任务输出目录')
        with path.open('rb') as stream:
            hashes[path.name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    logger.info('批任务导出核验完成')
    return hashes


def verified_target(result, hashes, output, limit):
    # /* ===== 步骤2：核对重启证据；数据源：数据库和文件；操作：1) 核对条数 2) 核对导出摘要 ===== */
    logger.info('开始核对批任务完成证据')
    valid = False
    try:
        if result.get('reason') == 'target_reached' and not result.get('error') and not result.get('export_error'):
            database = output / 'network.sqlite3'
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
                count = db.execute('SELECT COUNT(*) FROM network_reviews WHERE object_key=?',
                                   (result['object_key'],)).fetchone()[0]
            valid = count >= limit and bool(hashes) and export_hashes(result, output) == hashes
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        valid = False
    logger.info('批任务完成证据核对结束：%s', valid)
    return valid


def collect_batch(urls, output, limit, collector, stop=None, emit=None):
    # /* ===== 步骤3：建立持久任务；数据源：用户清单；操作：1) 去重 2) 按目标条数隔离状态 ===== */
    logger.info('开始批量任务')
    if limit < 1:
        raise ValueError('目标条数必须大于零')
    urls = list(dict.fromkeys(canonical_url(url) for url in urls))
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    stop, emit = stop or Event(), emit or logger.info
    report = {'total': len(urls), 'processed': 0, 'skipped': 0, 'reason': 'queue_exhausted',
              'complete': False, 'results': [], 'error': '', 'task_database': str(output / 'batch.sqlite3')}
    with closing(sqlite3.connect(output / 'batch.sqlite3', timeout=30)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS tasks (url TEXT, target INTEGER, status TEXT, result TEXT, hashes TEXT, updated_at TEXT, PRIMARY KEY(url,target))')
        db.commit()
        # 3.1 逐对象提交；崩溃留下的running任务下次重试，复用评论库去重。
        for url in urls:
            if stop.is_set():
                report['reason'] = 'cancelled'
                break
            row = db.execute('SELECT result,hashes FROM tasks WHERE url=? AND target=? AND status=?',
                             (url, limit, 'target_reached')).fetchone()
            if row and verified_target(json.loads(row[0]), json.loads(row[1]), output, limit):
                report['skipped'] += 1
                emit('已核验已有数据，跳过：' + url)
                continue
            now = datetime.now(timezone.utc).isoformat()
            with db:
                db.execute('INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?)', (url, limit, 'running', '{}', '{}', now))
            try:
                result = collector(url, output, limit, stop, emit)
            except Exception as exc:
                result = {'reason': 'error', 'error': str(exc), 'export_error': '', 'complete': False}
            hashes = {}
            # 3.2 仅在成功导出后允许下次跳过；摘要失败保留错误并停止后续请求。
            if result.get('reason') == 'target_reached' and not result.get('error') and not result.get('export_error'):
                try:
                    hashes = export_hashes(result, output)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    result['export_error'] = str(exc)
            with db:
                db.execute('UPDATE tasks SET status=?,result=?,hashes=?,updated_at=? WHERE url=? AND target=?',
                           (result['reason'], json.dumps(result, ensure_ascii=False), json.dumps(hashes),
                            datetime.now(timezone.utc).isoformat(), url, limit))
            report['processed'] += 1
            report['results'].append(result)
            if result.get('reason') == 'error' or result.get('error') or result.get('export_error'):
                report.update(reason='error', error=result.get('error') or result.get('export_error') or '采集失败')
                break
            if result.get('reason') == 'cancelled':
                report['reason'] = 'cancelled'
                break
    # 3.3 本次清单执行结束不证明平台数据完整。
    (output / 'batch-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info('批量任务结束：%s', report['reason'])
    return report

