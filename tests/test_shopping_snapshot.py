"""购物详情页的评分维度与公开点评样本。"""
import logging
import unittest
from unittest.mock import patch

from shopping_snapshot import extract_shopping_review_snapshot
from network import load_target

logger = logging.getLogger(__name__)
URL = 'https://you.ctrip.com/shopping/154/1373439.html'
TIME = '2026-11-14T10:00:00+08:00'


def responses():
    overview = {'ResultCode':0, 'PoiOverviewInfo':{'BusinessId':1373439,'PoiId':18179130}}
    comments = {'ResultCode':0,'ResponseStatus':{'Ack':'Success','Errors':[]},
                'commentDetail':{'avgScore':4.7,'totalCount':38,
                    'scoreList':[{'name':'商品','score':4.7},{'name':'环境','score':4.6}],
                    'commentList':[{'commentId':'123','content':'页面可见样本','score':5,
                                    'imageCount':'1','imageList':[{'url':'https://dimg04.c-ctrip.com/x.jpg'}],
                                    'isBest':True,'author':{'nickName':'private-name','avatar':'private-avatar'}}],
                    'moreUrl':'https://m.ctrip.com/webapp/you/comment/list?businessId=1373439&businessType=13'},
                'talentNoteDetail':{'talentNoteList':[{'title':'未授权游记'}]}}
    return overview, comments


class ShoppingSnapshotTests(unittest.TestCase):
    def test_network_target_contains_shopping_review_snapshot(self):
        # /* ===== 步骤1：接入购物快照；数据源：模拟公开接口；操作：1) 读取对象 2) 留存评分和样本 ===== */
        logger.info('开始购物快照接入测试')
        overview, comments = responses()
        overview['PoiOverviewInfo'].update(PoiName='测试商场', CommentCount=38)
        with patch('network.post_json', side_effect=[overview, comments]):
            target = load_target(URL)
        self.assertEqual(target['shopping_review_overview_status'], 'captured')
        snapshot = target['shopping_review_overview']
        self.assertEqual((snapshot['average_score'], snapshot['display_total']), (4.7, 38))
        self.assertEqual(snapshot['comment_samples'][0]['commentId'], '123')
        self.assertFalse(snapshot['complete'])
        logger.info('购物快照接入测试完成')

    def test_invalid_snapshot_is_marked_without_losing_object(self):
        # /* ===== 步骤1：保留对象；数据源：不完整评分响应；操作：1) 标明不可用 2) 不阻断主体 ===== */
        logger.info('开始购物快照异常测试')
        overview, comments = responses()
        overview['PoiOverviewInfo'].update(PoiName='测试商场', CommentCount=38)
        comments['ResponseStatus']['Ack'] = 'Failure'
        with patch('network.post_json', side_effect=[overview, comments]):
            target = load_target(URL)
        self.assertEqual(target['resource_id'], 1373439)
        self.assertEqual(target['shopping_review_overview_status'], 'unavailable')
        self.assertNotIn('shopping_review_overview', target)
        logger.info('购物快照异常测试完成')

    def test_keeps_review_statistics_and_sample_with_source_boundary(self):
        # /* ===== 步骤1：留存点评概览；数据源：模拟购物响应；操作：1) 校验身份 2) 保留可见字段 ===== */
        logger.info('开始购物点评快照测试')
        overview, comments = responses()
        result = extract_shopping_review_snapshot(URL, overview, comments, 18179130, TIME)
        self.assertEqual(result['kind'], 'shopping')
        self.assertEqual((result['resource_id'], result['poi_id'], result['resource_type']), (1373439,18179130,13))
        self.assertEqual((result['display_total'], result['average_score']), (38,4.7))
        self.assertEqual(result['score_dimensions'][0], {'name':'商品','score':4.7})
        self.assertEqual(result['comment_samples'][0]['commentId'], '123')
        self.assertEqual(result['comment_samples'][0]['imageList'][0]['url'], 'https://dimg04.c-ctrip.com/x.jpg')
        self.assertNotIn('author', result['comment_samples'][0])
        self.assertNotIn('private-name', str(result))
        self.assertNotIn('未授权游记', str(result))
        self.assertFalse(result['complete'])
        self.assertEqual(result['comment_samples_status'], 'available')
        logger.info('购物点评快照测试完成')

    def test_rejects_wrong_identity_and_failure_response(self):
        # /* ===== 步骤2：拒绝混写；数据源：模拟错误响应；操作：1) 验证对象 2) 检查接口状态 ===== */
        logger.info('开始购物对象身份测试')
        overview, comments = responses()
        with self.assertRaises(ValueError):
            extract_shopping_review_snapshot(URL, overview, comments, 999, TIME)
        comments['commentDetail']['moreUrl'] = '?businessId=999&businessType=13'
        with self.assertRaises(ValueError):
            extract_shopping_review_snapshot(URL, overview, comments, 18179130, TIME)
        overview, comments = responses()
        comments['ResponseStatus']['Ack'] = 'Failure'
        with self.assertRaises(ValueError):
            extract_shopping_review_snapshot(URL, overview, comments, 18179130, TIME)
        logger.info('购物对象身份测试完成')

    def test_absence_and_malformed_lists_remain_explicit(self):
        # /* ===== 步骤3：标明缺失；数据源：缺失或异常列表；操作：1) 不猜空值 2) 保留其他字段 ===== */
        logger.info('开始购物字段缺失测试')
        overview, comments = responses()
        comments['commentDetail']['scoreList'] = {}
        del comments['commentDetail']['commentList']
        result = extract_shopping_review_snapshot(URL, overview, comments, 18179130, TIME)
        self.assertEqual(result['score_dimensions_status'], 'invalid')
        self.assertEqual(result['comment_samples_status'], 'missing')
        self.assertEqual(result['display_total'], 38)
        # 3.1 非字符串入口只能作为受控错误，不能打断调用方的主体对象降级逻辑。
        comments['commentDetail']['moreUrl'] = {'businessId':1373439}
        with self.assertRaises(ValueError):
            extract_shopping_review_snapshot(URL, overview, comments, 18179130, TIME)
        logger.info('购物字段缺失测试完成')

    def test_zero_review_does_not_fabricate_resource_type(self):
        # /* ===== 步骤4：检查零点评；数据源：无入口快照；操作：1) 保留零 2) 不猜业务类型 ===== */
        logger.info('开始零点评快照测试')
        overview, comments = responses()
        comments['commentDetail'].update(totalCount=0, commentList=[], moreUrl=None)
        result = extract_shopping_review_snapshot(URL, overview, comments, 18179130, TIME)
        self.assertIsNone(result['resource_type'])
        self.assertEqual(result['display_total'], 0)
        self.assertFalse(result['complete'])
        logger.info('零点评快照测试完成')
