"""Reparse the saved evidence paired with historical Android AI summaries."""
import hashlib
import json
import logging
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from ai_capture import parse_ai, save_ai_summary
from android_coverage import AI_FILE, local_file

logger = logging.getLogger(__name__)
MAX_BYTES = 8 * 1024 * 1024
MAX_SUMMARIES = 200


def read_local(path):
    if not local_file(path):
        raise OSError('Evidence is absent or is not a regular local file')
    with path.open('rb') as stream:
        payload = stream.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError('Evidence exceeds the size limit')
    return payload


def reparse_summary(path, identity):
    record = json.loads(read_local(path))
    if (not isinstance(record, dict) or record.get('source') != 'ctrip_android_ui' or record.get('kind') != 'hotel'
            or type(record.get('resource_id')) is not int or record['resource_id'] != identity
            or not identity or not isinstance(record.get('name'), str) or not 1 <= len(record['name'].strip()) <= 100):
        return None, 'invalid_saved_summary'
    stamp = datetime.fromisoformat(record['captured_at'])
    if stamp.utcoffset() is None:
        return None, 'invalid_saved_summary'
    # Ignore absolute paths stored in old reports. Only the companion in this folder is eligible.
    raw = read_local(path.with_suffix('.xml'))
    digest = hashlib.sha256(raw).hexdigest()
    if record.get('evidence_sha256') is not None and record['evidence_sha256'] != digest:
        return None, 'evidence_digest_mismatch'
    parsed = parse_ai(raw.decode('utf-8'))
    previous = record.get('sections')
    if (not isinstance(previous, list) or not previous
            or [part.get('text') if isinstance(part, dict) else None for part in previous]
            != [part['text'] for part in parsed['sections']]):
        return None, 'summary_text_mismatch'
    parsed.pop('header_texts', None)
    saved_photos = record.get('ai_selected_images')
    if isinstance(saved_photos, dict) and saved_photos.get('metadata_status') == 'captured':
        count = saved_photos.get('selected_count')
        if type(count) is int and count >= 0:
            parsed['ai_selected_images'].update(selected_count=count, metadata_status='captured',
                                                selected_count_source='prior_gallery_record')
    return {**parsed, 'kind': 'hotel', 'resource_id': identity, 'name': record['name'].strip(),
            'captured_at': record['captured_at'], 'identity_source': 'operator_declared_current_phone_object',
            'source_file': path.name, 'evidence_sha256': digest,
            'evidence_validation': 'sha256_and_matching_text' if record.get('evidence_sha256')
            else 'matching_text_no_original_digest'}, None


def replay_ai_summaries(output, stop, emit):
    logger.info('开始重读本地 AI 总结证据')
    folder = Path(output).resolve()
    directory = folder / 'ai'
    if not directory.is_dir() or directory.is_symlink() or directory.is_junction():
        raise ValueError('本目录没有可重读的 AI 总结文件夹')
    candidates = sorted((entry for entry in directory.iterdir() if AI_FILE.fullmatch(entry.name)),
                        key=lambda entry: (entry.stem.rsplit('-', 1)[-1], entry.name), reverse=True)
    if not candidates:
        raise ValueError('AI 文件夹中没有旧总结报告')
    summaries, skipped = [], []
    for path in candidates[:MAX_SUMMARIES]:
        if stop.is_set():
            break
        identity = int(AI_FILE.fullmatch(path.name)[1])
        try:
            summary, reason = reparse_summary(path, identity)
        except OSError:
            summary, reason = None, 'unreadable_local_evidence'
        except (KeyError, ValueError, TypeError, ET.ParseError):
            summary, reason = None, 'invalid_saved_summary_or_xml'
        if summary is None:
            skipped.append({'source_file': path.name, 'reason': reason})
        else:
            summaries.append(summary)
    report_path = folder / f'ai-replay-{uuid.uuid4().hex}.json'
    report = {'source_method': 'android_ai_evidence_replay', 'complete': False,
              'reason': 'cancelled' if stop.is_set() else 'replayed', 'summaries': summaries, 'skipped': skipped,
              'remaining': len(candidates) - len(summaries) - len(skipped), 'report_file': str(report_path)}
    save_ai_summary(report_path, report)
    emit(f'重新读取 {len(summaries)} 份总结，跳过 {len(skipped)} 份')
    logger.info('本地 AI 总结证据重读完成')
    return report
