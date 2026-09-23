"""
查询改写服务（多路召回中的"多路"来源）

用户提问往往口语化、偏短，措辞与知识库原文对不上，单条查询容易漏召回。
这里让LLM把原问题改写成若干条更适合检索的问句，每条各走一次向量检索，
再由 RRF 把多路结果融合。

两个刻意的取舍
--------------
1. **不是每个问题都值得改写**。改写是一次额外的LLM往返（约1~3秒），而长而具体的
   问题改写收益很低。因此只在问题很短（几乎没有可用检索线索）或带指代词时才触发。
   注意指代词只是次要条件：本系统没有多轮记忆（见 rag_service.SYSTEM_PROMPT），
   指代对象无处可寻，改写只能猜，所以它不该单独把成本拉起来。
2. **只返回改写结果，不负责保留原问题**。原问题由调用方（HybridRetriever）置于
   首位。这样"忘了带上原问题"这类错误在结构上就不可能发生。

改写结果不做缓存：前面的答案缓存已经吸收了重复提问，再叠一层缓存等于多维护一个
失效口径，去覆盖一个已经被覆盖的场景。
"""
import json
import re
import threading

from flask import current_app
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from services.retrieval_llm import build_aux_llm


REWRITE_SYSTEM_PROMPT = """你是一个检索查询改写助手。用户的问题将用于在企业知识库中做检索，请把它改写成若干条更适合检索的问句。

规则：
1. 只输出一个 JSON 数组，元素为字符串，每个元素是一条改写后的问句。
2. 每条改写都要保持原问题的意图，不得改变提问对象，也不得凭空添加原问题中没有的信息。
3. 用更完整、更书面的说法替换口语化表达，并补全原文可能用到的关键词。
4. 除这个 JSON 数组外，不要输出任何其他文字或解释。

示例输出：["员工每年可以享受多少天带薪年假","公司带薪年假的休假天数规定"]
"""

REWRITE_USER_PROMPT = """用户问题：{question}

请改写为最多{count}条问句，输出 JSON 数组："""

# 指代词：出现时说明问题指向不明，值得尝试补全（保守起见只列明确的一批，
# 每多一个误判就多一次LLM往返）
_PRONOUNS = ('这个', '那个', '它们', '它', '上述', '前面提到', '刚才', '这些', '那些', '此项', '该流程')


def _extract_strings(obj):
    """
    从解析出的JSON里提取问句字符串，兼容模型多包一层的写法
    :param obj: json.loads 的结果
    :return: 问句列表
    """
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, list):
        out = []
        for item in obj:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                for key in ('query', 'question', 'text', '改写'):
                    if isinstance(item.get(key), str):
                        out.append(item[key])
                        break
        return out
    if isinstance(obj, dict):
        # {"queries": [...]} / {"questions": [...]} 这类包装。
        # **先按已知的键名找**，找不到再退而取第一个列表值：模型有时会同时给出
        # {"keywords":["年假","带薪"], "queries":["员工每年可休多少天带薪年假"]}，
        # 若只认"第一个列表值"，会把关键词当成改写问句去检索，白花两次嵌入往返、
        # 还给RRF候选池添噪声，而真正想要的改写反被丢掉
        for key in ('queries', 'questions', 'rewritten', '改写', '问句'):
            value = obj.get(key)
            if isinstance(value, list):
                found = _extract_strings(value)
                if found:
                    return found
        for value in obj.values():
            if isinstance(value, list):
                found = _extract_strings(value)
                if found:
                    return found
    return []


def _dedup_key(text):
    """
    去重比较用的键：忽略**全部**空白。
    不能只做空白折叠——「  年假  多少天 」折叠后是「年假 多少天」，与「年假多少天」
    不相等，于是同一句话会被当成一条有效改写，白白多占一路召回。
    中文里的空格本就没有意义，比较时整体去掉才符合直觉。
    :param text: 文本
    :return: 比较键
    """
    return re.sub(r'\s+', '', text or '')


def parse_rewrite_list(raw, question, limit):
    """
    解析改写结果，并过滤掉没有信息量的条目。
    解析失败返回空列表，由调用方只用原问题检索——绝不猜。
    :param raw: 模型原始输出
    :param question: 原问题，用于剔除与原问题重复（或只是排版不同）的改写
    :param limit: 最多保留几条
    :return: 改写后的问句列表
    """
    text = (raw or '').strip()

    items = []
    try:
        items = _extract_strings(json.loads(text))
    except (ValueError, TypeError):
        items = []

    if not items:
        # 取文本里第一个 [...] 片段（兼容 ```json 围栏）
        match = re.search(r'\[[^\[\]]*\]', text)
        if match:
            try:
                items = _extract_strings(json.loads(match.group(0)))
            except (ValueError, TypeError):
                items = []

    result = []
    seen = {_dedup_key(question)}
    for item in items:
        cleaned = ' '.join(str(item).split())
        # 空串、与原问题重复、长到不像问句的，都丢掉
        if not cleaned or len(cleaned) > 100:
            continue
        key = _dedup_key(cleaned)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) >= limit:
            break
    return result


class QueryRewriteService:
    """把短问题改写成多条检索问句"""

    def __init__(self):
        """从配置读取参数并构建LLM链"""
        cfg = current_app.config
        # 兜底值与 config.QUERY_REWRITE_MAX_CHARS 保持一致：两处不一致时，
        # 少配一个环境变量就会得到一个与文档不符的触发阈值
        self.max_chars = max(1, cfg.get('QUERY_REWRITE_MAX_CHARS', 8))
        self.count = max(1, cfg.get('QUERY_REWRITE_COUNT', 2))
        llm = build_aux_llm(num_predict=256)
        prompt = ChatPromptTemplate.from_messages([
            ('system', REWRITE_SYSTEM_PROMPT),
            ('human', REWRITE_USER_PROMPT)
        ])
        self.chain = prompt | llm | StrOutputParser()

    def should_rewrite(self, question):
        """
        判断是否值得为这个问题付一次LLM调用。
        短问题几乎没有可用检索线索，是主要触发条件；含指代词是次要条件。
        :param question: 用户问题
        :return: 是否需要改写
        """
        text = ' '.join((question or '').split())
        if not text:
            return False
        if len(text) <= self.max_chars:
            return True
        return any(word in text for word in _PRONOUNS)

    def rewrite(self, question):
        """
        生成改写问句。**永不抛异常**：不触发或失败时返回空列表，
        调用方只用原问题检索即可。
        :param question: 用户问题
        :return: 改写后的问句列表（不含原问题）
        """
        if not self.should_rewrite(question):
            return []

        try:
            raw = self.chain.invoke({'question': question, 'count': self.count})
        except Exception as e:
            current_app.logger.warning(f'查询改写失败，仅用原问题检索: {e}')
            return []

        rewritten = parse_rewrite_list(raw, question, self.count)
        if rewritten:
            current_app.logger.info(
                f'查询改写生效({len(rewritten)}条): {question} -> {rewritten}'
            )
        return rewritten


# 单例缓存：与其余服务保持同一套写法
_rewrite_service_instance = None
_rewrite_service_lock = threading.Lock()


def get_query_rewrite_service():
    """获取QueryRewriteService单例（需在应用上下文内调用）"""
    global _rewrite_service_instance
    if _rewrite_service_instance is None:
        with _rewrite_service_lock:
            if _rewrite_service_instance is None:
                _rewrite_service_instance = QueryRewriteService()
    return _rewrite_service_instance
