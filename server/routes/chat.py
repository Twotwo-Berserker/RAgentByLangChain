"""
问答对话路由
提供RAG问答和对话历史查询接口
"""
import uuid
import json
from datetime import datetime
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
        result = rag_service.ask(question, kb_id)
    except Exception as e:
        return error(f'问答服务异常: {str(e)}')

    answer = result['answer']
    source_docs = result['source_docs']

    # 保存对话记录（命中缓存的问答同样留痕，否则历史里会缺记录）
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
        'chat_id': chat.id,
        'from_cache': result['from_cache']
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
        from_cache = False
        try:
            for event in rag_service.stream(question, kb_id):
                if event['type'] == 'cache':
                    # 内部事件，仅用于标记来源，不转发给前端
                    from_cache = bool(event['data'])
                    continue
                if event['type'] == 'sources':
                    source_docs = event['data']
                elif event['type'] == 'token':
                    answer_parts.append(event['data'])
                yield json.dumps(event, ensure_ascii=False) + '\n'

            # 流式结束后保存对话记录（命中缓存的问答同样留痕）
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
                    'chat_id': chat.id,
                    'from_cache': from_cache
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


@chat_bp.route('/feedback', methods=['POST'])
@login_required
def feedback():
    """
    提交回答反馈（赞/踩），用于后续优化
    请求参数: chat_id(对话记录ID), feedback(1-赞，-1-踩，0-取消), comment(可选说明)
    """
    data = request.get_json()
    if not data:
        return error('请提供反馈信息')

    chat_id = data.get('chat_id')
    value = data.get('feedback')
    comment = (data.get('comment') or '').strip()

    if not chat_id:
        return error('缺少对话记录ID')
    if value not in (1, -1, 0):
        return error('反馈类型不合法，仅支持 1(赞) / -1(踩) / 0(取消)')

    chat = ChatHistory.query.get(chat_id)
    if not chat:
        return error('对话记录不存在', 404)

    # 普通用户只能反馈自己的问答记录，管理员不限
    if g.role != 'admin' and chat.user_id != g.user_id:
        return error('无权操作该对话记录', 403)

    chat.feedback = value
    chat.feedback_comment = comment if value == -1 else ''
    chat.feedback_time = datetime.now() if value != 0 else None
    db.session.commit()

    return success({
        'chat_id': chat.id,
        'feedback': chat.feedback,
        'feedback_comment': chat.feedback_comment or '',
        'feedback_time': chat.feedback_time.strftime('%Y-%m-%d %H:%M:%S') if chat.feedback_time else ''
    }, '反馈成功' if value != 0 else '已取消反馈')


@chat_bp.route('/history', methods=['GET'])
@login_required
def get_history():
    """
    获取对话历史列表（分页）
    查询参数: page, page_size, kb_id(可选), feedback(可选), user_id(可选，仅管理员)
    普通用户只能查看自己的记录，管理员可查看所有
    """
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)
    kb_id = request.args.get('kb_id', type=int)
    feedback = request.args.get('feedback', type=int)
    user_id = request.args.get('user_id', type=int)

    query = ChatHistory.query

    # 普通用户只能查看自己的对话记录
    if g.role != 'admin':
        query = query.filter_by(user_id=g.user_id)
    elif user_id:
        # 管理员可以按提问者筛选（普通用户走上面的分支，该参数对其无效）
        query = query.filter_by(user_id=user_id)

    if kb_id:
        query = query.filter_by(kb_id=kb_id)

    # 按反馈筛选（-1 可快速捞出被踩的问答，用于后续优化）
    if feedback in (1, -1, 0):
        query = query.filter_by(feedback=feedback)

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
