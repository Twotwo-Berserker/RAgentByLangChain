# RAgentByLangChain

基于 LangChain 的 RAG 企业内部知识库问答 Agent 系统。技术栈为后端 Python + 前端 Vue3 + MySQL + Chroma 向量数据库；LLM 使用 Ollama 的 `qwen3.5:9b`，嵌入模型使用 Ollama 的 `qwen3-embedding:4b`，属于入门级 Agent 项目。

## 功能特性

- 用户登录 / JWT 鉴权，密码以 argon2id 加盐哈希存储，管理员与普通用户权限隔离
- 知识库管理：创建、编辑、删除（逻辑删除）
- 文档管理：上传（txt / pdf / md / docx）、解析分块、向量化、删除
- RAG 智能问答：向量检索 + LLM 生成，流式输出（NDJSON），返回答案与引用来源
- 知识溯源：回答中的 `[来源N]` 角标可点击，展开原文并高亮被引用的片段
- 回答反馈：赞 / 踩（可附原因），管理端汇总好评率与差评最多的知识库
- 对话历史：按会话 / 用户分页查询，支持按知识库和反馈筛选
- 首页数据统计（ECharts 可视化）

## 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | Vue 3 + Vite + Vue Router + Pinia + Element Plus + ECharts |
| 后端 | Flask + Flask-SQLAlchemy + Flask-CORS + PyJWT |
| 向量库 | LangChain + ChromaDB |
| 数据库 | MySQL（`db_enterprise_qa`，端口 3306） |
| 模型 | Ollama：`qwen3.5:9b`（对话）、`qwen3-embedding:4b`（嵌入） |

## 目录结构

```
RAgentByLangChain/
├── client/                 # 前端（Vue 3 + Vite）
│   ├── src/api/            #   Axios 接口封装
│   ├── src/views/          #   页面
│   ├── src/stores/         #   Pinia 状态
│   └── src/router/         #   路由与鉴权
└── server/                 # 后端（Flask）
    ├── app.py              #   应用入口
    ├── config.py           #   集中配置
    ├── models/             #   ORM 模型
    ├── routes/             #   路由（蓝图）
    ├── services/           #   RAG / 向量化服务
    ├── utils/              #   JWT / 响应封装
    ├── sql/init.sql        #   建库脚本 + 测试数据
    ├── test_docs/          #   测试文档样例
    ├── uploads/            #   上传文件（运行时生成）
    └── chroma_data/        #   向量数据（运行时生成）
```

## 快速开始

前置依赖：Python 3.11+、Node.js 18+、MySQL 8（端口 3306）、Ollama。

> **Windows 上 MySQL 是作为服务安装的，装完不等于在跑，需要手动启动**，否则后面所有数据库操作都会
> 报 `Can't connect to MySQL server on '127.0.0.1:3306' (10061)`：
>
> ```bash
> net start MySQL80          # 需要以管理员身份打开终端；服务名以实际安装为准
> netstat -ano | findstr :3306   # 看到 LISTENING 才算起来了
> ```
>
> 也可以在「服务」（`services.msc`）里找到 `MySQL80` 手动启动，或设为自动启动。

1. 启动 Ollama 并拉取模型：

   ```bash
   ollama serve
   ollama pull qwen3.5:9b
   ollama pull qwen3-embedding:4b
   ```

2. 启动并初始化数据库
      ```bash
      mysql --default-character-set=utf8mb4 -h127.0.0.1 -P3306 -uroot -p123456 < server/sql/init.sql
      ```
      或启动**mysql>** 后执行命令 
      ```bash
      mysql -u root -p
      输入密码
      
      mysql> SET NAMES utf8mb4;
      mysql> source .../server/sql/init.sql;
      ```
   <br>

   > 补充：如果重新执行了 `init.sql`，`t_document` 记录会被清空，但 `server/chroma_data/` 里的向量数据还在，
   > 两边就对不上了。这种情况直接把 `server/chroma_data/` 目录删掉，重新上传文档向量化即可。


3. 启动后端（端口 5000）：
   建议为这个项目单独建一个虚拟环境。

   ```bash
   cd server
   pip install -r requirements.txt
   ```

   配置 `SECRET_KEY`（JWT 签名密钥）。**这是必填项**：未配置或长度不足 32 位时服务会直接拒绝启动，
   不会退回任何写死在源码里的默认密钥——默认密钥一旦公开，等于任何人可自行签发管理员 token。

   ```bash
   cp .env.example .env          # Windows cmd 用：copy .env.example .env
   # 生成一个随机密钥，把输出填进 .env 的 SECRET_KEY=
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

   `server/.env` 已在 `.gitignore` 中，不会被提交；部署时请改用真实环境变量注入，不要分发该文件。

   ```bash
   python app.py
   ```

4. 启动前端（端口 3000）：

   ```bash
   cd client
   npm install
   npm run dev
   ```

5. 访问 `http://localhost:3000`，使用测试账号登录：

   - 管理员：`admin / 123456`
   - 普通用户：`user1 / 123456`、`user2 / 123456` ...

6. 报错排查顺序建议：

   - 接口报「无法连接Ollama服务」→ 先确认 `ollama serve` 在跑、`ollama list` 能看到`qwen3.5:9b` 和 `qwen3-embedding:4b`。
   - 问答返回「抱歉，在知识库中未找到与您问题相关的内容」→ 这通常是**正确**结果，说明该知识库没有向量化过的文档。先上传文档（管理员账号 → 文档管理）再问。
   - 登录直接报 500 → 多半是数据库没初始化好，回到第 2 步用 `SHOW TABLES` 确认。

更详细的启动流程与代码架构见 [client/README.md](client/README.md) 与 [server/README.md](server/README.md)。

## 可优化方向

- **鉴权安全**：已完成密码 `argon2id` 加盐哈希（`SECRET_KEY` 强制环境变量注入）。
- **检索质量**：引入混合检索（BM25 + 向量）、重排序（reranker）、查询改写与多路召回，提升答案准确率。
- **文档解析**：补充表格、图片 OCR、扫描版 PDF 的解析能力，并支持更大规模文档的向量化（把线程池换成 Celery 队列以获得任务持久化与跨机扩展）。
- **工程健壮性**：增加单元测试与接口测试、结构化日志、统一异常处理、数据库迁移工具（Alembic）与部署容器化（Docker Compose）。
- **性能**：已完成单例复用、检索器缓存、问答流式输出、两级答案缓存、索引分片与增量更新、向量化异步化、前端图表按需引入。

## 可增加业务
- **多轮对话记忆**：把历史问答带入提示词，支持上下文相关的连续追问（当前每轮独立检索）。
- **知识库增强**：支持网页 / 接口 / 定时爬取入库，文档版本管理、审核发布流程与标签分类。
- **权限与协作**：细粒度知识库级授权、组织架构同步、SSO 单点登录。
- **监控与运营**：问答质量统计、模型调用计费、知识库覆盖率分析、用户行为报表。
