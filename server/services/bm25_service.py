"""
BM25 词法检索服务（混合检索中的稀疏一路）

为什么需要它
------------
向量检索擅长语义相近，但对「OA-2024-017」「3 号流程」这类低频精确串表示很弱——
这类串在嵌入空间里几乎退化成一个点，召回不稳定；而词频检索恰好靠字面命中，
两者互补。故 HybridRetriever 把向量与 BM25 的结果用 RRF 融合。

索引与失效
----------
每个知识库一份内存索引，语料来自 VectorService.iter_chunks()（即该库全部分片
+ 遗留 collection），因此与向量检索看到的是同一批数据。

失效采用**版本号**而非立即重建：invalidate() 只把版本 +1（微秒级），真正的重建
推迟到下次检索。若改成在 invalidate() 里即时重建，批量上传 N 个文档会触发 N 次
全量重建（O(N^2)），而且因为 task_queue 是在 process_document 返回之后才标记
「已向量化」，用户会为这些重建白白多等好几秒。

代价是文档变更后的**第一次**检索要支付一次重建（约1~3秒）。这在单次问答的
总耗时里是最便宜的一环——那条路径上还有改写与重排至少一次 LLM 往返。
"""
import math
import re
import threading

from flask import current_app
from langchain_core.documents import Document

from services.vector_service import CHUNK_PAGE_SIZE, get_vector_service


# BM25 标准参数。本项目的分块长度接近均匀（RecursiveCharacterTextSplitter
# 固定 CHUNK_SIZE=500，只有每篇文档的最后一块不同），因此 b 的作用本就有限，
# 不值得为它调参
K1 = 1.5
B = 0.75

# 切分用：英文数字整词，或一段连续的中文（U+4E00–U+9FFF，CJK统一表意文字）
_TOKEN_RE = re.compile(r'[a-z0-9]+|[一-鿿]+')
_CJK_START, _CJK_END = '一', '鿿'


def _is_cjk(ch):
    """判断是否为CJK统一表意文字"""
    return _CJK_START <= ch <= _CJK_END


def _char_tokenize(text):
    """
    中文按**相邻双字**切分，英文数字按整词切分。

    中文没有词边界，先按二元组切：「带薪年假」会产出 带薪/薪年/年假，查询「年假」
    能以二元组精确命中，对BM25这种词频统计已经够用，因此不必引入 jieba 这层依赖。

    两个刻意的取舍：
    - **不保留单字**。单字里混着大量虚词（与/的/和/在），它们文档频率极高、区分度
      近乎为零，却会让任何问题都"命中"一大批无关文档——实测「量子计算与航天器」
      仅因为一个「与」字就把薪酬制度文档排到了第一位，污染整个候选池。
      代价是单字查询检索不到结果，可以接受：一个字本身就不构成有效的检索线索。
    - **按「连续中文段」切，而不是全文逐字配对**。否则标点两侧的字会被连成
      「金补」这种跨标点的假词元（「五险一金、补充医疗」）。词元是噪音，
      还会稀释真实词元的权重。
    :param text: 原始文本
    :return: 词元列表
    """
    tokens = []
    for match in _TOKEN_RE.finditer((text or '').lower()):
        chunk = match.group(0)
        if _is_cjk(chunk[0]):
            for i in range(len(chunk) - 1):
                tokens.append(chunk[i:i + 2])
        else:
            tokens.append(chunk)
    return tokens


def _jieba_tokenize(text):
    """
    用 jieba 分词（BM25_TOKENIZER=jieba 时启用）。jieba 不是必需依赖。

    这里必须做与 _char_tokenize **同一口径的过滤**，否则切到 jieba 会重新引入
    被内置切分刻意规避掉的问题：
    - 单字照收 → 虚词（与/的/和/在）文档频率极高、区分度近乎为零，
      任何问题都能"命中"一大批无关文档；
    - 标点照收 → jieba 会把「、」「，」切成独立词元，它们同样是高频噪音。
    只丢掉这两类，中文词（≥2字）与英文数字整词一律保留。
    :param text: 原始文本
    :return: 词元列表
    """
    import jieba
    tokens = []
    for word in jieba.cut(text or ''):
        word = word.strip().lower()
        if not word:
            continue
        if _is_cjk(word[0]):
            if len(word) >= 2:
                tokens.append(word)
        elif word[0].isalnum():
            tokens.append(word)
    return tokens


