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
from services.cache_service import get_cache_service


# RAG系统提示词模板
SYSTEM_PROMPT = """你是一个企业内部知识库智能问答助手。请根据以下提供的参考资料来回答用户的问题。

要求：
1. 仅根据参考资料中的内容来回答问题，不要编造信息
2. 如果参考资料中没有相关信息，请如实告知用户
3. 回答要准确、简洁、专业
4. 使用中文回答
5. 必须标注引用来源：凡是依据某条参考资料得出的结论，都要在该句末尾加上来源编号，
   格式为 [来源N]（N为参考资料序号，例如 [来源1]）。没有依据参考资料的句子不要标注。
   示例：员工每年享有5天带薪年假[来源1]，入职满3年后增加至10天[来源2]。
6. 本系统只依据当前检索到的参考资料作答，不具备对话记忆：当用户询问
   "我刚刚问了什么问题""前面说过什么"这类与知识库无关的问题时，直接说明这一点，
   不要猜测，也不要给出空回答。

参考资料：
{context}
"""

# 用户提问模板
USER_PROMPT = "{question}"

# 未检索到相关内容时的提示
NO_RESULT_ANSWER = '抱歉，在知识库中未找到与您问题相关的内容，请尝试换个方式提问。'

# 模型返回空回答时的兜底文案
# 思考型模型偶发"思考结束但正文为空"，不兜底的话界面会留下一个空白气泡，
# 数据库里也会存下一条空答案
EMPTY_ANSWER_FALLBACK = '抱歉，本次未能生成有效回答，请尝试重新提问或换个说法。'


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
        model = ChatOllama(
            model=current_app.config['OLLAMA_LLM_MODEL'],
            base_url=current_app.config['OLLAMA_BASE_URL'],
            temperature=0.3,
            num_ctx=current_app.config['LLM_NUM_CTX'],
            num_predict=current_app.config['LLM_NUM_PREDICT'],
            timeout=3600
        )
        # think 透传给 Ollama：关闭思考后同一个问题从 203s 降到 6s，
        # 也不再出现"思考吃掉全部预算、正文为空"的空白回答。
        # 配置为 None 时不发送该参数，兼容不支持思考的模型（见 config.LLM_THINK）
        think = current_app.config.get('LLM_THINK')
        self.llm = model.bind(think=think) if think is not None else model
        self.vector_service = get_vector_service()
        self.cache = get_cache_service()

        # 提示词模板只构建一次
        self.prompt = ChatPromptTemplate.from_messages([
            ('system', SYSTEM_PROMPT),
            ('human', USER_PROMPT)
        ])

    def _format_docs(self, docs):
        """
        将检索到的文档格式化为上下文文本。
        编号格式与提示词中的引用标记 [来源N] 保持一致，便于前端溯源。
        :param docs: 检索到的文档列表
        :return: 格式化后的文本
        """
        formatted = []
        for i, doc in enumerate(docs, 1):
            source = doc.metadata.get('file_name', '未知来源')
            formatted.append(f"[来源{i}] 文件：{source}\n{doc.page_content}")
        return '\n\n'.join(formatted)

    def _extract_source_docs(self, docs):
        """
        提取参考文档来源信息。
        index 对应提示词中的 [来源N]，content 保留完整分块内容，
        供前端在原文中定位并高亮被引用的片段。
        :param docs: 检索到的文档列表
        :return: 来源信息列表
        """
        sources = []
        for i, doc in enumerate(docs, 1):
            sources.append({
                'index': i,
                'file_name': doc.metadata.get('file_name', '未知'),
                'doc_id': doc.metadata.get('doc_id'),
                'chunk_index': doc.metadata.get('chunk_index'),
                'content': doc.page_content
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
        向量检索相关文档（跨分片检索并归并）
        :param question: 用户问题
        :param kb_id: 知识库ID
        :return: 检索到的文档列表
        """
        return self.vector_service.search(kb_id, question)

    def _cache_put(self, kb_id, question, answer, source_docs):
        """
        写入答案缓存。
        兜底文案不代表知识库里的确定答案，缓存它们只会让"知识库没有这条信息"
        这个结论被固化下来，因此一律不缓存。
        :return: 是否写入成功
        """
        if answer in (NO_RESULT_ANSWER, EMPTY_ANSWER_FALLBACK):
            return False
        return self.cache.put(kb_id, question, answer, source_docs)

    def ask(self, question, kb_id):
        """
        RAG问答主方法（非流式）
        流程: 查缓存 -> (未命中) 向量检索 -> 构建上下文 -> LLM生成回答 -> 回写缓存
        :param question: 用户问题
        :param kb_id: 知识库ID
        :return: {'answer': 回答文本, 'source_docs': 来源列表, 'from_cache': 是否命中缓存}
        """
        cached = self.cache.get(kb_id, question)
        if cached is not None:
            answer, source_docs = cached
            return {'answer': answer, 'source_docs': source_docs, 'from_cache': True}

        docs = self.retrieve(question, kb_id)

        if not docs:
            return {'answer': NO_RESULT_ANSWER, 'source_docs': [], 'from_cache': False}

        answer = self._build_chain(docs).invoke(question)
        answer = self._guard_empty_answer(answer, question, kb_id)
        source_docs = self._extract_source_docs(docs)

        self._cache_put(kb_id, question, answer, source_docs)
        return {'answer': answer, 'source_docs': source_docs, 'from_cache': False}

    def _guard_empty_answer(self, answer, question, kb_id):
        """
        空回答兜底：模型返回空白时替换为提示文案并记录日志。
        空白回答既不该展示给用户，也不该落库，否则界面上是个空气泡、历史里是条空记录。
        :param answer: 模型返回的回答
        :param question: 用户问题（仅用于日志）
        :param kb_id: 知识库ID（仅用于日志）
        :return: 兜底后的回答
        """
        if answer and answer.strip():
            return answer

        current_app.logger.warning(
            f'模型返回空回答，已替换为兜底文案（kb_id={kb_id}, question={question[:50]}）'
        )
        return EMPTY_ANSWER_FALLBACK

    def stream(self, question, kb_id):
        """
        RAG流式问答，逐个产出事件字典：
        - {'type': 'cache', 'data': True}     命中缓存（仅内部使用，不转发给前端）
        - {'type': 'sources', 'data': [...]}  参考来源（首条）
        - {'type': 'token', 'data': '...'}    LLM生成片段
        :param question: 用户问题
        :param kb_id: 知识库ID
        """
        cached = self.cache.get(kb_id, question)
        if cached is not None:
            answer, source_docs = cached
            yield {'type': 'cache', 'data': True}
            yield {'type': 'sources', 'data': source_docs}
            yield {'type': 'token', 'data': answer}
            return

        docs = self.retrieve(question, kb_id)

        if not docs:
            yield {'type': 'sources', 'data': []}
            yield {'type': 'token', 'data': NO_RESULT_ANSWER}
            return

        source_docs = self._extract_source_docs(docs)
        yield {'type': 'sources', 'data': source_docs}

        answer_parts = []
        for chunk in self._build_chain(docs).stream(question):
            answer_parts.append(chunk)
            yield {'type': 'token', 'data': chunk}

        # 流式没有产出任何正文时补一条兜底文案，避免前端留下空白气泡
        answer = ''.join(answer_parts)
        fallback = self._guard_empty_answer(answer, question, kb_id)
        if fallback != answer:
            yield {'type': 'token', 'data': fallback}
            answer = fallback

        # 生成完成后回写缓存。放在这里而不是开头：中途出错或客户端断开时不应留下半截答案
        self._cache_put(kb_id, question, answer, source_docs)
