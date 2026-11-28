"""--auto-open 用目录地址做导航，身份解析要带出地址。"""
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from object_identity import list_object_refs, resolve_object_ref

logger = logging.getLogger(__name__)


def discovery_report(folder, rows):
    (Path(folder) / 'discovery-sight-2479-20261128T120000000000Z.json').write_text(json.dumps(
        {'kind': 'sight', 'district_id': 2479, 'objects': rows}, ensure_ascii=False), encoding='utf-8')


class AddressEvidenceTests(unittest.TestCase):
    def test_identity_carries_address_for_navigation(self):
        # /* ===== 步骤1：目录带区县；数据源：目录报告；操作：1) 身份里带 address ===== */
        logger.info('开始地址证据测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            discovery_report(root, [{'kind': 'sight', 'resource_id': 136039, 'name': '长白山西景区',
                                     'district_name': '抚松', 'zone_name': '松江河镇'}])
            identity = resolve_object_ref(root, 'sight-136039')
            # 1.1 导航要靠这个关键词把同名景点缩到一个。
            self.assertEqual(identity['address'], '松江河镇')
            self.assertEqual(identity['name'], '长白山西景区')
        logger.info('地址证据测试完成')

    def test_object_without_address_still_resolves(self):
        # /* ===== 步骤2：目录没有地址；数据源：旧报告；操作：1) 仍能解析 2) 不带 address ===== */
        logger.info('开始无地址解析测试')
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            discovery_report(root, [{'kind': 'sight', 'resource_id': 42, 'name': '某景点'}])
            identity = resolve_object_ref(root, 'sight-42')
            self.assertNotIn('address', identity)
            self.assertEqual(list_object_refs(root)['objects'][0]['object_ref'], 'sight-42')
        logger.info('无地址解析测试完成')


if __name__ == '__main__':
    unittest.main()