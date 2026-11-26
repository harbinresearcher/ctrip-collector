import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

from layout_fingerprint import (FAMILIES, attach_layout, layout_for, mobile_family,
                                page_structure_fingerprint, structure_fingerprint, web_family)
from network import collect_network
from runner import collect

FIXTURES = Path(__file__).parent / 'fixtures'
BASELINES = Path(__file__).parent / 'layout_baselines.json'


def parent_of(root, target):
    return next((node for node in root.iter('node') if any(child is target for child in node)), None)


class LayoutFingerprintTests(unittest.TestCase):
    def test_same_page_gives_same_fingerprint(self):
        # /* ===== 步骤1：指纹稳定；数据源：真实景点点评首屏；操作：1) 重复计算 2) 重排属性 ===== */
        xml = (FIXTURES / 'ctrip-comments.xml').read_text('utf-8')
        once, twice = structure_fingerprint(xml), structure_fingerprint(xml)
        self.assertEqual(once, twice)
        self.assertRegex(once, r'^[0-9a-f]{16}$')
        # 1.1 重新序列化只改属性顺序/缩进，不改变结构指纹。
        self.assertEqual(once, structure_fingerprint(ET.tostring(ET.fromstring(xml), encoding='unicode')))

    def test_text_and_bounds_edits_keep_fingerprint(self):
        # /* ===== 步骤2：只改正文；数据源：同一真实页面；操作：1) 改写全部 text 2) 改写 bounds ===== */
        xml = (FIXTURES / 'ctrip-comments.xml').read_text('utf-8')
        root = ET.fromstring(xml)
        for node in root.iter('node'):
            if node.get('text', '').strip():
                node.set('text', '改写的正文：' + node.get('text'))
            if node.get('bounds'):
                node.set('bounds', '[0,0][1,1]')
        self.assertEqual(structure_fingerprint(xml),
                         structure_fingerprint(ET.tostring(root, encoding='unicode')))

    def test_structure_edit_changes_fingerprint_and_flags_changed(self):
        # /* ===== 步骤3：改版提示；数据源：真机景点首屏；操作：1) 删/加节点 2) 改 class 3) 比对基线 ===== */
        xml = (FIXTURES / 'sight-review-live-yungang.xml').read_text('utf-8')
        baseline = FAMILIES['mobile_reviews_sight']['fingerprint']
        self.assertEqual(structure_fingerprint(xml), baseline)

        removed_root = ET.fromstring(xml)
        leaf = [node for node in removed_root.iter('node') if len(node) == 0][-1]
        parent_of(removed_root, leaf).remove(leaf)
        removed = structure_fingerprint(ET.tostring(removed_root, encoding='unicode'))
        self.assertNotEqual(removed, baseline)

        added_root = ET.fromstring(xml)
        added_root.append(ET.Element('node', {'class': 'android.widget.Toast', 'text': '新增节点'}))
        self.assertNotEqual(structure_fingerprint(ET.tostring(added_root, encoding='unicode')), baseline)

        class_root = ET.fromstring(xml)
        next(node for node in class_root.iter('node')
             if node.get('class') == 'android.widget.TextView').set('class', 'android.view.View')
        changed_class = structure_fingerprint(ET.tostring(class_root, encoding='unicode'))
        self.assertNotEqual(changed_class, baseline)

        # 3.1 结构变了：报告给出 changed 与 layout_changed，并写一条限制说明。
        report = {'limitations': []}
        self.assertTrue(attach_layout(report, 'mobile_reviews_sight', removed)['changed'])
        self.assertIs(report['layout_changed'], True)
        self.assertIn('结构指纹', report['limitations'][-1])
        # 3.2 结构没变：不写 layout_changed。
        report = {'limitations': []}
        self.assertFalse(attach_layout(report, 'mobile_reviews_sight', baseline)['changed'])
        self.assertNotIn('layout_changed', report)
        self.assertFalse(layout_for('mobile_reviews_sight', baseline)['changed'])

    def test_baselines_match_real_fixtures(self):
        # /* ===== 步骤4：基线有效；数据源：真实 fixture；操作：1) 重算 2) 对齐 JSON 与模块 ===== */
        baselines = json.loads(BASELINES.read_text('utf-8'))
        self.assertIn('mobile_reviews_sight', baselines)
        self.assertIn('web_reviews', baselines)
        self.assertEqual(set(baselines), set(FAMILIES))
        for family, meta in FAMILIES.items():
            path = FIXTURES / meta['fixture']
            if meta['kind'] == 'xml':
                actual = structure_fingerprint(path.read_text('utf-8'))
            else:
                actual = page_structure_fingerprint(json.loads(path.read_text('utf-8')))
            self.assertEqual(actual, baselines[family]['fingerprint'], f'{family} 的 fixture 基线过期')
            self.assertEqual(actual, meta['fingerprint'], f'{family} 的 FAMILIES 基线过期')
            self.assertEqual(baselines[family]['version'], meta['version'])

    def test_mobile_families_are_selected_by_kind(self):
        # /* ===== 步骤5：按类型分族；数据源：真机景点首屏；操作：1) 无基线类型不比对 2) 各类型族名互不共用 ===== */
        self.assertEqual(mobile_family('sight'), 'mobile_reviews_sight')
        self.assertEqual(mobile_family('hotel'), 'mobile_reviews_hotel')
        self.assertEqual(mobile_family('restaurant'), 'mobile_reviews_restaurant')
        self.assertEqual(mobile_family('shopping'), 'mobile_reviews_shopping')
        # 5.1 餐厅/购物没有真实基线：只记指纹，expected=None、changed=False，不能误报改版。
        for kind in ('restaurant', 'shopping'):
            layout = layout_for(mobile_family(kind), FAMILIES['mobile_reviews_sight']['fingerprint'])
            self.assertIsNone(layout['expected'])
            self.assertFalse(layout['changed'])
            report = {'limitations': []}
            self.assertFalse(attach_layout(report, mobile_family(kind), '0123456789abcdef')['changed'])
            self.assertNotIn('layout_changed', report)

    def test_sight_live_fixture_does_not_false_alarm(self):
        # /* ===== 步骤6：景点首屏不再假警报；数据源：真机云冈石窟首屏；操作：1) collect 2) 改一个 TextView 的 class ===== */
        xml = (FIXTURES / 'sight-review-live-yungang.xml').read_text('utf-8')
        with tempfile.TemporaryDirectory() as folder:
            adb = Mock()
            adb.snapshot.return_value = xml
            stop = Mock(is_set=lambda: False, wait=Mock(return_value=False))
            report = collect(adb, 'sight', '云冈石窟', folder, max_swipes=0, stop=stop, object_id='123')
            self.assertEqual(report['layout']['family'], 'mobile_reviews_sight')
            self.assertEqual(report['layout']['fingerprint'], FAMILIES['mobile_reviews_sight']['fingerprint'])
            self.assertEqual(report['layout']['expected'], FAMILIES['mobile_reviews_sight']['fingerprint'])
            self.assertFalse(report['layout']['changed'])
            self.assertNotIn('layout_changed', report)
            self.assertEqual(report['count'], 1)

        # 6.1 只改一个 TextView 的 class：结构指纹变化，报告提示改版，点评照常入库。
        root = ET.fromstring(xml)
        next(node for node in root.iter('node')
             if node.get('class') == 'android.widget.TextView').set('class', 'android.view.View')
        with tempfile.TemporaryDirectory() as folder:
            adb = Mock()
            adb.snapshot.return_value = ET.tostring(root, encoding='unicode')
            stop = Mock(is_set=lambda: False, wait=Mock(return_value=False))
            report = collect(adb, 'sight', '云冈石窟', folder, max_swipes=0, stop=stop, object_id='123')
            self.assertTrue(report['layout']['changed'])
            self.assertTrue(report['layout_changed'])
            self.assertTrue(any('结构指纹' in line for line in report['limitations']))
            self.assertEqual(report['count'], 1)

    def test_reports_carry_layout_without_breaking_collection(self):
        # /* ===== 步骤7：报告字段；数据源：真实酒店首屏与真实网页点评区；操作：1) 未改 2) 改一处结构 ===== */
        hotel = (FIXTURES / 'hotel-comments.xml').read_text('utf-8')
        with tempfile.TemporaryDirectory() as folder:
            adb = Mock()
            adb.snapshot.return_value = hotel
            stop = Mock(is_set=lambda: False, wait=Mock(return_value=False))
            report = collect(adb, 'hotel', '酒店', folder, max_swipes=0, stop=stop, object_id='123')
            self.assertEqual(report['layout']['family'], mobile_family('hotel'))
            self.assertEqual(report['layout']['fingerprint'], FAMILIES['mobile_reviews_hotel']['fingerprint'])
            self.assertFalse(report['layout']['changed'])
            self.assertNotIn('layout_changed', report)
            self.assertGreater(report['count'], 0)

        # 7.1 改一个节点的 class：报告标 layout_changed，但点评仍然入库。
        root = ET.fromstring(hotel)
        next(node for node in root.iter('node')
             if node.get('class') == 'android.widget.ImageView').set('class', 'android.view.View')
        with tempfile.TemporaryDirectory() as folder:
            adb = Mock()
            adb.snapshot.return_value = ET.tostring(root, encoding='unicode')
            stop = Mock(is_set=lambda: False, wait=Mock(return_value=False))
            report = collect(adb, 'hotel', '酒店', folder, max_swipes=0, stop=stop, object_id='123')
            self.assertTrue(report['layout_changed'])
            self.assertTrue(report['layout']['changed'])
            self.assertTrue(any('结构指纹' in line for line in report['limitations']))
            self.assertGreater(report['count'], 0)

        # 7.2 网页点评区的键结构变了：报告标 layout_changed，点评照常保存。
        page = json.loads((FIXTURES / 'web-sight-comment-info.json').read_text('utf-8'))
        target = {'kind': 'sight', 'url': 'https://you.ctrip.com/sight/tianjin154/112796.html',
                  'resource_id': 112796, 'resource_type': 66, 'poi_id': 10559411,
                  'name': '海河游船', 'display_total': 4888, 'details': {},
                  'comment_info': page, 'ai_summary': {'status': 'not_captured', 'content': None}}
        fetch = lambda _: {'code': 200, 'result': {'items': [{'commentId': '1'}], 'totalCount': 1}}
        with tempfile.TemporaryDirectory() as folder:
            report = collect_network(target, folder, limit=1, fetch=fetch, delay=0)
            self.assertEqual(report['layout']['family'], web_family('sight'))
            self.assertEqual(report['layout']['fingerprint'], FAMILIES['web_reviews']['fingerprint'])
            self.assertFalse(report['layout']['changed'])
            self.assertEqual(report['count'], 1)
        broken = dict(page)
        del broken['scores']
        with tempfile.TemporaryDirectory() as folder:
            report = collect_network(dict(target, comment_info=broken), folder, limit=1, fetch=fetch, delay=0)
            self.assertTrue(report['layout_changed'])
            self.assertTrue(report['layout']['changed'])
            self.assertEqual(report['count'], 1)


if __name__ == '__main__':
    unittest.main()