class BM25Index:
    """
    单个知识库的词法索引。

    用倒排表（token -> {文档序号: 词频}）而不是「逐文档算分」：检索时只遍历查询词
    命中的那些文档，代价与命中量成正比，与语料规模无关。
    """

    def __init__(self, docs, tokenized):
        """
        :param docs: Document 列表
        :param tokenized: 与 docs 一一对应的词元列表
        """
        self.docs = docs
        self.n = len(docs)
        self.doc_len = [len(tokens) for tokens in tokenized]
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0

        postings = {}
        for index, tokens in enumerate(tokenized):
            tf = {}
            for token in tokens:
                tf[token] = tf.get(token, 0) + 1
            for token, count in tf.items():
                postings.setdefault(token, {})[index] = count
        self.postings = postings

    def _idf(self, df):
        """
        Lucene 变体 IDF：ln(1 + (N - df + 0.5) / (df + 0.5))。

        刻意不用教科书里的 ln((N - df + 0.5) / (df + 0.5))——那个式子在词出现在
        超过半数文档时会变成负数，导致「命中该词反而扣分」。rank_bm25 是靠
        epsilon * average_idf 兜底的，这里换个恒为正的式子更干净。

        副产品：分数大于0必然意味着词面命中，所以天然没有"零分噪声"需要过滤。
        :param df: 包含该词的文档数
        :return: IDF值（恒为正）
        """
        return math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))

    def search(self, tokens, top_k):
        """
        检索并返回按BM25分数降序的 (Document, 分数) 列表
        :param tokens: 查询词元（内部会去重）
        :param top_k: 返回条数
        :return: [(Document, score), ...]
        """
        if not self.n or not self.avgdl or not tokens:
            return []

        scores = {}
        # 去重后每个查询词只看一遍，即标准的 BM25（不做查询词权重）
        for token in set(tokens):
            posting = self.postings.get(token)
            if not posting:
                continue
            idf = self._idf(len(posting))
            for doc_index, tf in posting.items():
                denom = tf + K1 * (1.0 - B + B * self.doc_len[doc_index] / self.avgdl)
                scores[doc_index] = scores.get(doc_index, 0.0) + idf * tf * (K1 + 1.0) / denom

        if not scores:
            return []

        ranked = sorted(
            (pair for pair in scores.items() if pair[1] > 0),
            key=lambda pair: pair[1],
            reverse=True
        )
        return [(self.docs[i], score) for i, score in ranked[:top_k]]


# 单例缓存：语料索引必须跨请求复用，否则每次问答都要重建整个知识库的词法索引
_bm25_service_instance = None
_bm25_service_lock = threading.Lock()


def get_bm25_service():
    """获取BM25Service单例（需在应用上下文内调用）"""
    global _bm25_service_instance
    if _bm25_service_instance is None:
        with _bm25_service_lock:
            if _bm25_service_instance is None:
                _bm25_service_instance = BM25Service()
    return _bm25_service_instance


