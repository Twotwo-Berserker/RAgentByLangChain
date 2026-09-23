"""
混合检索编排：多路召回 → RRF 融合 → 重排

链路
----
1. **查询改写**（可选）：短问题由LLM改写成若干条问句；
2. **多路召回**：原问题 + 每条改写各走一次向量检索（稠密），原问题再走一次BM25（稀疏）；
   N 条改写就是 N+1 路向量 + 1 路词法；
3. **RRF 融合**：按名次融合成一个候选池；
4. **重排**：用重排器看着**用户原问题**逐条判断相关性，取前 RETRIEVER_TOP_K 返回。

为什么词法只跑原问题
--------------------
改写句按定义换了词，而这恰恰是词法匹配最不擅长的——让一个近乎随机的列表参与
RRF投票，只会把它任意的前20名抬进候选池。向量检索才是改写该发挥作用的地方。

为什么融合用名次而不是分数
--------------------------
向量返回的是 Chroma 距离（越小越好），BM25 返回的是相似度（越大越好），两者量纲
不可比，做归一化的口径怎么定都站不住。RRF 只吃名次，天然免疫这个问题，
代价是丢掉了"差距有多大"的信息——精确性由重排环节补回来。
分工很清晰：**RRF 保守地最大化召回，重排负责精确**。

降级策略
--------
每一路失败只跳过该路，绝不因此让问答报错。但**向量召回全部失败、且融合池为空**
时会重新抛出，交给 rag_service 回退到纯向量检索：否则"检索全挂"会伪装成
"知识库没有这条信息"，把一次故障固化成一条假阴性答案。

判定条件为什么是"向量全挂 **且** 池子为空"：
- 只看"所有路都失败"是不成立的。BM25Service.search 内部把异常吞成空列表，
  它**永远不会抛**，所以只要词法那一路在列表里，"全部失败"就不可达。
- 只看"向量全挂"又太急。改写/原问题三路向量都失败但词法召回到了候选时，
  仍然有一次可用的检索结果，此时抛异常回退到纯向量只会**更糟**
  （纯向量正是刚刚全体失败的那条路），正确做法是用这份词法结果继续问答。
- 两者同时成立，才真的无候选可用，此时抛出去让上层兜底并留下告警。
"""
import hashlib
import threading

from flask import current_app

from services.vector_service import get_vector_service
from services.bm25_service import get_bm25_service
from services.reranker_service import get_reranker
from services.query_rewrite_service import get_query_rewrite_service


# 单例缓存：内部持有多个服务单例与LLM客户端，不能每次问答重建
_hybrid_retriever_instance = None
_hybrid_retriever_lock = threading.Lock()


def get_hybrid_retriever():
    """获取HybridRetriever单例（需在应用上下文内调用）"""
    global _hybrid_retriever_instance
    if _hybrid_retriever_instance is None:
        with _hybrid_retriever_lock:
            if _hybrid_retriever_instance is None:
                _hybrid_retriever_instance = HybridRetriever()
    return _hybrid_retriever_instance


class HybridRetriever:
    """混合检索编排器"""

    def __init__(self):
        """从配置读取参数并装配各子服务"""
        cfg = current_app.config
        self.recall_top_k = cfg.get('RECALL_TOP_K', 20)
        self.final_top_k = cfg.get('RETRIEVER_TOP_K', 4)
        self.rrf_k = cfg.get('RRF_K', 60)
        self.rerank_top_n = cfg.get('RERANK_TOP_N', 12)

        self.vector_service = get_vector_service()
        self.bm25_service = get_bm25_service()
        self.reranker = get_reranker()
        self.rewriter = (
            get_query_rewrite_service()
            if cfg.get('QUERY_REWRITE_ENABLED', True) else None
        )

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def retrieve(self, question, kb_id):
        """
        混合检索，返回按相关程度降序的文档列表
        :param question: 用户问题
        :param kb_id: 知识库ID
        :return: Document列表，最多 RETRIEVER_TOP_K 条
        """
        queries = [question]
        if self.rewriter is not None:
            queries.extend(self.rewriter.rewrite(question))

        lists = []
        vector_attempted = 0
        vector_failed = 0

        # 向量召回：原问题与每条改写各一路
        for query in queries:
            vector_attempted += 1
            try:
                lists.append(
                    self.vector_service.search(kb_id, query, top_k=self.recall_top_k)
                )
            except Exception as e:
                vector_failed += 1
                current_app.logger.warning(
                    f'向量召回失败已跳过(kb_id={kb_id}, query={query[:30]}): {e}'
                )

        # 词法召回：只在原问题上跑（见模块开头的说明）
        try:
            lists.append(
                self.bm25_service.search(kb_id, question, top_k=self.recall_top_k)
            )
        except Exception as e:
            current_app.logger.warning(f'词法召回失败已跳过(kb_id={kb_id}): {e}')

        fused = self._rrf_fuse(lists)
        if not fused:
            # 向量全挂且词法也没捞到东西，才是真的无候选可用。抛给 rag_service 兜底，
            # 别让它伪装成"知识库没有这条信息"（见模块开头的降级策略）
            if vector_failed == vector_attempted:
                raise RuntimeError('向量召回全部失败且无词法候选可用')
            return []

        # 候选数不超过最终返回条数时跳过重排：集合完全相同，重排只能改变顺序，
        # 不值得为此多付一次LLM调用（小知识库上这能让重排成本归零）
        candidates = fused[:self.rerank_top_n]
        if len(candidates) > self.final_top_k:
            try:
                candidates = self.reranker.rerank(question, candidates)
            except Exception as e:
                # 重排器实现方本应自行兜底，这里再兜一层，确保问答不因重排失败而中断
                current_app.logger.warning(f'重排异常，沿用融合顺序: {e}')

        return candidates[:self.final_top_k]

    # ------------------------------------------------------------------
    # RRF 融合
    # ------------------------------------------------------------------

    def _rrf_fuse(self, lists):
        """
        Reciprocal Rank Fusion：score(d) = Σ 1 / (k + rank(d))

        :param lists: 多个已排序的文档列表
        :return: 融合后的文档列表，按分数降序
        """
        scores = {}
        best_rank = {}
        first_seen = {}

        for docs in lists:
            # 同一列表内理论上不会有重复id，但迁移期同一分块可能同时存在于遗留
            # collection与分片里而被同一路召回——按最小名次只计一次，避免重复计分
            ranks = {}
            for rank, doc in enumerate(docs, 1):
                doc_id = self._doc_key(doc)
                if doc_id not in ranks:
                    ranks[doc_id] = rank

            for doc_id, rank in ranks.items():
                scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (self.rrf_k + rank)
                if rank < best_rank.get(doc_id, float('inf')):
                    best_rank[doc_id] = rank
                first_seen.setdefault(doc_id, docs[rank - 1])

        # 同分时按(最好名次, id)兜底排序，保证输出确定、[来源N]编号稳定
        ordered = sorted(scores, key=lambda d: (-scores[d], best_rank[d], d))
        return [first_seen[doc_id] for doc_id in ordered]

    @staticmethod
    def _doc_key(doc):
        """
        融合用的文档标识。优先用分块id（向量与词法返回的是同一套稳定内容哈希），
        这是两路结果能对齐去重的前提。
        :param doc: Document实例
        :return: 标识字符串
        """
        if doc.id:
            return doc.id
        # 理论上不会走到这里。真出现了也不能静默丢弃，退化为按内容取键
        digest = hashlib.sha1((doc.page_content or '').encode('utf-8')).hexdigest()[:16]
        return f'__content__{digest}'
