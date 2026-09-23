# Server（后端服务）

基于 Flask 的 RAG 企业知识库后端，提供认证、知识库/文档管理、向量化与问答接口。

## 技术栈

- Flask + Flask-SQLAlchemy + Flask-CORS
- MySQL（`pymysql`）、PyJWT（JWT 鉴权）
- LangChain + ChromaDB（向量检索）
- Ollama（`qwen3.5:9b` 对话模型、`qwen3-embedding:4b` 嵌入模型）

## 启动流程

前置依赖：

1. Python 3.11+，MySQL 运行在 `127.0.0.1:3306`（账号 `root/123456`）。
2. 启动 Ollama 并拉取模型：

   ```bash
   ollama serve
   ollama pull qwen3.5:9b
   ollama pull qwen3-embedding:4b
   ```

3. 初始化数据库（创建 `db_enterprise_qa` 及表结构、测试数据）：

   ```bash
   mysql --default-character-set=utf8mb4 -h127.0.0.1 -P3306 -uroot -p123456 < sql/init.sql
   ```

4. 配置 `SECRET_KEY`（JWT 签名密钥，**必填**：未配置或长度不足 32 位时服务拒绝启动）：

   ```bash
   cp .env.example .env          # Windows cmd 用：copy .env.example .env
   python -c "import secrets; print(secrets.token_urlsafe(48))"   # 填进 .env 的 SECRET_KEY=
   ```

   `server/.env` 已被 `.gitignore` 忽略；部署时请用真实环境变量注入。

启动服务：

```bash
pip install -r requirements.txt
python app.py        # 默认监听 0.0.0.0:5000
```

其余连接配置（数据库、Ollama、Chroma 路径、分块/检索参数）均可在 [config.py](config.py) 中通过环境变量覆盖，完整的可覆盖项见 [.env.example](.env.example)。

## 升级已有数据库

已经按旧版 `init.sql` 建好库、不想重建数据的，按顺序执行 [sql/](sql/) 下的增量脚本，**不要**重跑 `init.sql`（它会 DROP 表）：

| 脚本 | 用途 |
| --- | --- |
| `migrate_v2_feedback.sql` | 对话反馈字段 |
| `migrate_v3_cache.sql` | 答案缓存表（L2） |
| `migrate_v4_password.sql` | 密码列放宽到 `VARCHAR(255)`（配合 argon2 哈希） |

```bash
mysql --default-character-set=utf8mb4 -h127.0.0.1 -P3306 -uroot -p123456 < sql/migrate_v4_password.sql
```

> `migrate_v4_password.sql` 必须在部署新版代码**之前**执行。旧列宽 `VARCHAR(64)` 装不下约 97 字符的
> argon2 哈希，先上代码后迁移会导致密码升级写入被截断或报 1406。
>
> 存量密码是无盐 MD5，无需手动处理：登录逻辑能识别并校验它，用户下次登录时自动改写为 argon2 哈希。
> 查看升级进度：`SELECT COUNT(*) FROM t_user WHERE password NOT LIKE '$argon2id$%';`

## 代码架构

```
server/
├── app.py                  # 应用入口（工厂模式，注册蓝图、CORS、初始化 DB）
├── config.py               # 集中配置（MySQL/Ollama/Chroma/分块/检索）
├── requirements.txt        # Python 依赖
├── sql/init.sql            # 建库建表 + 测试数据
├── models/                 # SQLAlchemy ORM 模型
│   ├── user.py             #   用户
│   ├── knowledge_base.py   #   知识库
│   ├── document.py         #   文档
│   └── chat_history.py     #   对话历史
├── routes/                 # 路由层（蓝图，按模块拆分）
│   ├── auth.py             #   /api/auth     登录、用户信息
│   ├── knowledge_base.py   #   /api/knowledge_base
│   ├── document.py         #   /api/document 上传/列表/删除
│   ├── chat.py             #   /api/chat     RAG 问答、历史
│   ├── user.py             #   /api/user
│   └── stats.py            #   /api/stats
├── services/               # 业务层
│   ├── rag_service.py      #   RAG 问答链（检索→提示词→LLM→解析）
│   └── vector_service.py   #   文档解析/分块/向量写入/删除/检索
└── utils/
    ├── auth.py             #   密码哈希（argon2id，兼容存量MD5）+ JWT 生成校验 + login_required/admin_required 装饰器
    └── response.py         #   统一响应封装（success/error/page_response）
```

分层约定：`routes`（HTTP 参数校验与鉴权）→ `services`（业务逻辑与 RAG 处理）→ `models`（ORM）。RAG 主流程见 [rag_service.py](services/rag_service.py) 的 `ask()`：向量检索 → 拼装上下文 → 提示词 → LLM 生成 → 提取引用来源；文档向量化与 Chroma 写入见 [vector_service.py](services/vector_service.py) 的 `process_document()`（含 Ollama 预检查与失败重试）。

## 主要接口

| 模块 | 方法/路径 | 说明 |
| --- | --- | --- |
| 认证 | `POST /api/auth/login` | 登录，返回 JWT |
| 知识库 | `GET/POST /api/knowledge_base` | 列表 / 新建（管理员） |
| 文档 | `POST /api/document/upload` | 上传并向量化（管理员） |
| 问答 | `POST /api/chat/ask` | RAG 问答，返回答案与引用来源 |
| 问答 | `GET /api/chat/history` | 对话历史（分页） |

接口统一返回 `{ code, message, data }`，`code=200` 表示成功。
