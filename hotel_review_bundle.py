"""Export one hotel review run alongside a matching saved AI summary."""
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from tempfile import SpooledTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

from android_coverage import AI_FILE
from workbench_result_files import open_output_file, open_task_csv


MAX_SUMMARY_BYTES = 8 * 1024 * 1024


def latest_matching_summary(output, hotel_id, name, *, selected_file=None, run_id=None):
    folder = output / 'ai'
    if not folder.is_dir() or folder.is_symlink() or folder.is_junction():
        raise LookupError('没有找到同名且同编号的 AI 总结，请先采集该酒店的 AI 总结。')
    candidates = []
    for path in folder.iterdir():
        match = AI_FILE.fullmatch(path.name)
        if match and int(match[1]) == hotel_id and (selected_file is None or path.name == selected_file):
            candidates.append(path)
    candidates.sort(key=lambda path: (path.stem.rsplit('-', 1)[-1], path.name), reverse=True)
    for path in candidates:
        try:
            with open_output_file(folder, path) as file:
                raw = file.read(MAX_SUMMARY_BYTES + 1)
            if len(raw) > MAX_SUMMARY_BYTES:
                continue
            record = json.loads(raw)
            if (not isinstance(record, dict) or record.get('source') != 'ctrip_android_ui'
                    or record.get('generated_by_ai') is not True or record.get('kind') != 'hotel'
                    or type(record.get('resource_id')) is not int or record['resource_id'] != hotel_id
                    or record.get('name') != name or (run_id is not None and record.get('review_run_id') != run_id)):
                continue
            stamp = datetime.fromisoformat(record['captured_at'])
            if stamp.utcoffset() is None:
                continue
        except (OSError, KeyError, ValueError, TypeError):
            continue
        sections = record.get('sections')
        lead = record.get('lead')
        if not isinstance(sections, list) or not isinstance(lead, list):
            continue
        lead = [line for line in lead if isinstance(line, str) and line]
        sections = [{key: value for key in ('topic', 'body', 'text', 'linked_count')
                     if isinstance((value := row.get(key)), str)}
                    for row in sections if isinstance(row, dict) and isinstance(row.get('body'), str) and row['body']]
        if not lead and not sections:
            continue
        photos = record.get('ai_selected_images')
        counts = {key: photos[key] for key in ('display_count', 'selected_count')
                  if isinstance(photos, dict) and type(photos.get(key)) is int and photos[key] >= 0}
        dimensions = record.get('rating_dimensions')
        return {'kind': 'hotel', 'resource_id': hotel_id, 'name': name, 'source': 'ctrip_android_ui',
                'generated_by_ai': True, 'captured_at': record['captured_at'],
                'source_file': path.name, 'complete': False, 'lead': lead, 'sections': sections,
                'expanded_boundary_visible': record.get('expanded_boundary_visible') is True,
                'rating_dimensions': [
                    {'label': row['label'], 'score_text': row['score_text']}
                    for row in dimensions if isinstance(row, dict)
                    and isinstance(row.get('label'), str) and isinstance(row.get('score_text'), str)]
                if isinstance(dimensions, list) else [],
                'linked_review_scope': 'display_counts_only', 'image_count_labels': counts}
    raise LookupError('没有找到同名且同编号的 AI 总结，请先采集该酒店的 AI 总结。')


def open_hotel_review_bundle(output, snapshot):
    result = snapshot.get('result')
    if not isinstance(result, dict) or result.get('kind') != 'hotel':
        raise ValueError('当前没有可导出的酒店点评任务')
    name, raw_id, count = (result.get(key) for key in ('place', 'object_id', 'sample_csv_count'))
    if (not isinstance(name, str) or not name.strip() or not isinstance(raw_id, str)
            or not re.fullmatch(r'[1-9][0-9]{0,19}', raw_id) or type(count) is not int or count < 1):
        raise ValueError('当前没有已保存的酒店点评样本')
    output = Path(output).resolve()
    with open_task_csv(output, snapshot, sample=True) as sample:
        first_screen = result.get('first_screen_ai')
        if first_screen is not None:
            if (first_screen.get('status') != 'saved'
                    or not isinstance(first_screen.get('source_file'), str) or not first_screen['source_file']):
                raise LookupError('这次任务没有保存首屏 AI 总结，不会用旧总结代替。')
            summary = latest_matching_summary(output, int(raw_id), name, selected_file=first_screen['source_file'],
                                              run_id=result['run_id'])
        else:
            summary = latest_matching_summary(output, int(raw_id), name)
        timing = ('总结来自本次点评任务的首屏。' if first_screen is not None
                  else 'AI 总结可能来自另一次采集。')
        manifest = {'name': name, 'resource_id': int(raw_id), 'complete': False,
                    'identity_verification': 'operator_declared_id_and_name_only',
                    'review_sample': {'run_id': result['run_id'], 'file': 'review-sample.csv',
                                      'row_count': count, 'stop_reason': result.get('reason'),
                                      'count_check': result.get('review_count_check')},
                    'ai_summary': {'file': 'ai-summary.json', 'captured_at': summary['captured_at'],
                                   'expanded_boundary_visible': summary['expanded_boundary_visible'],
                                   'same_review_run': first_screen is not None},
                    'limitations': ['按填写的编号与名称配对，未验证平台对象身份。',
                                    timing + '不能对应到样本中的具体点评。',
                                    '仅包含本次点评样本和已保存的 AI 总结；不含原始界面或图集文件。']}
        archive = SpooledTemporaryFile(max_size=4 * 1024 * 1024)
        try:
            with ZipFile(archive, 'w', ZIP_DEFLATED, compresslevel=3) as bundled:
                bundled.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
                bundled.writestr('ai-summary.json', json.dumps(summary, ensure_ascii=False, indent=2))
                with bundled.open('review-sample.csv', 'w', force_zip64=os.fstat(sample.fileno()).st_size >= 2**30) as out:
                    shutil.copyfileobj(sample, out, length=64 * 1024)
            archive.seek(0)
        except BaseException:
            archive.close()
            raise
    return archive
