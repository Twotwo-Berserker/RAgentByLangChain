"""
RAG问答核心服务
基于LangChain构建检索增强生成（RAG）问答链
使用Ollama的qwen3.5:9b作为大语言模型
"""
import threading
from flask import current_app
from langchain_ollama import ChatOllama
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough
from services.vector_service import get_vector_service


# RAG系统提示词模板
SYSTEM_PROMPT = """你是一个企业内部知识库智能问答助手。请根据以下提供的参考资料来回答用户的问题。

要求：
1. 仅根据参考资料中的内容来回答问题，不要编造信息
2. 如果参考资料中没有相关信息，请如实告知用户
3. 回答要准确、简洁、专业
4. 使用中文回答

参考资料：
{context}
"""

# 用户提问模板
USER_PROMPT = "{question}"

# 未检索到相关内容时的提示
NO_RESULT_ANSWER = '抱歉，在知识库中未找到与您问题相关的内容，请尝试换个方式提问。'


# 单例缓存：避免每次请求都重新初始化LLM/向量服务
_rag_service_instance = None
_rag_service_lock = threading.Lock()


def get_rag_service():
    """
    获取RAGService单例。
    每次问答请求都重新构造 RAGService 会重复创建 ChatOllama 客户端、
    加载嵌入模型配置并重建 Chroma 连接，带来不必要的延迟。这里做进程级单例复用。
    """
    global _rag_service_instance
    if _rag_service_instance is None:
        with _rag_service_lock:
            if _rag_service_instance is None:
                _rag_service_instance = RAGService()
    return _rag_service_instance


class RAGService:
    """RAG问答服务类"""

    def __init__(self):
        """初始化LLM模型和向量服务"""
        self.llm = ChatOllama(
            model=current_app.config['OLLAMA_LLM_MODEL'],
            base_url=current_app.config['OLLAMA_BASE_URL'],
            temperature=0.3,
            timeout=3600
        )
        self.vector_service = get_vector_service()

        # 提示词模板只构建一次
        self.prompt = ChatPromptTemplate.from_messages([
            ('system', SYSTEM_PROMPT),
            ('human', USER_PROMPT)
        ])

    def _format_docs(self, docs):
        """
        将检索到的文档格式化为上下文文本
        :param docs: 检索到的文档列表
        :return: 格式化后的文本
        """
        formatted = []
        for i, doc in enumerate(docs, 1):
            source = doc.metadata.get('file_name', '未知来源')
            formatted.append(f"[来源{i}: {source}]\n{doc.page_content}")
        return '\n\n'.join(formatted)

    def _extract_source_docs(self, docs):
        """
        提取参考文档来源信息
        :param docs: 检索到的文档列表
        :return: 来源信息列表
        """
        sources = []
        seen = set()
        for doc in docs:
            file_name = doc.metadata.get('file_name', '未知')
            if file_name not in seen:
                seen.add(file_name)
                sources.append({
                    'file_name': file_name,
                    'content': doc.page_content[:200]
                })
        return sources

    def _build_chain(self, docs):
        """
        构建RAG链：检索 -> 格式化上下文 -> 提示词 -> LLM -> 解析输出
        """
        return (
            {
                'context': lambda x: self._format_docs(docs),
                'question': RunnablePassthrough()
            }
            | self.prompt
            | self.llm
            | StrOutputParser()
        )

    def retrieve(self, question, kb_id):
        """
        向量检索相关文档
        :param question: 用户问题
        :param kb_id: 知识库ID
        :return: 检索到的文档列表
        """
        retriever = self.vector_service.get_retriever(kb_id)
        return retriever.invoke(question)

    def ask(self, question, kb_id):
        """
        RAG问答主方法（非流式）
        流程: 用户提问 -> 向量检索 -> 构建上下文 -> LLM生成回答
        :param question: 用户问题
        :param kb_id: 知识库ID
        :return: (回答文本, 参考来源列表)
        """
        docs = self.retrieve(question, kb_id)

        if not docs:
            return NO_RESULT_ANSWER, []

        answer = self._build_chain(docs).invoke(question)
        source_docs = self._extract_source_docs(docs)

        return answer, source_docs

    def stream(self, question, kb_id):
        """
        RAG流式问答，逐个产出事件字典：
        - {'type': 'sources', 'data': [...]}  参考来源（首条）
        - {'type': 'token', 'data': '...'}    LLM生成片段
        :param question: 用户问题
        :param kb_id: 知识库ID
        """
        docs = self.retrieve(question, kb_id)

        if not docs:
            yield {'type': 'sources', 'data': []}
            yield {'type': 'token', 'data': NO_RESULT_ANSWER}
            return

        yield {'type': 'sources', 'data': self._extract_source_docs(docs)}

        for chunk in self._build_chain(docs).stream(question):
            yield {'type': 'token', 'data': chunk}
