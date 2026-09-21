"""
问答答案缓存模型
对应数据库表 t_answer_cache（两级缓存中的L2持久层）
"""
from models import db
from datetime import datetime


class AnswerCache(db.Model):
    """问答答案缓存表ORM模型"""

    __tablename__ = 't_answer_cache'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True, comment='缓存ID')
    kb_id = db.Column(db.Integer, nullable=False, comment='知识库ID')
    question_hash = db.Column(db.String(64), nullable=False, comment='归一化问题的sha256')
    question = db.Column(db.String(500), nullable=False, comment='原始问题（截断，仅用于排查）')
    answer = db.Column(db.Text, nullable=False, comment='回答文本')
    source_docs = db.Column(db.Text, default=None, comment='参考来源JSON')
    hit_count = db.Column(db.Integer, nullable=False, default=0, comment='命中次数（入库那次算未命中，从0开始）')
    create_time = db.Column(db.DateTime, nullable=False, default=datetime.now, comment='创建时间')
    update_time = db.Column(db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now, comment='更新时间')
    expire_time = db.Column(db.DateTime, nullable=False, comment='过期时间（L2 TTL）')