class BM25Service:
    """按知识库维护BM25索引的检索服务"""

    def __init__(self):
        """从配置读取参数并初始化缓存与锁"""
        cfg = current_app.config
        self.recall_top_k = cfg.get('RECALL_TOP_K', 20)
        self.page_size = CHUNK_PAGE_SIZE

        # 切分器：默认 char（无依赖、行为可复现）；配成 jieba 且装得上才用 jieba
        mode = (cfg.get('BM25_TOKENIZER') or 'char').lower()
        if mode == 'jieba':
            try:
                import jieba  # noqa: F401  仅探测可用性，实际调用在 _jieba_tokenize 里
                self._tokenize = _jieba_tokenize
                self.tokenizer_name = 'jieba'
            except ImportError:
                current_app.logger.warning(
                    'BM25_TOKENIZER=jieba 但未安装 jieba，已回退为内置的 char 切分'
                    '（pip install jieba 后可启用）'
                )
                self._tokenize = _char_tokenize
                self.tokenizer_name = 'char'
        else:
            self._tokenize = _char_tokenize
            self.tokenizer_name = 'char'

        # kb_id -> (构建时读到的版本号, BM25Index)
        self._cache = {}
        # kb_id -> 版本号，invalidate() 时自增
        self._version = {}
        # 保护 _cache / _version，临界区极短
        self._cache_lock = threading.Lock()
        # 串行化构建：并发冷查询只让一个人真正重建，其余等锁后复用结果
        self._build_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search(self, kb_id, question, top_k=None):
        """
        词法检索。任何异常都只返回空列表——混合检索里少一路不该让问答失败。
        :param kb_id: 知识库ID
        :param question: 用户问题
        :param top_k: 返回条数，默认取配置的 RECALL_TOP_K
        :return: Document列表，按BM25分数降序
        """
        tokens = self._tokenize(question or '')
        if not tokens:
            return []

        try:
            index = self._get_index(kb_id)
            if index is None or not index.n:
                return []
            return [doc for doc, _score in index.search(tokens, top_k or self.recall_top_k)]
        except Exception as e:
            current_app.logger.warning(f'词法检索不可用已跳过(kb_id={kb_id}): {e}')
            return []

    # ------------------------------------------------------------------
    # 索引构建与缓存
    # ------------------------------------------------------------------

    def _get_index(self, kb_id):
        """
        取该知识库的词法索引，必要时重建。

        构建全程**不持有** _cache_lock：否则后台线程的 invalidate() 会被一次
        数秒的分词阻塞，而 invalidate() 就在 process_document 的末尾，
        阻塞它等于把整个上传流程拖慢。
        :param kb_id: 知识库ID
        :return: BM25Index
        """
        with self._cache_lock:
            entry = self._cache.get(kb_id)
            wanted = self._version.get(kb_id, 0)
        if entry is not None and entry[0] == wanted:
            return entry[1]

        with self._build_lock:
            # 双重检查：等锁期间可能已经有别的请求把索引建好了
            with self._cache_lock:
                entry = self._cache.get(kb_id)
                started = self._version.get(kb_id, 0)
            if entry is not None and entry[0] == started:
                return entry[1]

            index = self._build(kb_id)
            with self._cache_lock:
                # 存"构建开始那一刻"的版本号，而不是当前版本：构建期间若发生了
                # invalidate()，存当前版本会让这份陈旧索引自称最新，之后一直被沿用
                self._cache[kb_id] = (started, index)
            current_app.logger.info(
                f'词法索引构建完成(kb_id={kb_id}, 分块数={index.n}, 切分={self.tokenizer_name})'
            )
            return index

    def _build(self, kb_id):
        """
        从向量库拉取该知识库的全部语料并构建索引。
        单个分片读取失败不让整库索引失败——拿到多少用多少。
        :param kb_id: 知识库ID
        :return: BM25Index
        """
        docs = []
        tokenized = []
        seen = set()
        try:
            chunks = get_vector_service().iter_chunks(kb_id, self.page_size)
            for chunk_id, text, metadata in chunks:
                # 必须按 id 去重：iter_chunks 会同时遍历遗留 collection 与分片，
                # 迁移期同一分块在两处都存在。不去重会让语料数N与文档频率df都偏大，
                # 并且同一条内容能占掉两个候选位
                if chunk_id in seen:
                    continue
                seen.add(chunk_id)
                docs.append(Document(page_content=text, metadata=metadata or {}, id=chunk_id))
                tokenized.append(self._tokenize(text))
        except Exception as e:
            current_app.logger.warning(
                f'词法索引语料读取不完整(kb_id={kb_id}, 已取{len(docs)}块): {e}'
            )

        return BM25Index(docs, tokenized)

    def invalidate(self, kb_id):
        """
        标记该知识库的词法索引已过期（文档增删后由 VectorService.invalidate 调用）。
        这里只自增版本号，真正的重建推迟到下次检索——见模块开头的说明。
        :param kb_id: 知识库ID
        """
        with self._cache_lock:
            self._version[kb_id] = self._version.get(kb_id, 0) + 1
            self._cache.pop(kb_id, None)
