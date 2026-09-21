"""
LLM答案缓存服务（两级）

L1：进程内 LRU + TTL，承接高频命中，微秒级返回。
L2：MySQL 表 t_answer_cache，保证重启不丢、多进程共享，并可统计命中次数。

缓存键为 知识库ID + 归一化问题 的哈希。文档增删后由 VectorService.invalidate()
统一清空该知识库的两级缓存，避免答案与知识库内容不一致。

注意：命中率只能按进程统计——入库那次本身算未命中，而被刻意排除不缓存的答案
（兜底文案、空答案）在 L2 里不留行，miss 的分母无法从表里推出来。
"""
import json
import hashlib
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from models import db
from models.answer_cache import AnswerCache


# 单例缓存：与 VectorService/RAGService 保持同一套写法
_cache_service_instance = None
_cache_service_lock = threading.Lock()


def get_cache_service():
    """获取CacheService单例（需在应用上下文内调用）"""
    global _cache_service_instance
    if _cache_service_instance is None:
        with _cache_service_lock:
            if _cache_service_instance is None:
                _cache_service_instance = CacheService()
    return _cache_service_instance


class CacheService:
    """两级LLM答案缓存"""

    def __init__(self):
        """从配置读取缓存参数并初始化L1"""
        cfg = current_app.config
        self.enabled = cfg.get('CACHE_ENABLED', True)
        self.l1_maxsize = max(1, int(cfg.get('CACHE_L1_MAXSIZE', 500)))
        self.l1_ttl = max(1, int(cfg.get('CACHE_L1_TTL', 120)))
        self.l2_ttl = max(1, int(cfg.get('CACHE_L2_TTL', 3600)))

        # L1必须不大于L2：从L2提升进L1时以L2的过期时间为上界。
        # 否则L1会活得比它的来源更久，purge_expired清掉L2行后L1仍在供数。
        if self.l1_ttl > self.l2_ttl:
            current_app.logger.warning(
                f'CACHE_L1_TTL({self.l1_ttl}s) 大于 CACHE_L2_TTL({self.l2_ttl}s)，'
                f'已按L2的值收敛以保证两级过期一致'
            )
            self.l1_ttl = self.l2_ttl

        # L1: key -> (expire_ts, answer, source_docs)，OrderedDict 实现 LRU
        self._l1 = OrderedDict()
        self._l1_lock = threading.Lock()

        # 命中/未命中计数（进程内，重启归零）
        self._counters = {'l1_hit': 0, 'l2_hit': 0, 'miss': 0}
        self._counter_lock = threading.Lock()

        # L2不可用时（例如还没建表）降级为纯内存缓存，并只告警一次，避免刷屏
        self._l2_available = True

    # ------------------------------------------------------------------
    # 键与归一化
    # ------------------------------------------------------------------

    @staticmethod
    def normalize(question):
        """
        归一化问题，让无意义的排版差异共用同一条缓存。
        只做空白折叠，不做同义词/改写——那属于语义缓存，超出本层职责。
        :param question: 原始问题
        :return: 归一化后的问题文本
        """
        return ' '.join((question or '').split())

    @classmethod
    def make_key(cls, kb_id, question):
        """
        生成缓存键：知识库ID + 归一化问题的sha256
        :param kb_id: 知识库ID
        :param question: 用户问题
        :return: 缓存键字符串
        """
        digest = hashlib.sha256(cls.normalize(question).encode('utf-8')).hexdigest()
        return f'{kb_id}:{digest}'

    @staticmethod
    def _question_hash(question):
        """归一化问题的sha256，与L2表 question_hash 列对应"""
        return hashlib.sha256(CacheService.normalize(question).encode('utf-8')).hexdigest()

    def _count(self, name):
        """命中/未命中计数自增"""
        with self._counter_lock:
            self._counters[name] += 1

    # ------------------------------------------------------------------
    # 事务：缓存读写使用独立Session
    # ------------------------------------------------------------------

    def _new_session(self):
        """
        建立独立的数据库会话。
        缓存必须与调用方事务隔离：VectorService.invalidate() 会在向量化后台线程里
        调用本服务，若共用 db.session，这里的 commit 会把worker尚未完成的其他改动一并提交。
        :return: Session实例
        """
        return Session(db.engine)

    def _handle_l2_error(self, exc, action):
        """
        L2不可用时降级为纯内存缓存。
        常见于还没执行 migrate_v3_cache.sql 的情况——此时问答应继续可用，
        而不是每个问题都因为查不到表而报错。
        :param exc: 捕获到的异常
        :param action: 动作描述，用于日志
        :return: True表示已降级
        """
        self._l2_available = False
        current_app.logger.warning(
            f'L2缓存{action}失败，已降级为纯内存缓存（请确认已执行 sql/migrate_v3_cache.sql）: {exc}'
        )
        return True

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get(self, kb_id, question):
        """
        读取缓存
        :param kb_id: 知识库ID
        :param question: 用户问题
        :return: (answer, source_docs) 元组；未命中返回 None
        """
        if not self.enabled:
            return None

        key = self.make_key(kb_id, question)

        # L1
        hit = self._l1_get(key)
        if hit is not None:
            self._count('l1_hit')
            return hit

        # L2
        result = self._l2_get(kb_id, question, key)
        if result is not None:
            self._count('l2_hit')
            return result

        self._count('miss')
        return None

    def _l1_get(self, key):
        """L1查找，命中时把条目移到队尾维持LRU顺序"""
        now = time.time()
        with self._l1_lock:
            entry = self._l1.get(key)
            if entry is None:
                return None
            expire_ts, answer, source_docs = entry
            if expire_ts <= now:
                del self._l1[key]
                return None
            self._l1.move_to_end(key)
            return answer, source_docs

    def _l1_put(self, key, answer, source_docs, expire_ts):
        """写入L1，超出容量时淘汰最久未使用的条目"""
        with self._l1_lock:
            self._l1[key] = (expire_ts, answer, source_docs)
            self._l1.move_to_end(key)
            while len(self._l1) > self.l1_maxsize:
                self._l1.popitem(last=False)

    def _l2_get(self, kb_id, question, key):
        """
        L2查找，命中时累加 hit_count 并提升进L1
        :return: (answer, source_docs) 元组；未命中或已过期返回 None
        """
        if not self._l2_available:
            return None
        try:
            with self._new_session() as session:
                row = session.query(AnswerCache).filter_by(
                    kb_id=kb_id,
                    question_hash=self._question_hash(question)
                ).first()

                if row is None:
                    return None

                now = datetime.now()
                if row.expire_time <= now:
                    # 过期行直接清掉，避免purge_expired没跑到时一直命中一条陈旧数据
                    session.delete(row)
                    session.commit()
                    return None

                row.hit_count = (row.hit_count or 0) + 1
                session.commit()

                answer = row.answer
                source_docs = self._parse_sources(row.source_docs)
                # 提升进L1时以L2的过期时间为上界，保证两级不会不一致
                expire_ts = min(time.time() + self.l1_ttl, row.expire_time.timestamp())
        except SQLAlchemyError as e:
            self._handle_l2_error(e, '读取')
            return None

        self._l1_put(key, answer, source_docs, expire_ts)
        return answer, source_docs

    @staticmethod
    def _parse_sources(raw):
        """解析 source_docs JSON，坏数据按空列表处理"""
        if not raw:
            return []
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def put(self, kb_id, question, answer, source_docs):
        """
        写入缓存（两级）
        :param kb_id: 知识库ID
        :param question: 用户问题
        :param answer: 回答文本
        :param source_docs: 参考来源列表
        :return: 是否写入成功
        """
        if not self.enabled:
            return False
        if not answer or not answer.strip() or not source_docs:
            return False

        now = datetime.now()
        expire_time = now + timedelta(seconds=self.l2_ttl)
        sources_json = json.dumps(source_docs, ensure_ascii=False)
        key = self.make_key(kb_id, question)

        if self._l2_available:
            try:
                with self._new_session() as session:
                    session.add(AnswerCache(
                        kb_id=kb_id,
                        question_hash=self._question_hash(question),
                        question=self.normalize(question)[:500],
                        answer=answer,
                        source_docs=sources_json,
                        hit_count=0,
                        expire_time=expire_time
                    ))
                    session.commit()
            except IntegrityError:
                # 唯一键冲突：并发下有另一个请求刚写了同一条，改为更新其内容并续期
                try:
                    with self._new_session() as session:
                        row = session.query(AnswerCache).filter_by(
                            kb_id=kb_id,
                            question_hash=self._question_hash(question)
                        ).first()
                        if row is not None:
                            row.answer = answer
                            row.source_docs = sources_json
                            row.expire_time = expire_time
                            session.commit()
                except SQLAlchemyError as e:
                    self._handle_l2_error(e, '更新')
            except SQLAlchemyError as e:
                self._handle_l2_error(e, '写入')

        # L1 单独写入：即使L2不可用，进程内缓存依然生效
        self._l1_put(key, answer, source_docs, min(time.time() + self.l1_ttl, expire_time.timestamp()))
        return True

    # ------------------------------------------------------------------
    # 失效
    # ------------------------------------------------------------------

    def purge_kb(self, kb_id):
        """
        清空指定知识库的两级缓存（文档增删后调用）
        注意：L1是进程内缓存，本方法清不掉其他进程持有的那一份，
        只能靠较短的 CACHE_L1_TTL 兜底。
        :param kb_id: 知识库ID
        :return: 清除的L2行数
        """
        prefix = f'{kb_id}:'
        with self._l1_lock:
            stale = [k for k in self._l1 if k.startswith(prefix)]
            for k in stale:
                del self._l1[k]

        if not self._l2_available:
            return 0

        try:
            with self._new_session() as session:
                deleted = session.query(AnswerCache).filter_by(kb_id=kb_id).delete(
                    synchronize_session=False
                )
                session.commit()
                return deleted
        except SQLAlchemyError as e:
            self._handle_l2_error(e, '清理')
            return 0

    def purge_expired(self):
        """
        清理L2中的过期行（由定时任务或启动时调用）
        :return: 清除行数
        """
        if not self._l2_available:
            return 0
        try:
            with self._new_session() as session:
                deleted = session.query(AnswerCache).filter(
                    AnswerCache.expire_time < datetime.now()
                ).delete(synchronize_session=False)
                session.commit()
                return deleted
        except SQLAlchemyError as e:
            self._handle_l2_error(e, '清理过期')
            return 0

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self):
        """
        缓存统计。
        命中率的分母只包含本进程统计到的请求，因此 frontend 上必须标注
        "本进程自启动以来"，否则重启后归零会被误认为缓存失效。
        :return: 统计字典
        """
        with self._counter_lock:
            l1_hit = self._counters['l1_hit']
            l2_hit = self._counters['l2_hit']
            miss = self._counters['miss']

        total = l1_hit + l2_hit + miss
        with self._l1_lock:
            l1_size = len(self._l1)

        return {
            'enabled': self.enabled,
            'l2_available': self._l2_available,
            'l1_hit': l1_hit,
            'l2_hit': l2_hit,
            'miss': miss,
            'total': total,
            'hit_rate': round((l1_hit + l2_hit) / total * 100, 1) if total else 0.0,
            'l1_size': l1_size,
            'l1_maxsize': self.l1_maxsize
        }
