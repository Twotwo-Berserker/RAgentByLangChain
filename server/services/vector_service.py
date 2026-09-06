"""
文档向量化服务
负责文档解析、文本分块和Chroma向量存储
"""
import os
import time
from flask import current_app
from ollama import Client as OllamaClient, ResponseError
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_ollama import OllamaEmbeddings
from langchain_chroma import Chroma


class OllamaServiceError(Exception):
    """Ollama服务相关异常，用于提供更清晰的错误提示"""
    pass


class VectorService:
    """文档向量化服务类"""

    def __init__(self):
        """初始化嵌入模型和文本分割器"""
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

    def _get_collection_name(self, kb_id):
        """
        根据知识库ID生成Chroma集合名称
        每个知识库使用独立的collection进行隔离
        """
        return f"kb_{kb_id}"

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

    def _add_texts_with_retry(self, vectorstore, texts, metadatas, ids):
        """
        带重试的向量写入，处理Ollama瞬时故障(502/503/504等)
        :param vectorstore: Chroma向量库实例
        :param texts: 文本分块列表
        :param metadatas: 元数据列表
        :param ids: ID列表
        """
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

    def process_document(self, doc_id, file_path, file_type, kb_id):
        """
        处理文档：预检查 -> 解析文件 -> 文本分块 -> 分批存入向量库
        :param doc_id: 文档ID
        :param file_path: 文件路径
        :param file_type: 文件类型
        :param kb_id: 知识库ID
        :return: 分块数量
        """
        self._check_ollama()

        text = self._load_file(file_path, file_type)
        if not text.strip():
            raise ValueError('文档内容为空，无法进行向量化')

        chunks = self.text_splitter.split_text(text)
        if not chunks:
            raise ValueError('文档分块失败')

        file_name = os.path.basename(file_path)
        metadatas = [{'doc_id': doc_id, 'file_name': file_name, 'chunk_index': i} for i in range(len(chunks))]
        ids = [f"doc_{doc_id}_chunk_{i}" for i in range(len(chunks))]

        collection_name = self._get_collection_name(kb_id)
        vectorstore = Chroma(
            collection_name=collection_name,
            embedding_function=self.embeddings,
            persist_directory=self.persist_dir
        )

        # 分批写入，降低单次Ollama嵌入请求的压力
        for i in range(0, len(chunks), self.batch_size):
            batch_end = min(i + self.batch_size, len(chunks))
            self._add_texts_with_retry(
                vectorstore,
                texts=chunks[i:batch_end],
                metadatas=metadatas[i:batch_end],
                ids=ids[i:batch_end],
            )

        return len(chunks)

    def delete_document(self, doc_id, kb_id):
        """
        从向量库中删除指定文档的所有分块
        :param doc_id: 文档ID
        :param kb_id: 知识库ID
        """
        collection_name = self._get_collection_name(kb_id)
        vectorstore = Chroma(
            collection_name=collection_name,
            embedding_function=self.embeddings,
            persist_directory=self.persist_dir
        )
        # 根据文档ID过滤并删除
        vectorstore._collection.delete(where={'doc_id': doc_id})

    def get_retriever(self, kb_id):
        """
        获取指定知识库的检索器
        :param kb_id: 知识库ID
        :return: Chroma检索器
        """
        collection_name = self._get_collection_name(kb_id)
        vectorstore = Chroma(
            collection_name=collection_name,
            embedding_function=self.embeddings,
            persist_directory=self.persist_dir
        )
        return vectorstore.as_retriever(
            search_kwargs={'k': current_app.config['RETRIEVER_TOP_K']}
        )
