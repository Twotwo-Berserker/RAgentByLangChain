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
    RETRIEVER_TOP_K = 4     # 检索返回的相似文档数量

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
