"""
向量化后台任务队列

上传接口只负责落盘和入库，向量化交给本模块的进程内线程池执行，
避免大文件的向量化耗时把HTTP请求拖到超时（前端上传超时是300s）。

线程模型
--------
选用进程内 ThreadPoolExecutor 而非 Celery：本项目是单机 + 本机 Ollama，
引入 Redis 的运维成本远大于收益。代价是任务不持久化——所以进程启动时会把
上次退出时卡在 uploading 的文档重新入队（见 recover_pending_documents）。

数据库会话纪律（重要）
---------------------
worker 里绝不使用 Flask-SQLAlchemy 的 db.session：
1. db.session 按 app context 作用域绑定，且worker若在向量化前开一个读事务，
   InnoDB 默认的 REPEATABLE READ 会让它在整个向量化期间（可能几分钟）看不到
   其他请求的提交，并一直持有快照；
2. 因此这里统一改用独立短会话（Session(db.engine)），每次读写各自开合，
   既不长时间占用连接，也能读到最新已提交的数据。
"""
import atexit
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import db
from models.document import Document
from models.knowledge_base import KnowledgeBase
from services.vector_service import get_vector_service


# 线程池单例
_executor = None
_executor_lock = threading.Lock()

# 在途文档ID，用于去重（同一个文档不重复入队）
_inflight = set()
_inflight_lock = threading.Lock()

# 进度表：doc_id -> {'done', 'total', 'stage', 'update_time'}
_progress = {}
_progress_lock = threading.Lock()

# 记录已告警过恢复失败的文档，避免反复刷屏
_MAX_PROGRESS_ENTRIES = 200


def get_executor():
    """获取线程池单例（按配置的 VECTOR_WORKERS 创建）"""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                workers = max(1, int(current_app.config.get('VECTOR_WORKERS', 1)))
                _executor = ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix='vectorize'
                )
    return _executor


def shutdown(wait=False):
    """关闭线程池（进程退出时注册到 atexit）"""
    global _executor
    with _executor_lock:
        executor = _executor
        _executor = None
    if executor is not None:
        executor.shutdown(wait=wait)


atexit.register(shutdown)


# ----------------------------------------------------------------------
# 进度
# ----------------------------------------------------------------------

