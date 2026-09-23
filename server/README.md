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
│   ├── hybrid_retriever.py #   混合检索编排（多路召回→RRF融合→重排）
│   ├── vector_service.py   #   文档解析/分块/向量写入/删除/检索
│   ├── bm25_service.py     #   BM25 词法索引（混合检索的稀疏一路）
│   ├── reranker_service.py #   重排序（可插拔：llm / cross_encoder / none）
│   ├── query_rewrite_service.py # 短问题查询改写（多路召回的来源）
│   ├── retrieval_llm.py    #   检索辅助任务的 LLM 客户端工厂
│   ├── cache_service.py    #   两级答案缓存（L1 进程内 + L2 MySQL）
│   └── task_queue.py       #   向量化后台线程池（上传后异步处理）
└── utils/
    ├── auth.py             #   密码哈希（argon2id，兼容存量MD5）+ JWT 生成校验 + login_required/admin_required 装饰器
    └── response.py         #   统一响应封装（success/error/page_response）
```

分层约定：`routes`（HTTP 参数校验与鉴权）→ `services`（业务逻辑与 RAG 处理）→ `models`（ORM）。RAG 主流程见 [rag_service.py](services/rag_service.py) 的 `ask()`：查缓存 → 混合检索 → 拼装上下文 → 提示词 → LLM 生成 → 提取引用来源；文档向量化与 Chroma 写入见 [vector_service.py](services/vector_service.py) 的 `process_document()`（含 Ollama 预检查与失败重试）。

### 检索链路

检索从「单路向量 top_k」升级为**多路召回 + 融合 + 重排**，由 [hybrid_retriever.py](services/hybrid_retriever.py) 编排：

```
问题 ─┬─ 查询改写(仅短问题) ─→ 每条改写各一路向量召回 ─┐
      └─ 原问题 ─→ 向量召回 ────────────────────────┼─→ RRF 融合 ─→ 重排 ─→ top_k
                    └─ BM25 词法召回 ────────────────┘
```

四个刻意的设计选择：

| 选择 | 原因 |
| --- | --- |
| 融合用 RRF（只吃名次） | 向量返回的是距离（越小越好），BM25 返回的是相似度（越大越好），量纲不可比，任何归一化口径都站不住 |
| 词法只跑原问题，不跑改写 | 改写句按定义换了词，恰是词法匹配最不擅长的；让近随机的列表参与投票只会把它的任意 top-N 抬进候选池 |
| RRF 统一权重、不调参 | 加权只压低「仅被改写命中」的文档，而那正是改写想召回的那批。分工改为：RRF 保守地最大化召回，精确性交给重排 |
| 重排输出「排列」而非分数 | 分数数组在候选数对不上时错位是**静默**的；排列可做集合校验，缺失项补在末尾，模型只能提前、不能丢弃 |

中文切分不用分词库，按**相邻双字**（「带薪年假」→ 带薪/薪年/年假），且不保留单字——单字里混着大量虚词（与/的/和），文档频率极高、区分度近乎为零，实测会让「量子计算与航天器」仅因一个「与」字就把薪酬制度文档排到第一位。词法索引按知识库惰性构建、失效只递增版本号（`invalidate()` 实测 0.002ms），重建推迟到文档变更后的首次检索。

重排是本次唯一显著的新增延迟（约 2~5 秒）。`HYBRID_ENABLED` / `RERANK_ENABLED` / `QUERY_REWRITE_ENABLED` 三个开关可独立关闭，且每个环节失败都只降级不报错：重排失败退回融合顺序、改写失败只用原问题，全部召回路径都失败才抛异常交给上层回退到纯向量检索。

## 主要接口

| 模块 | 方法/路径 | 说明 |
| --- | --- | --- |
| 认证 | `POST /api/auth/login` | 登录，返回 JWT |
| 知识库 | `GET/POST /api/knowledge_base` | 列表 / 新建（管理员） |
| 文档 | `POST /api/document/upload` | 上传并向量化（管理员） |
| 问答 | `POST /api/chat/ask` | RAG 问答，返回答案与引用来源 |
| 问答 | `GET /api/chat/history` | 对话历史（分页） |

接口统一返回 `{ code, message, data }`，`code=200` 表示成功。
