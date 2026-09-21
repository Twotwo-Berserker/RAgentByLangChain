"""
项目配置文件
包含数据库、Ollama、Chroma等配置信息
"""
import os


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


class Config:
    """基础配置类"""

    # Flask密钥，用于JWT签名
    SECRET_KEY = os.environ.get('SECRET_KEY', 'enterprise-qa-secret-key-2024')

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
