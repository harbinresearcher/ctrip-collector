"""手机采集对象身份：编号由落盘证据给出，缺证据就报错，不猜编号。"""
import csv
import io
import json
import logging
import os
import sqlite3
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, Mock, patch

import app
from object_identity import resolve_object_ref
from runner import collect

logger = logging.getLogger(__name__)
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
SIGHT = (FIXTURES / 'ctrip-comments2.xml').read_text('utf-8')
SIGHT_NAME = '长白山西景区'


def write_snapshot(folder, key, **fields):
    data = {'kind': key.split('-')[0], 'name': SIGHT_NAME}
    data.update(fields)
    path = Path(folder) / f'{key}-object.json'
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


def fake_adb(xml):
    # 只模拟一台停在评论页的手机：不连真机，不读旧 dump 文件。
    device = Mock()
    device.snapshot.return_value = xml
    factory = MagicMock()
    factory.return_value.__enter__.return_value = device
    return factory


def run_main(argv):
    with patch('sys.argv', ['app.py', *argv]), redirect_stdout(io.StringIO()) as out:
        app.main()
    return json.loads(out.getvalue())


class ObjectRefTests(unittest.TestCase):
    def test_full_evidence_supplies_object_id_and_source(self):
        # /* ===== 步骤1：证据齐全；数据源：对象快照 + 真实景点样本；操作：1) 命令行采集 2) 核对报告/库/CSV ===== */
        logger.info('开始证据身份采集测试')
        with TemporaryDirectory() as folder:
            write_snapshot(folder, 'sight-136039', resource_id=136039,
                           url='https://you.ctrip.com/sight/a1/136039.html', display_total=1099)
            with patch('app.Adb', fake_adb(SIGHT)):
                report = run_main(['--collect', '--object-ref', 'sight-136039', '--output', folder,
                                   '--limit', '5', '--max-swipes', '0'])
            # 1.1 编号来自证据，名称来自证据，报告能说明证据出处。
            self.assertEqual((report['kind'], report['object_id'], report['place']),
                             ('sight', '136039', SIGHT_NAME))
            self.assertEqual(report['identity_source'], 'evidence_manifest')
            self.assertEqual(report['identity_evidence'], 'sight-136039-object.json')
            self.assertFalse(report['identity_id_verified'])
            self.assertGreater(report['count'], 0)
            self.assertTrue(any('落盘证据' in item for item in report['limitations']))
            # 1.2 库和 CSV 都能区分身份来源，字段名保持原样。
            with closing(sqlite3.connect(Path(folder) / 'reviews.sqlite3')) as db:
                sources = {row[0] for row in db.execute('SELECT identity_source FROM reviews_scoped')}
                identity = db.execute('SELECT object_id FROM reviews_scoped').fetchone()[0]
            self.assertEqual((sources, identity), ({'evidence_manifest'}, '136039'))
            with open(report['csv'], encoding='utf-8-sig', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual({row['identity_source'] for row in rows}, {'evidence_manifest'})
        logger.info('证据身份采集测试完成')

    def test_evidence_without_positive_id_is_rejected_before_collecting(self):
        # /* ===== 步骤2：证据缺编号；数据源：缺字段/编号不一致的快照；操作：1) 解析报错 2) 不动设备 ===== */
        logger.info('开始证据缺编号测试')
        with TemporaryDirectory() as folder:
            write_snapshot(folder, 'sight-999', display_total=None)
            with self.assertRaises(ValueError) as caught:
                resolve_object_ref(folder, 'sight-999')
            self.assertIn('没有正整数对象编号', str(caught.exception))
            # 2.1 编号与引用不一致时也不能挑一个用。
            write_snapshot(folder, 'sight-998', resource_id=1000)
            with self.assertRaises(ValueError) as caught:
                resolve_object_ref(folder, 'sight-998')
            self.assertIn('不一致', str(caught.exception))
            # 2.2 完全没有该对象证据时同样报错，不猜编号。
            with self.assertRaises(ValueError) as caught:
                resolve_object_ref(folder, 'sight-136039')
            self.assertIn('不会猜测对象编号', str(caught.exception))
            # 2.3 走命令行解析失败发生在连设备之前，采集函数一次都不调用。
            factory = fake_adb(SIGHT)
            with patch('app.Adb', factory), patch('app.collect') as collect_mock, \
                    patch('sys.argv', ['app.py', '--collect', '--object-ref', 'sight-999',
                                       '--output', folder]), redirect_stdout(io.StringIO()):
                with self.assertRaises(ValueError):
                    app.main()
            factory.assert_not_called()
            collect_mock.assert_not_called()
        logger.info('证据缺编号测试完成')

    def test_object_ref_conflicts_with_manual_place_and_resource_id(self):
        # /* ===== 步骤3：身份来源互斥；数据源：命令行参数；操作：1) 同时给手填值 2) 退出码 2 ===== */
        logger.info('开始身份来源互斥测试')
        for extra in (['--place', SIGHT_NAME], ['--resource-id', '136039']):
            with self.subTest(extra=extra), redirect_stderr(io.StringIO()) as err, \
                    patch('sys.argv', ['app.py', '--collect', '--object-ref', 'sight-136039', *extra]):
                with self.assertRaises(SystemExit) as caught:
                    app.main()
            self.assertEqual(caught.exception.code, 2)
            self.assertIn('互斥', err.getvalue())
        # 3.1 没有 --collect 时也不能用 --object-ref 悄悄跑别的模式。
        with redirect_stderr(io.StringIO()) as err, patch('sys.argv', ['app.py', '--object-ref', 'sight-136039']):
            with self.assertRaises(SystemExit) as caught:
                app.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn('只能与 --collect 一起使用', err.getvalue())
        logger.info('身份来源互斥测试完成')

    def test_legacy_path_keeps_operator_declared_unverified(self):
        # /* ===== 步骤4：旧路径不变；数据源：真实酒店样本；操作：1) 手填编号仍为声明 2) 手填名称入口不新增字段 ===== */
        logger.info('开始旧路径身份测试')
        hotel = (FIXTURES / 'hotel-comments2.xml').read_text('utf-8')
        with TemporaryDirectory() as folder:
            adb = Mock()
            adb.snapshot.side_effect = [hotel, hotel, hotel, hotel, hotel, hotel]
            report = collect(adb, 'hotel', '测试酒店', folder, max_swipes=0, object_id='123')
            self.assertEqual(report['identity_source'], 'operator_declared_unverified')
            self.assertNotIn('identity_evidence', report)
            self.assertTrue(any('操作者填写' in item for item in report['limitations']))
            with closing(sqlite3.connect(Path(folder) / 'reviews.sqlite3')) as db:
                sources = {row[0] for row in db.execute('SELECT identity_source FROM reviews_scoped')}
            self.assertEqual(sources, {'operator_declared_unverified'})
        # 4.2 命令行旧入口（只给 --place）行为不变：仍走旧表，也不新增 identity_source。
        with TemporaryDirectory() as folder, patch('app.Adb', fake_adb(hotel)):
            legacy = run_main(['--collect', '--place', '测试酒店', '--output', folder, '--max-swipes', '0'])
        self.assertEqual(legacy['identity_verification'], 'operator_declared_unverified')
        self.assertNotIn('identity_source', legacy)
        self.assertNotIn('object_id', legacy)
        logger.info('旧路径身份测试完成')

    def test_directory_report_also_supplies_identity(self):
        # /* ===== 步骤5：无快照时回退目录报告；数据源：发现报告对象行；操作：1) 解析名称 2) 记证据文件 ===== */
        logger.info('开始目录报告身份测试')
        with TemporaryDirectory() as folder:
            report = {'kind': 'sight', 'listing_total': 3000, 'objects': [
                {'kind': 'sight', 'resource_id': 136039, 'name': SIGHT_NAME,
                 'url': 'https://you.ctrip.com/sight/a1/136039.html', 'comment_count': 1099}]}
            (Path(folder) / 'discovery-sight-fusong2479.json').write_text(
                json.dumps(report, ensure_ascii=False), encoding='utf-8')
            identity = resolve_object_ref(folder, 'sight-136039')
        self.assertEqual((identity['kind'], identity['object_id'], identity['name']),
                         ('sight', '136039', SIGHT_NAME))
        self.assertEqual(identity['identity_evidence'], 'discovery-sight-fusong2479.json')
        # 5.1 相对目录也要能解析：命令行常传相对 --output，证据文件须按普通文件读取。
        #     临时目录必须与当前工作目录同盘，否则 Windows 上 os.path.relpath 直接抛
        #     ValueError（path is on mount 'C:', start on mount 'D:'），CI 上就会红。
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            report['objects'] = [{'kind': 'sight', 'resource_id': 7, 'name': '相对路径景点',
                                  'url': 'https://you.ctrip.com/sight/a1/7.html', 'comment_count': 1}]
            (Path(folder) / 'discovery-sight-relative.json').write_text(json.dumps(report), encoding='utf-8')
            relative = os.path.relpath(folder)
            self.assertEqual(resolve_object_ref(relative, 'sight-7')['name'], '相对路径景点')
        # 5.2 手机旧报告只是手填编号的回声，不能反过来当身份证据。
        with TemporaryDirectory() as folder:
            (Path(folder) / 'report-20260101T000000-abcdef.json').write_text(json.dumps(
                {'kind': 'sight', 'place': SIGHT_NAME, 'object_id': '136039',
                 'identity_source': 'operator_declared_unverified'}), encoding='utf-8')
            with self.assertRaises(ValueError):
                resolve_object_ref(folder, 'sight-136039')
        logger.info('目录报告身份测试完成')


if __name__ == '__main__':
    unittest.main()