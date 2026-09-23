"""
检索阶段辅助LLM客户端工厂与输出解析

重排与查询改写都需要调用LLM，但它们的诉求和"生成答案"完全不同，因此刻意不复用
rag_service 里那个客户端。三处关键差异：

1. format='json' —— 让 Ollama 把解码约束在合法JSON上，基本消除"模型先写一段
   解释再给结果"这种最常见的失败模式（已确认 langchain_ollama 0.3.0 的
   ChatOllama 支持该参数，且它是由 kwargs.pop("format", self.format) 读取的）；
2. temperature=0 —— 这两个任务要的是确定性的判断，不是文采；
3. 超时取 RETRIEVAL_LLM_TIMEOUT（默认60秒）。这两个调用就落在HTTP请求路径上，
   Ollama卡死时绝不能把请求一直挂住。
   **必须走 client_kwargs 传**：ChatOllama 没有声明 timeout 字段，而它的
   model_config 是 extra='ignore'，所以 `ChatOllama(timeout=60)` 会被 pydantic
   静默丢弃（实测得到的 httpx 超时是 Timeout(timeout=None)，即永不超时），
   只有 client_kwargs 才会被 `Client(host=..., **client_kwargs)` 真正透传下去。

另外 num_predict 压得很小：它们的输出只是一个数组，给多了只会让模型啰嗦。
"""
import json
import re

from flask import current_app
from langchain_ollama import ChatOllama


def build_aux_llm(model=None, num_predict=128):
    """
    构造用于检索辅助任务（重排、查询改写）的聊天客户端
    :param model: 模型名，留空则用 OLLAMA_LLM_MODEL（同模型不会触发Ollama重新加载）
    :param num_predict: 单次生成长度上限
    :return: 已绑定参数的 ChatOllama 实例
    """
    cfg = current_app.config
    llm = ChatOllama(
        model=model or cfg['OLLAMA_LLM_MODEL'],
        base_url=cfg['OLLAMA_BASE_URL'],
        temperature=0,
        # 与生成答案共用 LLM_NUM_CTX：重排提示词的长度由
        # RERANK_TOP_N × RERANK_CANDIDATE_CHARS 决定，必须落在这个窗口内
        num_ctx=cfg.get('LLM_NUM_CTX', 8192),
        num_predict=num_predict,
        format='json',
        # 只能通过 client_kwargs 传超时，见模块开头的说明
        client_kwargs={'timeout': cfg.get('RETRIEVAL_LLM_TIMEOUT', 60)}
    )
    # think 三态与全局配置一致：None（对应 LLM_THINK=auto）表示完全不发送该参数，
    # 用于换成不支持思考的模型时退避；其余情况一律强制关闭——辅助任务开着思考
    # 既慢，又容易把JSON挤没
    return llm if cfg.get('LLM_THINK') is None else llm.bind(think=False)


def extract_first_json_array(text):
    """
    从模型输出里取出第一个 JSON 数组。

    虽然已经用 format='json' 约束了解码，但模型仍可能把数组包一层
    （{"queries":[...]}）或用 ```json 围栏包起来，所以重排与查询改写两个解析器
    都需要这一步。抽成公用函数是为了让两处保持一致——否则修了一个忘了另一个，
    两个解析器的行为就会悄悄分叉。

    只处理**不含嵌套数组**的情形（正则匹配到第一个 ] 就停）：
    本项目的输出结构里没有嵌套，真要支持得换成正则以外的括号配平扫描。
    :param text: 模型原始输出
    :return: 解析出的列表；取不到返回 None
    """
    if not text:
        return None

    # 先试整体解析：format='json' 生效时这是最常见的情况
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, list):
        return obj

    match = re.search(r'\[[^\[\]]*\]', text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, list) else None

