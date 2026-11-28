import logging
import copy
import csv
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch, Mock

from runner import Adb, collect
from core import Store, bounds, parse_reviews

logger = logging.getLogger(__name__)


class RunnerTests(unittest.TestCase):
    def test_review_pacing_skips_only_pre_swipe_wait_when_new_reviews_arrive(self):
        fixtures = Path(__file__).parent / 'fixtures'
        first = (fixtures / 'hotel-comments2.xml').read_text('utf-8')
        original = parse_reviews(first, 'hotel')[0]['content']
        tree = ET.fromstring(first)
        next(node for node in tree.iter('node') if node.get('text') == original).set('text', '新的测试点评正文')
        next_review = ET.tostring(tree, encoding='unicode')
        with tempfile.TemporaryDirectory() as folder:
            adb = Mock()
            adb.snapshot.side_effect = [first, first, next_review]
            stop = Mock(is_set=lambda: False, wait=Mock(return_value=False))
            report = collect(adb, 'hotel', '酒店', folder, max_swipes=2, stop=stop, object_id='123')
            self.assertEqual(report['new_count'], 2)
            self.assertEqual(adb.run.call_count, 2)
            self.assertEqual([entry.args[0] for entry in stop.wait.call_args_list], [1, 1.5, 1])

    def test_review_pacing_skips_swipe_if_cancelled_during_focus_check(self):
        first = (Path(__file__).parent / 'fixtures' / 'hotel-comments2.xml').read_text('utf-8')
        with tempfile.TemporaryDirectory() as folder:
            stop = threading.Event()
            adb = Mock()
            adb.snapshot.return_value = first

            def check_focus():
                if adb.ensure_foreground.call_count == 2:
                    stop.set()

            adb.ensure_foreground.side_effect = check_focus
            report = collect(adb, 'hotel', '酒店', folder, max_swipes=3, stop=stop, object_id='123')
            self.assertEqual(report['reason'], 'stopped_by_user')
            self.assertEqual(report['new_count'], 1)
            adb.run.assert_not_called()

    def test_sight_review_pacing_skips_retry_wait_only_on_verified_forward_motion(self):
        fixtures = Path(__file__).parent / 'fixtures'
        first = (fixtures / 'ctrip-comments2.xml').read_text('utf-8')
        next_review = (fixtures / 'ctrip-comments3.xml').read_text('utf-8')
        original = parse_reviews(first, 'sight')[0]

        def change_card(delta, *, clipped_date=False, duplicate=False, loading=False):
            root = ET.fromstring(first)
            parents = {child: node for node in root.iter() for child in node}
            date = next(node for node in root.iter('node') if node.get('text') == original['date'])
            card = parents[date]
            while not any(node.get('text') == original['content'] for node in card.iter('node')):
                card = parents[card]
            for node in card.iter('node'):
                left, top, right, bottom = bounds(node)
                node.set('bounds', f'[{left},{top + delta}][{right},{bottom + delta}]')
            if clipped_date:
                left, top, right, bottom = bounds(date)
                date.set('bounds', f'[{left},{top}][{right},{bottom - 8}]')
            if duplicate:
                parents[card].append(copy.deepcopy(card))
            if loading:
                ET.SubElement(root[0], 'node', {'package': 'ctrip.android.view', 'text': '正在加载'})
            return ET.tostring(root, encoding='unicode')

        # Preserve the measured 549px/2400px stride at this fixture's 3200px height.
        moving = change_card(-732)
        for label, frame, waits in (
            ('forward motion', moving, [1, 1]),
            ('identical frame', first, [1, 1.5, 1]),
            ('reverse motion', change_card(732), [1, 1.5, 1]),
            ('clipped date', change_card(-732, clipped_date=True), [1, 1.5, 1]),
            ('ambiguous match', change_card(-732, duplicate=True), [1, 1.5, 1]),
            ('loading', change_card(-732, loading=True), [1, 1.5, 1]),
            ('review controls changed', moving.replace('text="1095"', 'text="1096"'), [1, 1.5, 1]),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as folder:
                adb = Mock()
                adb.snapshot.side_effect = [first, frame, next_review]
                stop = Mock(is_set=lambda: False, wait=Mock(return_value=False))
                report = collect(adb, 'sight', '长白山西景区', folder, max_swipes=2, stop=stop, object_id='123')
                self.assertEqual(report['reason'], 'screen_budget_reached')
                self.assertEqual((report['screens'], report['new_count'], report['sample_csv_count']), (3, 3, 3))
                self.assertEqual(adb.run.call_count, 2)
                self.assertEqual([entry.args[0] for entry in stop.wait.call_args_list], waits)

    def test_rerun_keeps_counts_and_samples_consistent(self):
        # /* ===== 步骤2：端到端一致；数据源：真实酒店样本两屏；操作：1) 同一对象采两轮 2) 三方计数对齐 ===== */
        logger.info('开始采集计数一致检查')
        from contextlib import closing
        import sqlite3
        fixtures = Path(__file__).parent / 'fixtures'
        first = (fixtures / 'hotel-comments.xml').read_text('utf-8')
        second = (fixtures / 'hotel-comments2.xml').read_text('utf-8')

        def run(folder):
            adb = Mock()
            adb.snapshot.side_effect = [first, second, second, second, second, second]
            stop = Mock(is_set=lambda: False, wait=Mock(return_value=False))
            return collect(adb, 'hotel', '测试酒店', folder, max_swipes=1, stop=stop, object_id='123')

        def records(path):
            with open(path, encoding='utf-8-sig', newline='') as stream:
                return list(csv.DictReader(stream))

        with tempfile.TemporaryDirectory() as folder:
            one = run(folder)
            with closing(sqlite3.connect(Path(folder) / 'reviews.sqlite3')) as db:
                rows = db.execute('SELECT COUNT(*) FROM reviews_scoped').fetchone()[0]
                keys = {row[0] for row in db.execute('SELECT key FROM reviews_scoped')}
            sample, cumulative = records(one['sample_csv']), records(one['csv'])
            # 2.1 报告计数、数据库行、本任务样本、累计导出四者必须一致。
            self.assertEqual((one['count'], rows, len(sample), len(cumulative)), (2, 2, 2, 2))
            self.assertEqual({row['key'] for row in sample} | {row['key'] for row in cumulative}, keys)
            self.assertEqual(len({row['key'] for row in cumulative}), 2)
            # 2.2 同一对象再采一轮：没有新评论，但本次任务样本仍是这两条，累计不重复。
            two = run(folder)
            with closing(sqlite3.connect(Path(folder) / 'reviews.sqlite3')) as db:
                rows = db.execute('SELECT COUNT(*) FROM reviews_scoped').fetchone()[0]
                runs = db.execute('SELECT COUNT(*) FROM mobile_review_runs').fetchone()[0]
            self.assertEqual((two['new_count'], two['count'], rows, runs), (0, 2, 2, 2))
            self.assertEqual(len(records(two['sample_csv'])), 2)
            self.assertEqual(len(records(two['csv'])), 2)
        logger.info('采集计数一致检查完成')

    def test_foreign_window_cannot_supply_missing_page_label(self):
        # /* ===== 步骤1：核验页面标签来源；数据源：混合窗口；操作：1) 移除携程标签 2) 拒绝外部代替 ===== */
        logger.info('开始混合窗口标签检查')
        root = ET.fromstring((Path(__file__).parent / 'fixtures' / 'hotel-comments2.xml').read_text('utf-8'))
        # 1.1 携程缺少“全部”时，其他应用同名标签不能让页面通过检查。
        for node in root.iter('node'):
            if node.get('package') == 'ctrip.android.view' and node.get('text') == '全部':
                node.set('text', '其他')
        root.append(ET.Element('node', {'package':'other.app', 'text':'全部'}))
        xml = ET.tostring(root, encoding='unicode')
        with tempfile.TemporaryDirectory() as folder:
            adb = Adb('adb', 'test')
            with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', return_value=xml):
                report = collect(adb, 'hotel', '酒店', Path(folder), max_swipes=0, object_id='123')
            # 未到达点评页现在是 arrival_failed（未知布局可计数），不再混进普通 error。
            # 这一屏的酒店名只是包含目标名，判据给的是 name_partial_match。
            self.assertEqual(report['reason'], 'arrival_failed')
            self.assertEqual(report['arrival_reason'], 'name_partial_match')
            self.assertEqual(report['count'], 0)
        logger.info('混合窗口标签检查完成')

    def test_scoped_reviews_ignore_legacy_limit_and_isolate_same_name(self):
        # /* ===== 步骤1：核验评论作用域；数据源：旧库和同名手机页面；操作：1) 独立判断上限 2) 核对导出 ===== */
        logger.info('开始手机评论作用域测试')
        xml = (Path(__file__).parent / 'fixtures' / 'hotel-comments2.xml').read_text('utf-8')
        place = '同名酒店'
        with tempfile.TemporaryDirectory() as folder:
            # 1.1 旧库已达目标，同名新 ID 仍必须读取页面。
            legacy = Store(folder)
            legacy.save('hotel', place, parse_reviews(xml, 'hotel'), 'old.xml')
            legacy.close()
            reports = []
            for object_id in ('100', '200'):
                adb = Adb('adb', 'test')
                with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', return_value=xml) as snapshot:
                    report = collect(adb, 'hotel', place, Path(folder), limit=1, max_swipes=0, object_id=object_id)
                snapshot.assert_called_once()
                self.assertEqual((report['count'], report['new_count'], report['existing_count']), (1, 1, 0))
                self.assertEqual(report['object_id'], object_id)
                self.assertEqual(report['identity_source'], 'operator_declared_unverified')
                self.assertFalse(report['identity_id_verified'])
                with Path(report['csv']).open(encoding='utf-8-sig', newline='') as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['object_id'], object_id)
                reports.append(report)
            self.assertNotEqual(reports[0]['csv'], reports[1]['csv'])
            # 1.2 同 ID 重跑沿用新作用域，不读取页面或重复写入。
            adb = Adb('adb', 'test')
            with patch.object(adb, 'snapshot') as snapshot:
                report = collect(adb, 'hotel', place, Path(folder), limit=1, max_swipes=0, object_id='100')
            snapshot.assert_not_called()
            self.assertEqual((report['count'], report['new_count'], report['existing_count']), (1, 0, 1))
        logger.info('手机评论作用域测试完成')

    def test_scoped_sight_keeps_title_guard_and_declared_id_boundary(self):
        # /* ===== 步骤2：核对景点作用域；数据源：景点真实页面；操作：1) 校验名称 2) 保留 ID 未验证边界 ===== */
        logger.info('开始景点作用域身份测试')
        xml = (Path(__file__).parent / 'fixtures' / 'ctrip-comments2.xml').read_text('utf-8')
        with tempfile.TemporaryDirectory() as folder:
            # 2.1 标题一致只证明本屏名称，不能证明填写的 ID。
            adb = Adb('adb', 'test')
            with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', return_value=xml):
                report = collect(adb, 'sight', '长白山西景区', Path(folder), max_swipes=0, object_id='00123')
            self.assertEqual(report['count'], 1)
            self.assertEqual(report['identity_verification'], 'screen_title_name_only')
            self.assertEqual(report['object_id'], '123')
            self.assertEqual(report['identity_source'], 'operator_declared_unverified')
            self.assertFalse(report['identity_id_verified'])
            self.assertTrue(any('操作者填写' in item for item in report['limitations']))
            # 2.2 另一 ID 对应错误标题时不得入库。
            wrong = xml.replace('text="长白山西景区"', 'text="其他景区"', 1)
            with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', return_value=wrong):
                report = collect(adb, 'sight', '长白山西景区', Path(folder), max_swipes=0, object_id='456')
            self.assertEqual(report['reason'], 'identity_unverified')
            self.assertEqual(report['count'], 0)
        logger.info('景点作用域身份测试完成')

    def test_sight_title_checked_on_each_real_fixture(self):
        # /* ===== 步骤1：核验真实景点页面；数据源：三份手机 XML；操作：1) 识别标题 2) 核验名称并记录边界 ===== */
        logger.info('开始检查景点标题')
        for name in ('ctrip-comments.xml', 'ctrip-comments2.xml', 'ctrip-comments3.xml'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                # 1.1 三份页面均能匹配同一景点，并写入评论。
                xml = (Path(__file__).parent / 'fixtures' / name).read_text('utf-8')
                adb = Adb('adb', 'test')
                with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', return_value=xml):
                    report = collect(adb, 'sight', '长白山西景区', Path(folder), max_swipes=0)
                self.assertGreater(report['count'], 0)
                self.assertEqual(report['identity_verification'], 'screen_title_name_only')
                self.assertFalse(report['identity_id_verified'])
        logger.info('景点标题检查完成')

    def test_sight_title_missing_mismatch_and_ambiguous_are_rejected(self):
        # /* ===== 步骤2：阻止错误归属；数据源：真实 XML 的受控变体；操作：1) 改名或隐藏 2) 构造歧义 ===== */
        logger.info('开始检查景点身份异常')
        xml = (Path(__file__).parent / 'fixtures' / 'ctrip-comments2.xml').read_text('utf-8')
        for label, variant in (
            ('mismatch', xml.replace('text="长白山西景区"', 'text="其他景区"', 1)),
            ('missing', xml.replace('text="长白山西景区"', 'text=""', 1)),
            ('ambiguous', xml.replace('text="长白山西景区"', 'text="长白山西景区…"', 1)),
            ('not_reviews', xml.replace('用户点评', '其他页面')),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as folder:
                # 2.1 页面身份异常时不能保存评论，也不能滑动。
                adb = Adb('adb', 'test')
                with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', return_value=variant), patch.object(adb, 'run') as run:
                    report = collect(adb, 'sight', '长白山西景区', Path(folder), max_swipes=0)
                self.assertEqual(report['count'], 0)
                self.assertEqual(report['reason'], 'identity_unverified')
                run.assert_not_called()
        logger.info('景点身份异常检查完成')

    def test_sight_title_change_during_collection_stops_before_save(self):
        # /* ===== 步骤3：检查中途切换；数据源：两屏 XML；操作：1) 保存首屏 2) 拒绝另一景点 ===== */
        logger.info('开始检查中途切换')
        base = Path(__file__).parent / 'fixtures'
        first = (base / 'ctrip-comments2.xml').read_text('utf-8')
        second = (base / 'ctrip-comments3.xml').read_text('utf-8').replace('text="长白山西景区"', 'text="其他景区"', 1)
        with tempfile.TemporaryDirectory() as folder:
            # 3.1 第二屏有有效评论，仍不能写入原景点。
            adb = Adb('adb', 'test')
            stop = Mock()
            stop.is_set.return_value = False
            stop.wait.return_value = False
            with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', side_effect=[first, second]), patch.object(adb, 'run'):
                report = collect(adb, 'sight', '长白山西景区', Path(folder), max_swipes=2, stop=stop)
            self.assertEqual(report['count'], 1)
            self.assertEqual(report['screens'], 2)
            self.assertEqual(report['reason'], 'identity_unverified')
        logger.info('中途切换检查完成')

    def test_sight_ambiguous_navigation_is_rejected(self):
        # /* ===== 步骤4：拒绝重复标题；数据源：真实 XML 的双导航变体；操作：1) 复制导航 2) 禁止写入 ===== */
        logger.info('开始检查重复导航')
        xml = (Path(__file__).parent / 'fixtures' / 'ctrip-comments2.xml').read_text('utf-8')
        root = ET.fromstring(xml)
        # 4.1 两个符合结构的导航不能任选一个。
        parents = {child: parent for parent in root.iter() for child in parent}
        title = next(n for n in root.iter('node') if n.get('text') == '长白山西景区')
        nav = parents[title]
        parents[nav].append(copy.deepcopy(nav))
        with tempfile.TemporaryDirectory() as folder:
            adb = Adb('adb', 'test')
            with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', return_value=ET.tostring(root, encoding='unicode')):
                report = collect(adb, 'sight', '长白山西景区', Path(folder), max_swipes=0)
            self.assertEqual(report['count'], 0)
            self.assertEqual(report['reason'], 'identity_unverified')
        logger.info('重复导航检查完成')

    def test_sight_old_rows_are_retained_without_claiming_verified(self):
        # /* ===== 步骤5：保留旧数据；数据源：旧格式 SQLite；操作：1) 准备旧记录 2) 拒绝错误页面 ===== */
        logger.info('开始检查旧数据兼容')
        xml = (Path(__file__).parent / 'fixtures' / 'ctrip-comments2.xml').read_text('utf-8')
        with tempfile.TemporaryDirectory() as folder:
            # 5.1 旧数据继续可读，但不因本次身份检查而变成已核验。
            store = Store(Path(folder))
            store.save('sight', '长白山西景区', parse_reviews(xml, 'sight'), 'old.xml')
            store.close()
            adb = Adb('adb', 'test')
            wrong = xml.replace('text="长白山西景区"', 'text="其他景区"', 1)
            with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', return_value=wrong):
                report = collect(adb, 'sight', '长白山西景区', Path(folder), max_swipes=0)
            self.assertEqual((report['count'], report['new_count']), (1, 0))
            self.assertEqual(report['existing_count'], 1)
            self.assertEqual(report['identity_verification'], 'operator_declared_unverified')
            self.assertEqual(report['identity_verification_scope'], 'current_run_checked_screens_only')
            self.assertFalse(report['identity_id_verified'])
            # 5.2 旧记录已达到上限时不会读取页面，也不能宣称名称已核验。
            with patch.object(adb, 'snapshot') as snapshot:
                report = collect(adb, 'sight', '长白山西景区', Path(folder), limit=1, max_swipes=0)
            snapshot.assert_not_called()
            self.assertEqual(report['identity_checked_screens'], 0)
            self.assertEqual(report['identity_verification'], 'operator_declared_unverified')
            self.assertEqual((report['existing_count'], report['new_count']), (1, 0))
        logger.info('旧数据兼容检查完成')

    def test_fast_snapshot_excludes_other_windows_and_releases_service(self):
        # /* ===== 步骤0：验证快速页面读取；数据源：多窗口 XML；操作：1) 只保留携程窗口 2) 复用连接 3) 释放自动化服务 ===== */
        logger.info('开始快速读取检查')
        device = Mock()
        device.dump_hierarchy.return_value = '<hierarchy><node package="com.android.systemui" text="private" bounds="[0,0][100,10]"/><node package="ctrip.android.view" text="全部" bounds="[0,0][100,200]"/></hierarchy>'
        adb = Adb('adb', 'test', ui_backend='uiautomator2')
        with patch('uiautomator2.connect', return_value=device) as connect:
            first = adb.snapshot()
            adb.snapshot()
            self.assertNotIn('private', first)
            self.assertIn('[0,0][100,200]', first)
            connect.assert_called_once_with('test')
            adb.close()
            device.stop_uiautomator.assert_called_once()
        logger.info('快速读取检查完成')

    def test_export_lock_does_not_discard_report(self):
        logger.info('开始检查 CSV 文件占用')
        # 1.0 导出失败仍记录停止原因，已保存数据库不能被当成丢失。
        stop = threading.Event()
        stop.set()
        with tempfile.TemporaryDirectory() as folder:
            with patch('runner.Store.export', side_effect=PermissionError('CSV is open in Excel')):
                report = collect(Adb('adb', 'test'), 'hotel', 'test', Path(folder), stop=stop)
            self.assertEqual(report['reason'], 'stopped_by_user')
            self.assertEqual(report['identity_verification'], 'operator_declared_unverified')
            self.assertIn('Excel', report['export_error'])
            self.assertIsNone(report['csv'])
            self.assertEqual(len(list(Path(folder).glob('report-*.json'))), 1)
        logger.info('CSV 文件占用检查完成')

    def test_report_write_failure_keeps_reason_and_saved_rows(self):
        logger.info('开始检查报告写入失败')
        # 1.0 报告写不进去时，停止原因与已保存条数仍要返回给调用方。
        stop = threading.Event()
        stop.set()
        original = Path.replace
        def replace(path, destination):
            if str(destination).endswith('.json') and 'report-' in destination.name:
                raise OSError('模拟报告写入失败')
            return original(path, destination)
        with tempfile.TemporaryDirectory() as folder, patch.object(Path, 'replace', replace):
            report = collect(Adb('adb', 'test'), 'hotel', 'test', Path(folder), stop=stop)
            self.assertEqual(report['reason'], 'stopped_by_user')
            self.assertIn('模拟报告写入失败', report['report_error'])
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])
        logger.info('报告写入失败检查完成')

    def test_hotel_detail_rejected_before_swiping(self):
        logger.info('开始检查详情页误识别')
        # 1.0 酒店详情页也有“点评”，但不是评论列表。
        xml = (Path(__file__).parent / 'fixtures' / 'hotel-detail.xml').read_text('utf-8')
        with tempfile.TemporaryDirectory() as folder:
            adb = Adb('adb', 'test')
            with patch.object(adb, 'ensure_foreground'), patch.object(adb, 'snapshot', return_value=xml), patch.object(adb, 'run') as run:
                report = collect(adb, 'hotel', 'test', Path(folder), max_swipes=1)
                run.assert_not_called()
                self.assertEqual(report['reason'], 'arrival_failed')
        logger.info('详情页误识别检查完成')

    def test_android16_focus_uses_full_window_dump(self):
        logger.info('开始检查 Android 16 前台检测')
        # 1.0 Android 16 的 windows 子命令可能不含焦点信息。
        adb = Adb('adb', 'test')
        def response(*args):
            return 'mCurrentFocus=Window{123 u0 ctrip.android.view/Test}' if args == ('shell', 'dumpsys', 'window') else ''
        with patch.object(adb, 'run', side_effect=response):
            adb.ensure_foreground()
        logger.info('Android 16 前台检测完成')

    # /* ================================================================
    # 步骤1：检查设备异常与停止
    # 数据源：受控设备响应；操作：1) 注入 dump 失败 2) 验证不读旧文件
    # ================================================================ */
    def test_dump_failure_never_reads_old_file(self):
        logger.info('开始检查旧文件隔离')
        # 1.1 Android dump 可能返回零退出码但文字报错。
        adb = Adb('adb', 'test')
        with patch.object(adb, 'run', return_value='ERROR: could not get idle state.') as run:
            with self.assertRaises(RuntimeError):
                adb.snapshot()
            self.assertFalse(any('cat' in call.args for call in run.call_args_list))
        logger.info('旧文件隔离检查完成')

    def test_cancel_before_start(self):
        logger.info('开始检查停止状态')
        # 1.2 预先停止时不得向设备发命令，仍写出停止报告。
        stop = threading.Event()
        stop.set()
        with tempfile.TemporaryDirectory() as folder:
            adb = Adb('adb', 'test')
            with patch.object(adb, 'run') as run:
                result = collect(adb, 'hotel', 'test', Path(folder), 500, 10, stop)
                run.assert_not_called()
            self.assertEqual(result['reason'], 'stopped_by_user')
            self.assertEqual(result['count'], 0)
        logger.info('停止状态检查完成')


if __name__ == '__main__':
    unittest.main()
