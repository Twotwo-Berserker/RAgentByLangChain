"""
问答对话路由
提供RAG问答和对话历史查询接口
"""
import uuid
import json
from flask import Blueprint, request, g, Response, stream_with_context
from models import db
from models.chat_history import ChatHistory
from models.knowledge_base import KnowledgeBase
from utils.auth import login_required
from utils.response import success, error, page_response
from services.rag_service import get_rag_service

# 创建问答蓝图
chat_bp = Blueprint('chat', __name__)


@chat_bp.route('/ask', methods=['POST'])
@login_required
def ask():
    """
    RAG知识库问答接口
    请求参数: question(问题), kb_id(知识库ID), session_id(会话ID，可选)
    返回: AI回答和参考来源
    """
    data = request.get_json()
    if not data:
        return error('请提供问题信息')

    question = data.get('question', '').strip()
    kb_id = data.get('kb_id')
    session_id = data.get('session_id', str(uuid.uuid4().hex[:16]))

    if not question:
        return error('问题不能为空')
    if not kb_id:
        return error('请选择知识库')

    # 验证知识库是否存在
    kb = KnowledgeBase.query.get(kb_id)
    if not kb or kb.status != 1:
        return error('知识库不存在或已禁用')

    # 调用RAG服务进行问答（单例复用，避免重复初始化）
    try:
        rag_service = get_rag_service()
        answer, source_docs = rag_service.ask(question, kb_id)
    except Exception as e:
        return error(f'问答服务异常: {str(e)}')

    # 保存对话记录
    chat = ChatHistory(
        user_id=g.user_id,
        kb_id=kb_id,
        session_id=session_id,
        question=question,
        answer=answer,
        source_docs=json.dumps(source_docs, ensure_ascii=False)
    )
    db.session.add(chat)
    db.session.commit()

    return success({
        'answer': answer,
        'source_docs': source_docs,
        'session_id': session_id,
        'chat_id': chat.id
    })


@chat_bp.route('/ask/stream', methods=['POST'])
@login_required
def ask_stream():
    """
    RAG流式问答接口（NDJSON）
    请求参数: question(问题), kb_id(知识库ID), session_id(会话ID，可选)
    返回: application/x-ndjson 流，逐行输出JSON事件
         {'type':'sources','data':[...]}  参考来源
         {'type':'token','data':'...'}    回答片段
         {'type':'done','data':{...}}     完成（含chat_id等）
         {'type':'error','data':'...'}    错误
    """
    data = request.get_json()
    if not data:
        return error('请提供问题信息')

    question = data.get('question', '').strip()
    kb_id = data.get('kb_id')
    session_id = data.get('session_id', str(uuid.uuid4().hex[:16]))

    if not question:
        return error('问题不能为空')
    if not kb_id:
        return error('请选择知识库')

    # 验证知识库是否存在
    kb = KnowledgeBase.query.get(kb_id)
    if not kb or kb.status != 1:
        return error('知识库不存在或已禁用')

    # 在进入生成器前捕获用户ID，避免依赖请求上下文
    user_id = g.user_id
    rag_service = get_rag_service()

    def generate():
        answer_parts = []
        source_docs = []
        try:
            for event in rag_service.stream(question, kb_id):
                if event['type'] == 'sources':
                    source_docs = event['data']
                elif event['type'] == 'token':
                    answer_parts.append(event['data'])
                yield json.dumps(event, ensure_ascii=False) + '\n'

            # 流式结束后保存对话记录
            answer = ''.join(answer_parts)
            chat = ChatHistory(
                user_id=user_id,
                kb_id=kb_id,
                session_id=session_id,
                question=question,
                answer=answer,
                source_docs=json.dumps(source_docs, ensure_ascii=False)
            )
            db.session.add(chat)
            db.session.commit()

            yield json.dumps({
                'type': 'done',
                'data': {
                    'answer': answer,
                    'source_docs': source_docs,
                    'session_id': session_id,
                    'chat_id': chat.id
                }
            }, ensure_ascii=False) + '\n'
        except Exception as e:
            yield json.dumps(
                {'type': 'error', 'data': f'问答服务异常: {str(e)}'},
                ensure_ascii=False
            ) + '\n'

    return Response(
        stream_with_context(generate()),
        mimetype='application/x-ndjson'
    )


@chat_bp.route('/history', methods=['GET'])
@login_required
def get_history():
    """
    获取对话历史列表（分页）
    查询参数: page, page_size, kb_id(可选)
    普通用户只能查看自己的记录，管理员可查看所有
    """
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)
    kb_id = request.args.get('kb_id', type=int)

    query = ChatHistory.query

    # 普通用户只能查看自己的对话记录
    if g.role != 'admin':
        query = query.filter_by(user_id=g.user_id)

    if kb_id:
        query = query.filter_by(kb_id=kb_id)

    query = query.order_by(ChatHistory.create_time.desc())
    pagination = query.paginate(page=page, per_page=page_size, error_out=False)

    items = [item.to_dict() for item in pagination.items]
    return page_response(items, pagination.total, page, page_size)


@chat_bp.route('/session/<session_id>', methods=['GET'])
@login_required
def get_session(session_id):
    """
    获取指定会话的所有对话记录
    路径参数: session_id(会话ID)
    """
    query = ChatHistory.query.filter_by(session_id=session_id)

    # 普通用户只能查看自己的对话
    if g.role != 'admin':
        query = query.filter_by(user_id=g.user_id)

    chats = query.order_by(ChatHistory.create_time.asc()).all()
    return success([chat.to_dict() for chat in chats])
