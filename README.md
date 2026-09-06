# RAgentByLangChain

基于 LangChain 的 RAG 企业内部知识库问答 Agent 系统。技术栈为后端 Python + 前端 Vue3 + MySQL + Chroma 向量数据库；LLM 使用 Ollama 的 `qwen3.5:9b`，嵌入模型使用 Ollama 的 `qwen3-embedding:4b`，属于入门级 Agent 项目。

## 功能特性

- 用户登录 / JWT 鉴权，管理员与普通用户权限隔离
- 知识库管理：创建、编辑、删除（逻辑删除）
- 文档管理：上传（txt / pdf / md / docx）、解析分块、向量化、删除
- RAG 智能问答：向量检索 + LLM 生成，返回答案与引用来源
- 对话历史：按会话 / 用户分页查询
- 首页数据统计（ECharts 可视化）

## 技术栈

| 层 | 技术 |
| --- | --- |
| 前端 | Vue 3 + Vite + Vue Router + Pinia + Element Plus + ECharts |
| 后端 | Flask + Flask-SQLAlchemy + Flask-CORS + PyJWT |
| 向量库 | LangChain + ChromaDB |
| 数据库 | MySQL（`db_enterprise_qa`，端口 3308） |
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

前置依赖：Python 3.11+、Node.js 18+、MySQL 8（端口 3308）、Ollama。

1. 启动 Ollama 并拉取模型：

   ```bash
   ollama serve
   ollama pull qwen3.5:9b
   ollama pull qwen3-embedding:4b
   ```

2. 初始化数据库：

   ```bash
   mysql -h127.0.0.1 -P3308 -uroot -p123456 < server/sql/init.sql
   ```

3. 启动后端（端口 5000）：

   ```bash
   cd server
   pip install -r requirements.txt
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
   - 普通用户：`user1 / 123456`、`user2 / 123456`

更详细的启动流程与代码架构见 [client/README.md](client/README.md) 与 [server/README.md](server/README.md)。

## 可优化方向

- **鉴权安全**：密码改用 `bcrypt`/`argon2` 加盐哈希，替换当前 MD5；`SECRET_KEY` 强制从环境变量注入，避免硬编码。
- **检索质量**：引入混合检索（BM25 + 向量）、重排序（reranker）、查询改写与多路召回，提升答案准确率。
- **文档解析**：补充表格、图片 OCR、扫描版 PDF 的解析能力，并支持更大规模文档的异步向量化（Celery 队列）。
- **工程健壮性**：增加单元测试与接口测试、结构化日志、统一异常处理、数据库迁移工具（Alembic）与部署容器化（Docker Compose）。
- **性能**：向量检索与 LLM 调用加缓存；对大知识库做索引分片与增量更新；前端路由懒加载与体积优化。

## 可增加业务

- **流式问答与多轮对话**：基于 SSE 流式输出，支持上下文记忆的连续追问。
- **知识溯源与反馈**：答案高亮引用原文片段，支持「赞 / 踩」反馈用于后续优化。
- **知识库增强**：支持网页 / 接口 / 定时爬取入库，文档版本管理、审核发布流程与标签分类。
- **权限与协作**：细粒度知识库级授权、组织架构同步、SSO 单点登录。
- **监控与运营**：问答质量统计、模型调用计费、知识库覆盖率分析、用户行为报表。
