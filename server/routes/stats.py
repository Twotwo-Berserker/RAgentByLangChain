"""
统计数据路由
提供管理员首页的统计数据接口
"""
from flask import Blueprint
from datetime import datetime, timedelta
from sqlalchemy import func
from models import db
from models.user import User
from models.knowledge_base import KnowledgeBase
from models.document import Document
from models.chat_history import ChatHistory
from utils.auth import admin_required
from utils.response import success

# 创建统计蓝图
stats_bp = Blueprint('stats', __name__)


@stats_bp.route('/overview', methods=['GET'])
@admin_required
def overview():
    """
    获取首页统计概览数据（仅管理员）
    返回: 用户数、知识库数、文档数、今日提问数、近7天趋势、知识库文档占比
    """
    # 基础统计数量
    user_count = User.query.filter_by(status=1).count()
    kb_count = KnowledgeBase.query.filter_by(status=1).count()
    doc_count = Document.query.filter_by(status='vectorized').count()

    # 今日提问数量
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_chat_count = ChatHistory.query.filter(ChatHistory.create_time >= today_start).count()

    # 近7天每日提问数量趋势
    trend_data = []
    for i in range(6, -1, -1):
        day = datetime.now() - timedelta(days=i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        count = ChatHistory.query.filter(
            ChatHistory.create_time >= day_start,
            ChatHistory.create_time < day_end
        ).count()
        trend_data.append({
            'date': day_start.strftime('%m-%d'),
            'count': count
        })

    # 各知识库文档数量占比
    kb_doc_stats = db.session.query(
        KnowledgeBase.kb_name,
        KnowledgeBase.doc_count
    ).filter(
        KnowledgeBase.status == 1,
        KnowledgeBase.doc_count > 0
    ).all()

    kb_doc_data = [{'name': name, 'value': count} for name, count in kb_doc_stats]

    # 回答反馈统计：赞 / 踩 / 未评价，以及好评率（赞占已评价的比例）
    like_count = ChatHistory.query.filter_by(feedback=1).count()
    dislike_count = ChatHistory.query.filter_by(feedback=-1).count()
    unrated_count = ChatHistory.query.filter_by(feedback=0).count()
    rated_count = like_count + dislike_count
    like_rate = round(like_count / rated_count * 100, 1) if rated_count else 0.0

    # 被踩最多的知识库，用于定位需要补充/修正内容的知识库
    bad_kb_stats = db.session.query(
        KnowledgeBase.kb_name,
        func.count(ChatHistory.id)
    ).join(
        ChatHistory, ChatHistory.kb_id == KnowledgeBase.id
    ).filter(
        ChatHistory.feedback == -1
    ).group_by(
        KnowledgeBase.kb_name
    ).order_by(
        func.count(ChatHistory.id).desc()
    ).limit(5).all()

    return success({
        'user_count': user_count,
        'kb_count': kb_count,
        'doc_count': doc_count,
        'today_chat_count': today_chat_count,
        'trend_data': trend_data,
        'kb_doc_data': kb_doc_data,
        'feedback_stats': {
            'like_count': like_count,
            'dislike_count': dislike_count,
            'unrated_count': unrated_count,
            'rated_count': rated_count,
            'like_rate': like_rate,
            'bad_kb_data': [{'name': name, 'value': count} for name, count in bad_kb_stats]
        }
    })
