import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import object_identity
from object_identity import resolve_object_ref


def write_evidence(folder, filename, data):
    (Path(folder) / filename).write_text(json.dumps(data, ensure_ascii=False), 'utf-8')


class ObjectPickerEvidenceTests(unittest.TestCase):
    def test_directory_type_constrains_an_untyped_row_to_one_business_kind(self):
        with TemporaryDirectory() as folder:
            write_evidence(folder, 'discovery-sight-32.json', {'kind': 'sight', 'objects': [
                {'resource_id': 42, 'name': '景点样本'},
            ]})
            with self.assertRaises(ValueError):
                resolve_object_ref(folder, 'hotel-42')
            self.assertEqual(resolve_object_ref(folder, 'sight-42')['name'], '景点样本')
            self.assertEqual(object_identity.list_object_refs(folder, kind='hotel')['objects'], [])

    def test_report_prefix_can_supply_kind_and_conflicting_rows_are_skipped(self):
        with TemporaryDirectory() as folder:
            write_evidence(folder, 'discovery-sight-32.json', {'objects': [
                {'resource_id': 42, 'name': '景点样本'},
                {'kind': 'shopping', 'resource_id': 48, 'name': '业务类型冲突'},
            ]})
            write_evidence(folder, 'discovery-unknown.json', {'objects': [
                {'resource_id': 47, 'name': '缺少类型'},
            ]})
            write_evidence(folder, 'discovery-shopping-8.json', {'kind': 'sight', 'objects': [
                {'resource_id': 49, 'name': '报告类型冲突'},
            ]})
            result = object_identity.list_object_refs(folder)
            self.assertEqual(result['objects'], [resolve_object_ref(folder, 'sight-42')])
            for ref in ('sight-47', 'shopping-48', 'sight-49', 'shopping-49'):
                with self.subTest(ref=ref), self.assertRaises(ValueError):
                    resolve_object_ref(folder, ref)

    def test_listing_uses_same_priority_and_reads_each_artifact_once(self):
        with TemporaryDirectory() as folder:
            files = {
                'hotel-7-object.json': {'kind': 'hotel', 'resource_id': 7, 'name': 'Original Hotel'},
                'hotel-7-public-object.json': {'kind': 'hotel', 'resource_id': 7, 'name': 'Public Hotel'},
                'sight-8-report-20261126.json': {'object_key': 'sight-8', 'name': '山顶公园'},
                'hotel-7-report-20261126.json': {'object_key': 'hotel-7', 'name': 'Report Hotel'},
                'discovery-regions-a.json': {'objects': [
                    {'kind': 'hotel', 'resource_id': 7, 'name': 'Directory Hotel'},
                    {'kind': 'hotel', 'resource_id': 9, 'name': 'Another Hotel'},
                    {'kind': 'restaurant', 'poi_id': 17, 'name': '餐厅仅有 POI 编号'},
                ]},
                'restaurant-10-object.json': {'kind': 'restaurant', 'resource_id': 10,
                                              'poi_id': 17, 'name': '餐厅有点评编号'},
                'report-20261126-aaa.json': {'kind': 'shopping', 'object_id': '11', 'place': '手填商铺',
                                             'identity_source': 'operator_declared_unverified'},
                'report-20261126-bbb.json': {'kind': 'shopping', 'object_id': '12', 'place': '商场',
                                             'identity_source': 'screen_verified_id'},
                'hotel-20-object.json': {'kind': 'hotel', 'resource_id': 21, 'name': '编号不匹配'},
            }
            for filename, data in files.items():
                write_evidence(folder, filename, data)
            broken = Path(folder) / 'hotel-20-public-object.json'
            broken.write_text('{"incomplete":', 'utf-8')
            with patch('object_identity.read_evidence', wraps=object_identity.read_evidence) as read:
                result = object_identity.list_object_refs(folder)
            inspected = [call.args[0].name for call in read.call_args_list]
            self.assertCountEqual(inspected, [*files, broken.name])
            refs = {row['object_ref']: row for row in result['objects']}
            self.assertEqual(set(refs), {'hotel-7', 'hotel-9', 'sight-8', 'restaurant-10', 'shopping-12'})
            for ref, row in refs.items():
                self.assertEqual(row, resolve_object_ref(folder, ref))
            self.assertEqual(refs['hotel-7']['name'], 'Original Hotel')
            self.assertEqual(refs['hotel-7']['identity_evidence'], 'hotel-7-object.json')
            self.assertFalse(result['truncated'])
            filtered = object_identity.list_object_refs(folder, kind='hotel', query=' HOTEL ', limit=1)
            self.assertTrue(filtered['truncated'])
            self.assertEqual([row['object_ref'] for row in filtered['objects']], ['hotel-9'])
            by_id = object_identity.list_object_refs(folder, kind='restaurant', query='10')
            self.assertEqual([row['object_ref'] for row in by_id['objects']], ['restaurant-10'])

    def test_symbolic_link_is_not_enumerated(self):
        with TemporaryDirectory() as folder:
            external = Path(folder) / 'elsewhere'
            external.mkdir()
            write_evidence(external, 'hotel-7-object.json', {'kind': 'hotel', 'resource_id': 7, 'name': '外部'})
            try:
                (Path(folder) / 'hotel-7-object.json').symlink_to(external / 'hotel-7-object.json')
            except OSError:
                self.skipTest('当前账户无法创建符号链接')
            self.assertEqual(object_identity.list_object_refs(folder), {'objects': [], 'truncated': False})
            with self.assertRaises(ValueError):
                resolve_object_ref(folder, 'hotel-7')

    def test_empty_folder_and_invalid_filters(self):
        with TemporaryDirectory() as folder:
            result = object_identity.list_object_refs(Path(folder) / 'missing')
            self.assertEqual(result, {'objects': [], 'truncated': False})
            for kwargs in ({'kind': 'flight'}, {'query': None}, {'limit': True}, {'limit': 0}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    object_identity.list_object_refs(folder, **kwargs)

    def test_malformed_catalog_does_not_hide_later_valid_evidence(self):
        with TemporaryDirectory() as folder:
            deep = Path(folder) / 'discovery-a.json'
            deep.write_text('[' * 3000 + ']' * 3000, 'utf-8')
            write_evidence(folder, 'discovery-sight-1.json', {'objects': [
                {'resource_id': 10**20, 'name': '超长编号'},
                {'resource_id': 1, 'name': '普通景点'},
            ]})
            valid = resolve_object_ref(folder, 'sight-1')
            self.assertEqual(object_identity.list_object_refs(folder), {'objects': [valid], 'truncated': False})