def _set_progress(doc_id, done, total, stage):
    """记录向量化进度，供前端轮询"""
    with _progress_lock:
        _progress[doc_id] = {
            'done': done,
            'total': total,
            'stage': stage,
            'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        # 简单上限控制：进度只是辅助信息，条目过多时清掉最早的一批
        if len(_progress) > _MAX_PROGRESS_ENTRIES:
            for key in sorted(_progress)[:len(_progress) - _MAX_PROGRESS_ENTRIES]:
                _progress.pop(key, None)


def _clear_progress(doc_id):
    """清除进度记录"""
    with _progress_lock:
        _progress.pop(doc_id, None)


def get_progress(doc_id):
    """
    读取文档的向量化进度
    :param doc_id: 文档ID
    :return: 进度字典；无记录返回 None
    """
    with _progress_lock:
        entry = _progress.get(doc_id)
        return dict(entry) if entry else None


# ----------------------------------------------------------------------
# 数据库短会话辅助（worker专用，不碰 db.session）
# ----------------------------------------------------------------------

def _get_status(doc_id):
    """读取文档当前状态，文档不存在时返回 None"""
    with Session(db.engine) as session:
        return session.execute(
            select(Document.status).where(Document.id == doc_id)
        ).scalar()


def _mark_vectorized(doc_id, kb_id, chunk_count):
    """
    把文档标记为已就绪并同步知识库文档计数
    :return: 是否真正更新（文档已被删除时返回 False）
    """
    with Session(db.engine) as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            return False
        doc.status = 'vectorized'
        doc.chunk_count = chunk_count

        kb = session.get(KnowledgeBase, kb_id)
        if kb is not None:
            kb.doc_count = session.query(Document).filter_by(
                kb_id=kb_id, status='vectorized'
            ).count()
        session.commit()
        return True


def _mark_failed(doc_id):
    """把文档标记为失败"""
    with Session(db.engine) as session:
        doc = session.get(Document, doc_id)
        if doc is None:
            return
        doc.status = 'failed'
        session.commit()


# ----------------------------------------------------------------------
# 任务执行
# ----------------------------------------------------------------------

def _run_vectorize(app, doc_id, file_path, file_type, kb_id):
    """
    向量化任务主体。
    :param app: Flask应用实例（由路由在请求上下文里取出后传入）
    """
    # 整个执行体（含异常处理）都放在应用上下文内，
    # 否则 except 分支里的 current_app 无法解析
    with app.app_context():
        try:
            # 入队后、执行前状态可能已变（例如文档被删除），先确认一次
            if _get_status(doc_id) != 'uploading':
                current_app.logger.info(f'文档{doc_id}状态已非uploading，跳过向量化')
                return

            service = get_vector_service()
            result = service.process_document(
                doc_id, file_path, file_type, kb_id,
                progress=lambda done, total, stage: _set_progress(doc_id, done, total, stage),
                # 每批写入前复查：文档在向量化途中被删除时应立即停止，
                # 否则会留下一批永远无法清理、却仍可被检索到的孤儿向量
                should_abort=lambda: _get_status(doc_id) != 'uploading'
            )

            if _mark_vectorized(doc_id, kb_id, result['chunk_count']):
                current_app.logger.info(
                    f'文档{doc_id}处理完成，共{result["chunk_count"]}块'
                )
        except Exception as e:
            # 文档已被删除导致的中止属于预期情况，不必标记失败
            if '文档已被删除' in str(e):
                current_app.logger.warning(f'文档{doc_id}在向量化途中被删除，已中止')
                # 中止前可能已经写入了几批分块，而此时数据库行已经没了，
                # 删除接口的清理早已跑完——不补这一刀就会留下永远无人清理、
                # 却仍能被检索出来的孤儿向量
                if _get_status(doc_id) is None:
                    try:
                        cleaned = get_vector_service().delete_document(doc_id, kb_id)
                        current_app.logger.info(
                            f'已清理文档{doc_id}中止前写入的{cleaned}个分块'
                        )
                    except Exception as cleanup_error:
                        current_app.logger.warning(
                            f'清理文档{doc_id}的残留分块失败: {cleanup_error}'
                        )
                return

            current_app.logger.error(f'文档{doc_id}向量化失败: {e}\n{traceback.format_exc()}')
            try:
                _mark_failed(doc_id)
            except Exception as mark_error:
                current_app.logger.warning(f'标记文档{doc_id}失败状态时出错: {mark_error}')
        finally:
            with _inflight_lock:
                _inflight.discard(doc_id)
            _clear_progress(doc_id)


def submit_vectorize(app, doc_id, file_path, file_type, kb_id):
    """
    提交向量化任务（非阻塞）。
    :param app: Flask应用实例（路由内用 current_app._get_current_object() 取得）
    :param doc_id: 文档ID
    :param file_path: 文件路径
    :param file_type: 文件类型
    :param kb_id: 知识库ID
    :return: 是否成功入队
    """
    with _inflight_lock:
        if doc_id in _inflight:
            return True
        queue_size = int(current_app.config.get('VECTOR_QUEUE_SIZE', 100))
        if len(_inflight) >= queue_size:
            current_app.logger.warning(f'向量化队列已满({queue_size})，拒绝文档{doc_id}')
            return False
        _inflight.add(doc_id)

    try:
        get_executor().submit(_run_vectorize, app, doc_id, file_path, file_type, kb_id)
    except Exception as e:
        with _inflight_lock:
            _inflight.discard(doc_id)
        current_app.logger.error(f'提交向量化任务失败(doc_id={doc_id}): {e}')
        return False
    return True


def recover_pending_documents(app, limit=None):
    """
    启动恢复：把上次进程退出时卡在 uploading 的文档重新入队。
    线程池任务不持久化，没有这一步的话，服务重启会让这些文档永远停在"处理中"。
    :param app: Flask应用实例
    :param limit: 最多恢复的文档数，None表示不限制
    :return: 重新入队的文档数
    """
    with app.app_context():
        query = Document.query.filter_by(status='uploading').order_by(Document.id.asc())
        if limit:
            query = query.limit(limit)
        pending = query.all()
        if not pending:
            return 0

        recovered = 0
        for doc in pending:
            # 文件被删掉的情况下重新入队也没意义，直接判失败
            if not doc.file_path or not os.path.exists(doc.file_path):
                current_app.logger.warning(
                    f'文档{doc.id}的源文件已丢失，标记为失败: {doc.file_path}'
                )
                _mark_failed(doc.id)
                continue
            if submit_vectorize(app, doc.id, doc.file_path, doc.file_type, doc.kb_id):
                recovered += 1

        current_app.logger.info(f'启动恢复: 重新入队 {recovered}/{len(pending)} 个待处理文档')
        return recovered
