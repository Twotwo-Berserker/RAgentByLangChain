"""
项目配置文件
包含数据库、Ollama、Chroma等配置信息
"""
import os

from dotenv import load_dotenv


# 提前加载 server/.env（该文件已被 .gitignore 忽略）：本地开发把 SECRET_KEY 等放在这里，
# 部署时改用真实环境变量。不传 override=True，让已存在的环境变量优先于 .env 文件。
# 必须放在 class Config 之前——Config 的类体在模块导入时求值，晚于此处就会读到空值。
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))


def _require_secret_key():
    """
    读取并校验 SECRET_KEY
    缺失或过短直接抛异常。宁可启动失败，也不能退回一个写死在源码里的默认密钥——
    JWT的签名密钥一旦可猜，任何人都能自行签发任意 user_id/role 的token冒充管理员。
    :return: 校验通过的密钥
    """
    key = (os.environ.get('SECRET_KEY') or '').strip()
    # HS256的密钥长度应与摘要输出一致，即256位（32字节）
    if len(key) < 32:
        raise RuntimeError(
            'SECRET_KEY 未配置或长度不足32位，拒绝启动。\n'
            '请复制 server/.env.example 为 server/.env 并填入随机串，或通过环境变量注入。\n'
            '生成方式：python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    return key


def _parse_think(value):
    """
    解析 LLM_THINK 配置（三态）
    :param value: 环境变量原始值
    :return: True-开启思考；False-关闭思考；None-不向Ollama发送该参数
    """
    v = (value or '').strip().lower()
    # 'auto' 用于换成不支持思考的模型时退避：完全不发送 think 参数
    if v in ('auto', 'none', ''):
        return None
    return v in ('1', 'true', 'yes', 'on')


def _env_bool(name, default):
    """
    读取布尔型环境变量
    :param name: 环境变量名
    :param default: 默认值
    :return: 解析后的布尔值
    """
    return os.environ.get(name, str(default)).strip().lower() in ('1', 'true', 'yes', 'on')


def _env_int(name, default):
    """
    读取整型环境变量。值非法时回落到默认值而不是抛异常——
    一个拼错的数字不该导致服务起不来。
    :param name: 环境变量名
    :param default: 默认值
    :return: 解析后的整数
    """
    raw = (os.environ.get(name) or '').strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Config:
    """基础配置类"""

    # Flask密钥，用于JWT签名。必填，无默认值（见 _require_secret_key）
    SECRET_KEY = _require_secret_key()

    # MySQL数据库配置（端口3306，密码123456）
    MYSQL_HOST = os.environ.get('MYSQL_HOST', '127.0.0.1')
    MYSQL_PORT = int(os.environ.get('MYSQL_PORT', 3306))
    MYSQL_USER = os.environ.get('MYSQL_USER', 'root')
    MYSQL_PASSWORD = os.environ.get('MYSQL_PASSWORD', '123456')
    MYSQL_DATABASE = os.environ.get('MYSQL_DATABASE', 'db_enterprise_qa')

    # SQLAlchemy数据库连接URI
    SQLALCHEMY_DATABASE_URI = (
        f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
        f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}?charset=utf8mb4"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # JWT Token有效期（秒），默认24小时
    JWT_EXPIRATION = 86400

    # Ollama配置
    OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', 'http://localhost:11434')
    OLLAMA_LLM_MODEL = os.environ.get('OLLAMA_LLM_MODEL', 'qwen3.5:9b')
    OLLAMA_EMBED_MODEL = os.environ.get('OLLAMA_EMBED_MODEL', 'qwen3-embedding:4b')

    # ChromaDB持久化存储路径
    CHROMA_PERSIST_DIR = os.environ.get(
        'CHROMA_PERSIST_DIR',
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'chroma_data')
    )

    # 文件上传配置
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 最大上传文件大小：50MB
    ALLOWED_EXTENSIONS = {'txt', 'pdf', 'md', 'docx'}

    # 文档分块配置
    CHUNK_SIZE = 500        # 每个分块的字符数
    CHUNK_OVERLAP = 50      # 分块之间的重叠字符数

    # 向量化批处理配置
    EMBED_BATCH_SIZE = 10   # 每批发送给Ollama的分块数量
    EMBED_MAX_RETRIES = 3   # 嵌入失败最大重试次数

    # RAG检索配置
    RETRIEVER_TOP_K = 4     # 检索返回的相似文档数量（分片内 k 与全局 k 都用它，见 services/vector_service.search）

    # ------------------------------------------------------------------
    # 混合检索 / 多路召回 / 重排序
    #
    # 检索链路：查询改写 → 多路召回（向量 × N + BM25 × 1）→ RRF 融合 → 重排 → 取 RETRIEVER_TOP_K
    # 最终返回条数仍是 RETRIEVER_TOP_K，因此提示词长度与生成阶段的开销结构不变。
    #
    # 延迟提醒：重排与改写各是一次额外的 LLM 调用，是本项目新增的主要成本。
    # 三个 *_ENABLED 开关必须保持独立可用——现场演示卡顿时按需关掉即可降级，
    # 每个环节失败也都只降级、不报错（见 services/hybrid_retriever.py）。
    # ------------------------------------------------------------------

    # 混合检索总开关。关掉即退回改造前的纯向量检索，行为与旧版完全一致
    HYBRID_ENABLED = _env_bool('HYBRID_ENABLED', True)

    # 每一路召回的候选条数。必须明显大于 RETRIEVER_TOP_K，否则多路融合没有候选可用
    RECALL_TOP_K = _env_int('RECALL_TOP_K', 20)

    # RRF平滑常数。注意本项目召回列表只有 RECALL_TOP_K 条，k=60 时 rank1 与 rank20 的
    # 得分比仅约1.3倍，融合结果接近"被几路同时命中"的投票（共识即相关，是安全的选择）；
    # 想更看重单路名次可以调到 20 左右
    RRF_K = _env_int('RRF_K', 60)

    # BM25 中文切分方式：
    #   char  - 单字 + 相邻双字（默认）。"带薪年假" 会产出 带薪/薪年/年假，
    #           查询"年假"能以二元组精确命中，对词频统计已足够，且不引入分词库依赖
    #   jieba - 装了 jieba 才生效，用于对比效果
    BM25_TOKENIZER = os.environ.get('BM25_TOKENIZER', 'char').strip().lower()

    # 查询改写：短问题几乎没有可用检索线索，此时才值得多付一次 LLM 调用。
    # 阈值定在8而不是12，是因为中文问题信息密度高：「员工每年有多少天带薪年假」正好
    # 12个字却已经是个完整无歧义的问题，改写它纯属白花1~3秒；而「年假」「报销流程」
    # 这类短问题才是真正需要补全关键词的。这个值是最值得按语料实际效果微调的参数
    QUERY_REWRITE_ENABLED = _env_bool('QUERY_REWRITE_ENABLED', True)
    QUERY_REWRITE_COUNT = _env_int('QUERY_REWRITE_COUNT', 2)       # 改写条数；每多一条多一次嵌入往返
    QUERY_REWRITE_MAX_CHARS = _env_int('QUERY_REWRITE_MAX_CHARS', 8)

    # 重排序
    RERANK_ENABLED = _env_bool('RERANK_ENABLED', True)
    RERANK_BACKEND = os.environ.get('RERANK_BACKEND', 'llm').strip().lower()   # llm | cross_encoder | none
    # 留空复用 OLLAMA_LLM_MODEL：同一个模型不会触发 Ollama 重新加载
    RERANK_MODEL = os.environ.get('RERANK_MODEL', '').strip()
    # 送进重排的候选数。12×300字≈4k字，落在 LLM_NUM_CTX=8192 内
    RERANK_TOP_N = _env_int('RERANK_TOP_N', 12)
    # 每个候选在重排提示词里的截断长度。不截断的话长上下文会把重排拖慢甚至挤爆 num_ctx
    RERANK_CANDIDATE_CHARS = _env_int('RERANK_CANDIDATE_CHARS', 300)
    # 重排/改写这类请求路径上的辅助调用超时（秒）。
    # 绝不能沿用 LLM_TIMEOUT 那个 3600——辅助调用一多，Ollama 卡死时用户要等满一小时
    RETRIEVAL_LLM_TIMEOUT = _env_int('RETRIEVAL_LLM_TIMEOUT', 60)

    # 索引分片配置
    # 每个知识库按 doc_id 的稳定哈希拆成 SHARD_COUNT 个 collection（kb_{id}_shard_{n}），
    # 避免单个 collection 无上界增长，并让写入可以按分片并行。
    # 注意：改动该值只影响"新写入"落在哪个分片，读取始终遍历全部分片，因此调大/调小都是读安全的。
    SHARD_COUNT = int(os.environ.get('SHARD_COUNT', 3))
    # 是否兼容改造前的单 collection（kb_{id}）。线上存量向量都还在 legacy collection 里，
    # 关掉它会让这些文档检索不到，除非先把存量数据迁移完。
    SHARD_READ_LEGACY = os.environ.get('SHARD_READ_LEGACY', 'true').strip().lower() in ('1', 'true', 'yes', 'on')

    # 向量化后台任务配置
    # 上传接口只负责落盘入库，向量化交给后台线程池，避免大文件把请求拖到超时。
    # 默认 1 个 worker：串行写入便于排查问题，也避免同时压 Ollama。
    VECTOR_WORKERS = int(os.environ.get('VECTOR_WORKERS', 1))
    VECTOR_QUEUE_SIZE = int(os.environ.get('VECTOR_QUEUE_SIZE', 100))

    # LLM答案缓存配置（两级：进程内LRU + MySQL持久层）
    # 知识库问答的提问重复度很高，命中缓存时无需再走向量检索和LLM生成。
    CACHE_ENABLED = os.environ.get('CACHE_ENABLED', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
    CACHE_L1_MAXSIZE = int(os.environ.get('CACHE_L1_MAXSIZE', 500))
    # L1必须不大于L2：从L2提升到L1时以L2的过期时间为上界，否则L1会比它的来源活得更久。
    # L1是进程内缓存，清不掉其他进程的那一份，靠这个较短的TTL兜底。
    CACHE_L1_TTL = int(os.environ.get('CACHE_L1_TTL', 120))
    CACHE_L2_TTL = int(os.environ.get('CACHE_L2_TTL', 3600))

    # LLM生成参数
    # qwen3.5 是思考型模型，开着思考有两个问题：
    #   1. 慢：实测同一个问题 203s -> 关闭思考后 6s；
    #   2. 偶发"思考结束但正文为空"——模型把整轮预算花在思考上，最后没输出正文，
    #      而 langchain_ollama 0.3.0 会直接丢弃 thinking 字段，界面上只留一个空白回答，
    #      连排查线索都没有。
    # 知识库问答只需要最终答案，因此默认关闭思考。
    # 取值三态：true/false 显式开关；auto 表示不发送该参数（换成不支持思考的模型时用）
    LLM_THINK = _parse_think(os.environ.get('LLM_THINK', 'false'))

    # 上下文窗口与单次生成长度：显式指定，不依赖 Ollama 的默认值
    # 注意：若重新打开思考（LLM_THINK=true），num_predict 必须同步调大
    #      （实测思考会吃掉 1200~3500 个 token），否则正文会被截断成空
    LLM_NUM_CTX = int(os.environ.get('LLM_NUM_CTX', 8192))
    LLM_NUM_PREDICT = int(os.environ.get('LLM_NUM_PREDICT', 2048))

    # 生成答案的请求超时（秒）。本地模型在长文档上生成很慢，故取得很长；
    # 但**不能设成无限**——Ollama 卡死时请求会一直挂着，只能靠重启服务恢复。
    # 请求路径上的辅助调用（重排/改写）另见 RETRIEVAL_LLM_TIMEOUT，那个必须是短超时。
    LLM_TIMEOUT = _env_int('LLM_TIMEOUT', 3600)
