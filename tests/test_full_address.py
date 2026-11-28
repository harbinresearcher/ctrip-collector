"""全地址优先：对象快照的 details.address 比目录区县更能缩小搜索结果。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path

from object_identity import resolve_object_ref

logger = logging.getLogger(__name__)


def snapshot(folder, identity, name, address):
    (Path(folder) / f'sight-{identity}-object.json').write_text(json.dumps(
        {'kind': 'sight', 'resource_id': identity, 'name': name, 'display_total': 357,
         'details': {'address': address}}, ensure_ascii=False), encoding='utf-8')


def discovery(folder, identity, name, address):
    (Path(folder) / 'discovery-sight-2479-20261128T120000000000Z.json').write_text(json.dumps(
        {'kind': 'sight', 'district_id': 2479,
         'objects': [{'kind': 'sight', 'resource_id': identity, 'name': name, 'address': address}]},
        ensure_ascii=False), encoding='utf-8')


class FullAddressTests(unittest.TestCase):
    def test_snapshot_full_address_beats_directory_zone(self):
        # /* ===== 步骤1：两份证据都有地址；数据源：快照全地址 + 目录区县；操作：1) 取更长的 ===== */
        logger.info('开始全地址优先测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            discovery(root, 1407863, '万达长白山国际滑雪场', '长白山国际度假区')
            snapshot(root, 1407863, '万达长白山国际滑雪场', '吉林省白山市抚松县长白山国际旅游度假区')
            identity = resolve_object_ref(root, 'sight-1407863')
            # 1.1 搜索结果那行是「吉林-抚松-吉林省…度假区」，只有全地址能对上尾部。
            self.assertEqual(identity['address'], '吉林省白山市抚松县长白山国际旅游度假区')
            self.assertEqual(identity['identity_evidence'], 'sight-1407863-object.json')
        logger.info('全地址优先测试完成')

    def test_directory_address_still_used_without_snapshot(self):
        # /* ===== 步骤2：只有目录；数据源：目录区县；操作：1) 仍返回可用地址 ===== */
        logger.info('开始只有目录地址测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            discovery(root, 42, '某景点', '抚松')
            self.assertEqual(resolve_object_ref(root, 'sight-42')['address'], '抚松')
        logger.info('只有目录地址测试完成')


if __name__ == '__main__':
    unittest.main()