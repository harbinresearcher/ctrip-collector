"""Read App review gap recommendations for the Windows sight queue."""

from pathlib import Path

from coverage_audit import write_coverage_audit
from mobile_sight_queue import MAX_REVIEW_TARGET
from object_identity import evidence_objects, object_identity
from phone_queue import build_phone_queue
from workbench_app_jobs import sight_navigation_address


MAX_ITEMS = 20


def build_sight_gap_plan(output):
    folder = Path(output).resolve()
    items = []
    plan = {'items': items, 'skipped_count': 0, 'missing_address_count': 0}
    if not folder.is_dir():
        return plan

    audit = write_coverage_audit(folder, persist=False)
    phone = build_phone_queue(folder, audit)
    if phone['phone_database'] == 'unreadable':
        raise ValueError('无法读取 App 点评数据库，请确认数据目录和数据库状态。')
    no_review_entries = {
        row['object_key'] for row in audit['objects']
        if row.get('latest_phone_reason') == 'no_review_entry' and row.get('phone_no_review_entry') is True
    }
    plan['skipped_count'] = sum(row['kind'] == 'sight' for row in phone['skipped'])
    for row in phone['queue']:
        if row['kind'] != 'sight':
            continue
        ref = row['object_key']
        collected = row['phone_collected']
        remaining_capacity = MAX_REVIEW_TARGET - collected
        if ref in no_review_entries or remaining_capacity <= 0:
            plan['skipped_count'] += 1
            continue

        path = folder / f'{ref}-object.json'
        try:
            # The queue requires this exact snapshot. Avoid walking the whole directory for every row.
            _, name, _ = next(evidence_objects(path, 'object_snapshot', ref))
            sight_navigation_address(folder, object_identity(ref, name, path))
        except (OSError, ValueError, StopIteration):
            plan['missing_address_count'] += 1
            continue

        if len(items) < MAX_ITEMS:
            items.append({'object_ref': ref, 'name': name, 'target': row['target'],
                          'collected': collected, 'missing': row['missing'],
                          'suggested_add': min(row['limit_suggestion'], remaining_capacity)})
    return plan
