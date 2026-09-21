"""
文档路由
提供文档上传、列表查询和删除接口
"""
import os
import uuid
from flask import Blueprint, request, g, current_app
from models import db
from models.document import Document
from models.knowledge_base import KnowledgeBase
from utils.auth import login_required, admin_required
from utils.response import success, error, page_response
from services.vector_service import get_vector_service
from services.task_queue import submit_vectorize, get_progress


# 创建文档蓝图
doc_bp = Blueprint('document', __name__)


def allowed_file(filename):
    """检查文件扩展名是否允许上传"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in current_app.config['ALLOWED_EXTENSIONS']


@doc_bp.route('/list', methods=['GET'])
@login_required
def get_list():
    """
    获取文档列表（分页）
    查询参数: page, page_size, kb_id
    """
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)
    kb_id = request.args.get('kb_id', type=int)

    query = Document.query
    if kb_id:
        query = query.filter_by(kb_id=kb_id)

    query = query.order_by(Document.create_time.desc())
    pagination = query.paginate(page=page, per_page=page_size, error_out=False)

    items = [item.to_dict() for item in pagination.items]
    return page_response(items, pagination.total, page, page_size)


@doc_bp.route('/upload', methods=['POST'])
@admin_required
def upload():
    """
    上传文档并提交后台向量化（仅管理员）
    表单参数: file（文件）, kb_id（知识库ID）
    接口立即返回，向量化在后台线程池进行，处理进度通过 GET /<doc_id> 查询
    """
    if 'file' not in request.files:
        return error('请选择要上传的文件')

    file = request.files['file']
    kb_id = request.form.get('kb_id', type=int)

    if not kb_id:
        return error('请选择知识库')

    if file.filename == '':
        return error('请选择要上传的文件')

    if not allowed_file(file.filename):
        return error(f"不支持的文件类型，仅支持: {', '.join(current_app.config['ALLOWED_EXTENSIONS'])}")

    # 验证知识库是否存在
    kb = KnowledgeBase.query.get(kb_id)
    if not kb:
        return error('知识库不存在')

    # 生成唯一文件名并保存
    file_ext = file.filename.rsplit('.', 1)[1].lower()
    unique_name = f"{uuid.uuid4().hex}.{file_ext}"
    file_path = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_name)
    file.save(file_path)

    # 获取文件大小
    file_size = os.path.getsize(file_path)

    # 创建文档记录，状态为处理中
    doc = Document(
        kb_id=kb_id,
        file_name=file.filename,
        file_path=file_path,
        file_size=file_size,
        file_type=file_ext,
        status='uploading',
        creator_id=g.user_id
    )
    db.session.add(doc)
    # 必须先提交再投递任务：worker用的是独立会话，看不见未提交的行；
    # 而且本请求未结束的事务还会持有行锁，让worker的更新一直等到锁超时
    db.session.commit()

    # 提交后台向量化（非阻塞）
    app = current_app._get_current_object()
    if not submit_vectorize(app, doc.id, file_path, file_ext, kb_id):
        doc.status = 'failed'
        db.session.commit()
        return error('向量化队列已满，请稍后重试')

    return success(doc.to_dict(), '上传成功，正在后台处理')


@doc_bp.route('/<int:doc_id>', methods=['GET'])
@admin_required
def get_detail(doc_id):
    """
    获取单个文档的处理状态与向量化进度（仅管理员）
    供前端在上传后轮询，直到状态不再是 uploading
    """
    doc = Document.query.get(doc_id)
    if not doc:
        return error('文档不存在', 404)

    data = doc.to_dict()
    data['progress'] = get_progress(doc_id)
    return success(data)


@doc_bp.route('/<int:doc_id>', methods=['DELETE'])
@admin_required
def delete(doc_id):
    """
    删除文档（仅管理员）
    同时删除对应的向量数据和物理文件
    """
    doc = Document.query.get(doc_id)
    if not doc:
        return error('文档不存在', 404)

    kb_id = doc.kb_id

    # 删除向量数据（遍历该知识库的全部分片与遗留collection）
    try:
        vector_service = get_vector_service()
        deleted = vector_service.delete_document(doc.id, kb_id)
        current_app.logger.info(f'文档{doc_id}已删除{deleted}个向量分块')
    except Exception as e:
        # 向量删除失败不应阻塞文档本身的下线，但必须留下日志便于排查残留
        current_app.logger.warning(f'删除文档{doc_id}的向量数据失败: {e}')

    # 删除物理文件
    if doc.file_path and os.path.exists(doc.file_path):
        os.remove(doc.file_path)

    # 删除数据库记录
    db.session.delete(doc)

    # 更新知识库文档计数
    # 注意：这里不能再减1——上面的 delete 会在查询前自动flush，count() 已经把本行排除掉了
    kb = KnowledgeBase.query.get(kb_id)
    if kb:
        kb.doc_count = Document.query.filter_by(kb_id=kb_id, status='vectorized').count()

    db.session.commit()
    return success(message='删除成功')
