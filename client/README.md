# Client（前端）

基于 Vue 3 + Vite 的企业知识库问答前端，提供登录、知识库/文档/用户管理、智能问答与对话历史等功能。

## 技术栈

- Vue 3（`<script setup>`）+ Vite + Vue Router + Pinia
- Element Plus（含中文 locale）+ Element Plus Icons
- Axios（请求封装）、ECharts（数据可视化）

## 启动流程

前置依赖：Node.js 18+（建议 20+），后端服务已启动（默认 `http://127.0.0.1:5000`）。

```bash
npm install
npm run dev        # 默认监听 http://localhost:3000
```

生产构建与预览：

```bash
npm run build
npm run preview
```

开发环境下 [vite.config.js](vite.config.js) 将 `/api` 请求代理到后端 `http://127.0.0.1:5000`，无需额外配置跨域。

## 代码架构

```
client/
├── index.html
├── vite.config.js          # 端口(3000)与 /api 代理配置
├── package.json
└── src/
    ├── main.js             # 入口：注册 Router/Pinia/Element Plus/图标
    ├── App.vue
    ├── api/                # Axios 接口封装（按模块拆分）
    │   ├── index.js        #   axios 实例、拦截器（携带 Token、统一错误处理）
    │   ├── auth.js / user.js / chat.js / document.js / knowledge_base.js / stats.js
    ├── router/index.js     # 路由 + 导航守卫（登录校验、管理员权限控制）
    ├── stores/user.js      # Pinia 用户状态（token / userInfo / 登录态）
    ├── components/         # 复用组件（ChatMessage 等）
    └── views/              # 页面
        ├── Login.vue / Layout.vue
        ├── Home.vue            # 首页（ECharts 统计）
        ├── KnowledgeBase.vue   # 知识库管理（管理员）
        ├── Document.vue        # 文档管理（管理员）
        ├── UserManage.vue      # 用户管理（管理员）
        ├── Chat.vue            # 智能问答
        └── ChatHistory.vue     # 对话历史
```

分层约定：`views`（页面与交互）→ `api`（Axios 请求封装）→ `stores`（Pinia 状态）→ `router`（路由与鉴权）。登录态与 Token 存于 `localStorage`，由 [stores/user.js](src/stores/user.js) 统一管理；权限控制由 [router/index.js](src/router/index.js) 的全局前置守卫实现（普通用户无法访问标记 `requireAdmin` 的页面）。
