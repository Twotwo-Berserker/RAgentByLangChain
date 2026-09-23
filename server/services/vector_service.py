"""
文档向量化服务
负责文档解析、文本分块、分片向量存储与增量更新

索引结构
--------
每个知识库按 doc_id 的稳定哈希拆成 SHARD_COUNT 个collection（kb_{id}_shard_{n}），
避免单个collection无上界增长。读取时遍历该知识库的全部分片并在内存里归并取全局top_k。

为兼容改造前的单collection（kb_{id}），读取路径会把遗留collection一并纳入。
遗留数据不需要专门的迁移脚本：文档重新向量化时，它的旧分块会因id不在新集合里而被
判定为"已删除"并从遗留collection里清掉，即惰性、按文档、自愈的迁移。

增量更新
--------
分块id由内容哈希决定（doc_{doc_id}_{sha1(内容)[:16]}_{出现序号}），重新向量化时
比对现有id集合，只嵌入新增/变动的分块、只删除消失的分块，未变的分块原样保留。
注意：首次切换到哈希id时，存量分块用的是旧的 doc_{doc_id}_chunk_{i} 方案，
因此每个文档的第一次重新向量化仍会全量重嵌——增量特性从第二次开始生效。
"""
import os
import time
import zlib
import hashlib
import threading
import chromadb
from flask import current_app
from ollama import Client as OllamaClient, ResponseError
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_ollama import OllamaEmbeddings
from langchain_chroma import Chroma
from services.cache_service import get_cache_service


class OllamaServiceError(Exception):
    """Ollama服务相关异常，用于提供更清晰的错误提示"""
    pass


# 单例缓存：避免每次请求重复初始化嵌入模型客户端/分割器
_vector_service_instance = None
_vector_service_lock = threading.Lock()

# 分页读取分块（iter_chunks）时每页的条数
CHUNK_PAGE_SIZE = 500


def get_vector_service():
    """
    获取VectorService单例。
    RAGService 与文档上传/删除共用同一个实例，保证检索缓存能够被正确失效。
    """
    global _vector_service_instance
    if _vector_service_instance is None:
        with _vector_service_lock:
            if _vector_service_instance is None:
                _vector_service_instance = VectorService()
    return _vector_service_instance


class VectorService:
    """文档向量化服务类"""

    def __init__(self):
        """初始化嵌入模型、文本分割器和Chroma客户端"""
        self.embeddings = OllamaEmbeddings(
            model=current_app.config['OLLAMA_EMBED_MODEL'],
            base_url=current_app.config['OLLAMA_BASE_URL']
        )
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=current_app.config['CHUNK_SIZE'],
            chunk_overlap=current_app.config['CHUNK_OVERLAP'],
            length_function=len
        )
        self.persist_dir = current_app.config['CHROMA_PERSIST_DIR']
        self.batch_size = current_app.config.get('EMBED_BATCH_SIZE', 10)
        self.max_retries = current_app.config.get('EMBED_MAX_RETRIES', 3)
        self.top_k = current_app.config.get('RETRIEVER_TOP_K', 4)
        self.shard_count = max(1, int(current_app.config.get('SHARD_COUNT', 1)))
        self.read_legacy = current_app.config.get('SHARD_READ_LEGACY', True)

        # 共享一个Chroma客户端：多个Chroma实例各自建 PersistentClient 会触发
        # chromadb 的 SharedSystemClient 设置冲突告警
        self._client = chromadb.PersistentClient(path=self.persist_dir)

        # 读路径缓存：kb_id -> [Chroma,...]，避免每次问答都重建连接
        self._store_cache = {}
        self._store_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Ollama 预检查
    # ------------------------------------------------------------------

    def _check_ollama(self):
        """
        预检查Ollama服务可用性和嵌入模型是否就绪。
        仅在"服务未启动"和"模型未安装"时硬拦截；
        5xx等瞬时错误只记录警告，让后续重试机制处理。
        :raises OllamaServiceError: 服务不可达或模型未安装时抛出
        """
        base_url = current_app.config['OLLAMA_BASE_URL']
        model_name = current_app.config['OLLAMA_EMBED_MODEL']
        client = OllamaClient(host=base_url)

        try:
            model_list = client.list()
        except ConnectionError:
            raise OllamaServiceError(
                f'无法连接Ollama服务({base_url})，请确认Ollama已启动'
            )
        except ResponseError as e:
            current_app.logger.warning(
                f'Ollama预检查返回异常(status {e.status_code})，将继续尝试向量化: {e}'
            )
            return
        except Exception as e:
            current_app.logger.warning(f'Ollama预检查失败，将继续尝试向量化: {e}')
            return

        installed = {m.get('name', '') for m in model_list.get('models', [])}
        if not any(model_name in name or name in model_name for name in installed):
            raise OllamaServiceError(
                f'嵌入模型 {model_name} 未安装，请先执行: ollama pull {model_name}'
            )

    # ------------------------------------------------------------------
    # 文件解析与分块
    # ------------------------------------------------------------------

    def _load_file(self, file_path, file_type):
        """
        根据文件类型加载文档内容
        :param file_path: 文件路径
        :param file_type: 文件类型（txt/pdf/md/docx）
        :return: 文本内容
        """
        text = ''
        if file_type in ('txt', 'md'):
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()

        elif file_type == 'pdf':
            from pypdf import PdfReader
            reader = PdfReader(file_path)
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + '\n'

        elif file_type == 'docx':
            from docx import Document as DocxDocument
            doc = DocxDocument(file_path)
            for para in doc.paragraphs:
                if para.text.strip():
                    text += para.text + '\n'

        return text

    @staticmethod
    def _chunk_hash(text):
        """分块内容的sha1前缀，作为分块id的一部分"""
        return hashlib.sha1(text.encode('utf-8')).hexdigest()[:16]

    @classmethod
    def _build_chunk_records(cls, doc_id, file_name, chunks):
        """
        为分块生成稳定的id与元数据。
        id 形如 doc_{doc_id}_{内容哈希}_{出现序号}：以内容哈希为主键，内容没变id就不变，
        因此重新向量化时可以直接比对id做增量；序号用于区分同一文档内内容完全相同的分块
        ——若不带序号，重复段落会生成相同id，Chroma会抛DuplicateIDError，
        而该错误不含502/503/504，重试逻辑会直接判定不可重试并让文档处理失败。
        :param doc_id: 文档ID
        :param file_name: 文件名（写入元数据，用于问答溯源）
        :param chunks: 文本分块列表
        :return: [(id, text, metadata), ...]
        """
        seen = {}
        records = []
        for index, text in enumerate(chunks):
            digest = cls._chunk_hash(text)
            occ = seen.get(digest, 0)
            seen[digest] = occ + 1
            chunk_id = f"doc_{doc_id}_{digest}_{occ}"
            records.append((
                chunk_id,
                text,
                {
                    'doc_id': doc_id,
                    'file_name': file_name,
                    'chunk_index': index,
                    'chunk_hash': digest
                }
            ))
        return records

    # ------------------------------------------------------------------
    # 分片与collection解析
    # ------------------------------------------------------------------

    def _legacy_collection_name(self, kb_id):
        """改造前的单collection名称（兼容存量数据）"""
        return f"kb_{kb_id}"

    def _shard_collection_name(self, kb_id, shard):
        """分片collection名称"""
        return f"kb_{kb_id}_shard_{shard}"

    def _shard_index(self, doc_id):
        """
        计算文档所属分片。
        必须用 crc32 这类稳定哈希，不能用 Python 内置 hash()——后者每个进程的种子不同，
        重启后同一个文档会被映射到不同分片，造成数据分裂。
        """
        return zlib.crc32(str(doc_id).encode('utf-8')) % self.shard_count

    def _target_collection_name(self, kb_id, doc_id):
        """文档应当写入的collection名称"""
        return self._shard_collection_name(kb_id, self._shard_index(doc_id))

    def _existing_names(self):
        """当前Chroma中已存在的全部collection名称集合"""
        return {str(name) for name in self._client.list_collections()}

    def _resolve_collections(self, kb_id):
        """
        解析该知识库当前实际存在的collection列表（遗留 + 全部分片），按名称排序。
        读取路径必须遍历这里返回的全部collection，否则存量数据或部分分片会被漏掉。
        :param kb_id: 知识库ID
        :return: collection名称元组
        """
        names = self._existing_names()
        resolved = []
        if self.read_legacy:
            legacy = self._legacy_collection_name(kb_id)
            if legacy in names:
                resolved.append(legacy)
        for shard in range(self.shard_count):
            shard_name = self._shard_collection_name(kb_id, shard)
            if shard_name in names:
                resolved.append(shard_name)
        return tuple(sorted(resolved))

    def _get_vectorstore(self, collection_name):
        """
        打开指定collection（不缓存，用于读写操作）
        :param collection_name: collection名称
        :return: Chroma实例
        """
        return Chroma(
            collection_name=collection_name,
            embedding_function=self.embeddings,
            client=self._client
        )

    def _get_stores(self, kb_id):
        """
        获取该知识库的全部collection（带缓存），供读取路径使用
        :param kb_id: 知识库ID
        :return: Chroma实例列表
        """
        with self._store_lock:
            stores = self._store_cache.get(kb_id)
            if stores is None:
                stores = [self._get_vectorstore(name) for name in self._resolve_collections(kb_id)]
                self._store_cache[kb_id] = stores
            return stores

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def iter_chunks(self, kb_id, page_size=None):
        """
        分页遍历该知识库的全部分块（遗留collection + 全部分片），
        供词法索引(BM25)这类需要全量语料的场景使用。

        用 limit/offset 分页而不是一次 get()：Chroma 的 get 会把命中的文档与元数据
        一次性物化到内存，小库上无所谓，语料增长后单次响应体会同步膨胀；分页让峰值
        内存与语料规模解耦。

        两点使用约束：
        1. 这是生成器，异常在迭代时才抛出，调用方必须在 for 循环外做 try/except；
        2. **不按 id 去重**——迁移期为兼容存量数据，同一分块可能同时存在于遗留
           collection 与分片里，两处都会产出。调用方必须自己按 id 去重，
           否则同一分块会占两个候选位，语料总数与文档频率也会偏大。

        :param kb_id: 知识库ID
        :param page_size: 每页条数
        :return: 生成器，逐条产出 (chunk_id, 正文, 元数据)
        """
        page_size = max(1, int(page_size or CHUNK_PAGE_SIZE))

        for store in self._get_stores(kb_id):
            collection = store._collection
            name = collection.name
            try:
                total = collection.count()
            except Exception as e:
                current_app.logger.warning(f'读取分块总数失败已跳过(collection={name}): {e}')
                continue

            offset = 0
            while offset < total:
                try:
                    # include 必须显式带上 documents：ids 总是返回，但正文不会顺带
                    # 带出。绝不请求 embeddings——那会把整库向量拉进内存
                    res = collection.get(
                        limit=page_size,
                        offset=offset,
                        include=['documents', 'metadatas']
                    )
                except Exception as e:
                    # 单个分片异常不能中断整库的语料收集，已取到的部分照常使用
                    current_app.logger.warning(
                        f'分页读取分块失败已跳过(collection={name}): {e}'
                    )
                    break

                ids = res.get('ids') or []
                if not ids:
                    break
                documents = res.get('documents') or []
                metadatas = res.get('metadatas') or []
                for i, chunk_id in enumerate(ids):
                    text = documents[i] if i < len(documents) else None
                    if not text:
                        continue
                    metadata = metadatas[i] if i < len(metadatas) else None
                    yield chunk_id, text, metadata or {}

                # 用实际返回条数推进：分页不是快照，期间可能有并发写入，
                # 按 page_size 推进会在短页时漏读。最坏情况是某次查询看到部分语料，
                # 而写入方的 invalidate() 会触发下一次重建
                offset += len(ids)

    def search(self, kb_id, question, top_k=None):
        """
        跨分片检索：查询向量只嵌入一次，再分发到各分片，最后归并取全局top_k。
        若改成对每个分片调用 similarity_search_with_score，N个分片就是N次Ollama往返，
        会把之前"回答慢"的优化整个吃回去。

        "每个分片取top_k再归并"是精确而非近似的：若某文档在并集top_k内，
        则并集中比它近的至多k-1个，其所在分片内比它近的也至多k-1个，
        故它必然在该分片的top_k内，不会被漏掉。

        :param kb_id: 知识库ID
        :param question: 用户问题
        :param top_k: 返回数量，默认取配置的 RETRIEVER_TOP_K
        :return: Document列表，按相似度从高到低
        """
        k = top_k or self.top_k
        stores = self._get_stores(kb_id)
        if not stores:
            return []

        query_vector = self.embeddings.embed_query(question)

        merged = []
        for store in stores:
            try:
                # 跳过空分片并把k收敛到分片实际元素数：
                # 否则Chroma会对"请求数超过索引内元素数"每条查询刷一次WARNING，
                # 小知识库上每次提问都会刷满日志，看着像故障
                available = store._collection.count()
                if available == 0:
                    continue
                # 返回的score是距离，越小越相似（Chroma默认l2）
                scored = store.similarity_search_by_vector_with_relevance_scores(
                    query_vector, k=min(k, available)
                )
            except Exception as e:
                # 单个分片异常不能让整个问答挂掉，跳过并告警
                current_app.logger.warning(
                    f'分片检索失败已跳过(collection={store._collection.name}): {e}'
                )
                continue
            merged.extend(scored)

        # 按距离升序；同一分块若因迁移残留出现在多个collection里，只保留距离最小的那条
        merged.sort(key=lambda pair: pair[1])
        results = []
        seen_ids = set()
        for doc, _score in merged:
            if doc.id in seen_ids:
                continue
            seen_ids.add(doc.id)
            results.append(doc)
            if len(results) >= k:
                break
        return results

    # ------------------------------------------------------------------
    # 增量写入
    # ------------------------------------------------------------------

    def _read_existing(self, doc_id, stores):
        """
        读出该文档在各collection中现存的全部分块id及其归属。
        返回归属信息是必须的：删除时要落到真正持有该分块的collection上，
        否则遗留collection里的旧分块永远删不掉，会一直作为参考来源被检索出来。
        :param doc_id: 文档ID
        :param stores: 待遍历的Chroma实例列表
        :return: {分块id: collection名称}
        """
        existing = {}
        for store in stores:
            try:
                res = store._collection.get(where={'doc_id': doc_id}, include=['metadatas'])
            except Exception as e:
                current_app.logger.warning(
                    f'读取分块失败已跳过(collection={store._collection.name}): {e}'
                )
                continue
            name = store._collection.name
            for chunk_id in res.get('ids') or []:
                existing[chunk_id] = name
        return existing

    def _delete_ids(self, ids_by_collection):
        """
        按归属collection分组删除分块
        :param ids_by_collection: {collection名称: [分块id, ...]}
        """
        for collection_name, ids in ids_by_collection.items():
            if not ids:
                # Collection.delete(ids=[]) 会抛 ValueError，必须判空
                continue
            self._get_vectorstore(collection_name)._collection.delete(ids=ids)

    def _add_texts_with_retry(self, collection_name, texts, metadatas, ids):
        """
        带重试的向量写入，处理Ollama瞬时故障(502/503/504等)
        :param collection_name: 目标collection
        :param texts: 文本分块列表
        :param metadatas: 元数据列表
        :param ids: ID列表
        """
        vectorstore = self._get_vectorstore(collection_name)
        last_error = None
        for attempt in range(self.max_retries):
            try:
                vectorstore.add_texts(texts=texts, metadatas=metadatas, ids=ids)
                return
            except Exception as e:
                last_error = e
                err_msg = str(e)
                is_retryable = any(code in err_msg for code in ('502', '503', '504'))
                if not is_retryable or attempt == self.max_retries - 1:
                    raise
                wait = 2 ** attempt
                current_app.logger.warning(
                    f'Ollama嵌入请求失败(第{attempt + 1}次)，{wait}秒后重试: {err_msg}'
                )
                time.sleep(wait)
        raise last_error

    def process_document(self, doc_id, file_path, file_type, kb_id, progress=None, should_abort=None):
        """
        处理文档：预检查 -> 解析文件 -> 文本分块 -> 与现有分块比对 -> 只写入变化的部分
        :param doc_id: 文档ID
        :param file_path: 文件路径
        :param file_type: 文件类型
        :param kb_id: 知识库ID
        :param progress: 可选回调 progress(done, total, stage)，用于上报进度
        :param should_abort: 可选回调，返回True时中止（文档已被删除等）
        :return: {'chunk_count', 'added', 'removed', 'total_stored'}
        """
        self._check_ollama()

        text = self._load_file(file_path, file_type)
        if not text.strip():
            raise ValueError('文档内容为空，无法进行向量化')

        chunks = self.text_splitter.split_text(text)
        if not chunks:
            raise ValueError('文档分块失败')

        total_chunks = len(chunks)
        if progress:
            progress(0, total_chunks, 'parsing')

        file_name = os.path.basename(file_path)
        records = self._build_chunk_records(doc_id, file_name, chunks)
        target = self._target_collection_name(kb_id, doc_id)

        # 比对现有分块：在"遗留 + 全部分片"的并集上做，避免把同一分块重复写入
        stores = self._get_stores(kb_id)
        existing = self._read_existing(doc_id, stores)

        new_ids = {chunk_id: target for chunk_id, _, _ in records}
        added = [chunk_id for chunk_id in new_ids if chunk_id not in existing]
        removed = [chunk_id for chunk_id in existing if chunk_id not in new_ids]
        # 分片数变更后，未变的分块可能落在旧分片里，需要搬到新分片
        moved = [
            chunk_id for chunk_id, owner in existing.items()
            if chunk_id in new_ids and owner != target
        ]

        # 先删后写：删掉消失的与需要搬家的（含遗留collection里的旧方案id）
        stale = {}
        for chunk_id in removed + moved:
            stale.setdefault(existing[chunk_id], []).append(chunk_id)
        self._delete_ids(stale)

        # 只嵌入新增与搬家的分块，未变的一个都不重算
        to_write = set(added) | set(moved)
        pending = [record for record in records if record[0] in to_write]

        done = total_chunks - len(pending)
        for start in range(0, len(pending), self.batch_size):
            if should_abort and should_abort():
                current_app.logger.warning(
                    f'文档{doc_id}在向量化途中已被删除，中止写入'
                )
                raise ValueError('文档已被删除，已中止向量化')

            batch = pending[start:start + self.batch_size]
            self._add_texts_with_retry(
                target,
                texts=[item[1] for item in batch],
                metadatas=[item[2] for item in batch],
                ids=[item[0] for item in batch],
            )
            done += len(batch)
            if progress:
                progress(done, total_chunks, 'embedding')

        # 写入后使检索缓存与答案缓存失效，保证新文档立即可被检索、旧答案不再复用
        self.invalidate(kb_id)

        current_app.logger.info(
            f'文档{doc_id}向量化完成: 共{total_chunks}块, 新增{len(added)}, '
            f'删除{len(removed)}, 迁移{len(moved)}, 目标分片={target}'
        )

        return {
            'chunk_count': total_chunks,
            'added': len(added),
            'removed': len(removed),
            'total_stored': total_chunks
        }

    # ------------------------------------------------------------------
    # 删除与失效
    # ------------------------------------------------------------------

    def delete_document(self, doc_id, kb_id):
        """
        从向量库中删除指定文档的所有分块。
        必须遍历该知识库的全部collection：存量数据在遗留collection里，
        只删分片会让旧分块永远残留并可被检索到。
        :param doc_id: 文档ID
        :param kb_id: 知识库ID
        :return: 删除的分块数量
        """
        existing = self._read_existing(doc_id, self._get_stores(kb_id))
        if existing:
            grouped = {}
            for chunk_id, owner in existing.items():
                grouped.setdefault(owner, []).append(chunk_id)
            self._delete_ids(grouped)

        self.invalidate(kb_id)
        return len(existing)

    def delete_collection(self, kb_id):
        """
        删除该知识库的全部向量数据（遗留 + 全部分片）。

        仅供管理员显式触发的清空操作使用。注意知识库删除本身是逻辑删除
        （kb.status=0），不能接到这里——否则禁用知识库会一并销毁向量数据，
        重新启用时只剩一个doc_count不为0的空库。
        :param kb_id: 知识库ID
        :return: 删除的collection数量
        """
        names = self._existing_names()
        targets = set(self._resolve_collections(kb_id))
        for shard in range(self.shard_count):
            shard_name = self._shard_collection_name(kb_id, shard)
            if shard_name in names:
                targets.add(shard_name)

        deleted = 0
        for name in sorted(targets):
            try:
                self._client.delete_collection(name)
                deleted += 1
            except Exception as e:
                current_app.logger.warning(f'删除collection失败(collection={name}): {e}')

        self.invalidate(kb_id)
        return deleted

    def invalidate(self, kb_id):
        """
        使指定知识库的缓存失效（文档上传/删除后调用）。
        除检索缓存外，还必须清空LLM答案缓存——否则知识库内容已变，
        用户提问仍会命中基于旧内容生成的答案。
        注意：本方法会在向量化后台线程中被调用，purge_kb 使用独立会话自行提交。
        :param kb_id: 知识库ID
        """
        with self._store_lock:
            self._store_cache.pop(kb_id, None)

        try:
            # 局部导入：bm25_service 顶层依赖本模块（要调 iter_chunks 建语料），
            # 模块级互相导入会成环。本方法也是词法索引唯一的失效入口
            from services.bm25_service import get_bm25_service
            get_bm25_service().invalidate(kb_id)
        except Exception as e:
            # 词法索引失效失败不应影响文档处理本身（与下面清理答案缓存同样对待）
            current_app.logger.warning(f'词法索引失效失败(kb_id={kb_id}): {e}')

        try:
            get_cache_service().purge_kb(kb_id)
        except Exception as e:
            # 缓存失效失败不应影响文档处理本身
            current_app.logger.warning(f'清理答案缓存失败(kb_id={kb_id}): {e}')
