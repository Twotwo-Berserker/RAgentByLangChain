<template>
  <!-- 智能问答对话页面 -->
  <div class="chat-container">
    <!-- 左侧知识库选择 -->
    <div class="chat-sidebar">
      <div class="sidebar-title">选择知识库</div>
      <div class="kb-list">
        <div
          v-for="kb in kbList"
          :key="kb.id"
          class="kb-item"
          :class="{ active: selectedKb?.id === kb.id }"
          @click="selectKb(kb)"
        >
          <el-icon><FolderOpened /></el-icon>
          <span class="kb-name">{{ kb.kb_name }}</span>
          <el-tag size="small" type="info">{{ kb.doc_count }}篇</el-tag>
        </div>
      </div>
      <div v-if="kbList.length === 0" class="empty-tip">
        <el-empty description="暂无知识库" :image-size="60" />
      </div>
    </div>

    <!-- 右侧对话区域 -->
    <div class="chat-main">
      <!-- 对话窗口标题 -->
      <div class="chat-header">
        <span v-if="selectedKb">
          <el-icon><ChatDotRound /></el-icon>
          当前知识库：{{ selectedKb.kb_name }}
        </span>
        <span v-else class="hint">请先从左侧选择一个知识库</span>
        <span v-if="messages.length" class="keep-hint">切换知识库或页面都不会清空对话</span>
        <el-button
          v-if="messages.length"
          text
          size="small"
          class="clear-btn"
          @click="clearChat"
        >
          <el-icon><Delete /></el-icon>清空对话
        </el-button>
      </div>

      <!-- 消息列表 -->
      <div class="chat-messages" ref="messagesRef" @scroll.passive="onMessagesScroll">
        <div v-if="messages.length === 0" class="welcome">
          <el-icon :size="64" color="#c0c4cc"><ChatDotSquare /></el-icon>
          <h3>欢迎使用企业知识库问答系统</h3>
          <p>请从左侧选择知识库，然后输入您的问题</p>
        </div>
        <ChatMessage
          v-for="(msg, i) in messages"
          :key="i"
          :message="msg"
          :streaming="asking && i === messages.length - 1 && msg.role === 'ai'"
          @feedback="(value, comment) => handleFeedback(i, value, comment)"
        />
        <!-- 加载中提示 -->
        <div v-if="thinking" class="loading-msg">
          <el-avatar :size="36" :icon="Monitor" style="background-color: #67c23a" />
          <div class="loading-bubble">
            <span class="loading-dot">思考中</span>
            <el-icon class="is-loading"><Loading /></el-icon>
          </div>
        </div>
      </div>

      <!-- 上滑查看前文时不再把界面强行拉回底部，改由这个按钮回到最新 -->
      <transition name="fade">
        <el-button
          v-if="!autoScroll && messages.length"
          class="to-bottom-btn"
          circle
          @click="scrollToBottom(true)"
        >
          <el-icon><ArrowDown /></el-icon>
        </el-button>
      </transition>

      <!-- 输入区域 -->
      <div class="chat-input">
        <el-input
          v-model="question"
          type="textarea"
          :rows="2"
          placeholder="请输入您的问题..."
          :disabled="!selectedKb || asking"
          @keydown.enter.exact.prevent="sendQuestion"
          resize="none"
        />
        <el-button
          type="primary"
          :icon="Promotion"
          :loading="asking"
          :disabled="!selectedKb || !question.trim()"
          @click="sendQuestion"
          class="send-btn"
        >
          发送
        </el-button>
      </div>
    </div>
  </div>
</template>

<script setup>
/**
 * 智能问答对话页面
 * 左侧选择知识库，右侧进行对话
 * 对话状态（消息列表、会话ID、选中知识库）由 Pinia store 持有，
 * 因此切换到其他页面再回来内容不会丢失，流式回答也会继续写入。
 */
import { ref, computed, watch, onMounted, nextTick } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage } from 'element-plus'
import { Promotion, Loading, Monitor, Delete, ArrowDown } from '@element-plus/icons-vue'
import { getAllKB } from '../api/knowledge'
import { useChatStore } from '../stores/chat'
import ChatMessage from '../components/ChatMessage.vue'

const chatStore = useChatStore()
const { messages, asking } = storeToRefs(chatStore)

/** 知识库列表 */
const kbList = ref([])
/** 当前选中的知识库 */
const selectedKb = ref(null)
/** 当前输入的问题 */
const question = ref('')
/** 消息列表DOM引用 */
const messagesRef = ref(null)

/** 是否仍在等待首个回答片段（用于显示"思考中"提示） */
const thinking = computed(() => {
  if (!asking.value) return false
  const last = messages.value[messages.value.length - 1]
  return !last || last.role !== 'ai' || !last.content
})

/** 加载知识库列表 */
async function loadKBList() {
  try {
    const res = await getAllKB()
    kbList.value = res.data
    // 恢复上次选中的知识库（对话内容由 store 从 sessionStorage 恢复）
    if (chatStore.kbId) {
      const found = kbList.value.find((kb) => kb.id === chatStore.kbId)
      if (found) {
        selectedKb.value = found
      } else {
        // 知识库已被删除/禁用，清掉残留对话，避免展示查不到的知识库内容
        chatStore.reset()
      }
    }
  } catch (err) {
    // 错误已在拦截器处理
  }
}

/**
 * 选择知识库
 * 只影响后续提问的检索范围，已有对话保留（由 store 保证），
 * 因此切换知识库后可以直接追问，不必担心历史问答被清掉
 */
function selectKb(kb) {
  if (selectedKb.value?.id === kb.id) return
  selectedKb.value = kb
  chatStore.selectKb(kb)
}

/** 清空当前对话 */
function clearChat() {
  chatStore.reset()
  // 清空后消息区回到顶部，恢复自动跟随，否则该按钮会一直留在页面上
  autoScroll.value = true
  ElMessage.success('已清空对话')
}

/**
 * 是否跟随最新内容自动滚动
 * 用户手动上滑查看前文时置为 false，此时流式回答继续写入也不会把界面拉回底部；
 * 滑回底部（或点「回到底部」按钮、重新提问）后恢复跟随
 */
const autoScroll = ref(true)

/** 距底部多少像素以内算"还停在底部"：留一点余量，避免滚动惯性和取整导致误判 */
const BOTTOM_THRESHOLD = 60

/** 滚动到底部（force=true 时无论用户是否上滑都拉回底部，并恢复自动跟随） */
async function scrollToBottom(force = false) {
  if (force) autoScroll.value = true
  if (!autoScroll.value) return
  await nextTick()
  const el = messagesRef.value
  if (el) el.scrollTop = el.scrollHeight
}

/** 监听滚动，判断用户是否仍停在底部 */
function onMessagesScroll() {
  const el = messagesRef.value
  if (!el) return
  const distance = el.scrollHeight - el.scrollTop - el.clientHeight
  autoScroll.value = distance <= BOTTOM_THRESHOLD
}

/** 发送问题 */
async function sendQuestion() {
  const q = question.value.trim()
  if (!q || !selectedKb.value || asking.value) return

  question.value = ''
  // 请求在 store 中执行，切页/组件卸载都不会中断
  // 带上当前知识库，回答上会标注它基于哪个知识库（对话可跨知识库延续）
  const promise = chatStore.sendQuestion(q, selectedKb.value)
  // 主动提问说明用户想看新回答，此时强制回到底部并恢复自动跟随
  scrollToBottom(true)
  await promise
  scrollToBottom()
}

/** 提交反馈（赞 / 踩） */
async function handleFeedback(index, value, comment) {
  try {
    await chatStore.sendFeedback(index, value, comment)
    if (value === 1) ElMessage.success('感谢反馈，已标记为有帮助')
    else if (value === -1) ElMessage.success('感谢反馈，我们会持续优化回答质量')
  } catch (err) {
    ElMessage.error('反馈提交失败，请稍后重试')
  }
}

// 流式过程中内容不断增长，跟在底部时才继续滚动；
// 用户上滑查看前文后 autoScroll 为 false，回答继续生成也不会打断阅读
watch(
  () => messages.value[messages.value.length - 1]?.content,
  () => {
    if (asking.value) scrollToBottom()
  }
)

onMounted(async () => {
  await loadKBList()
  // 回到页面时定位到最新一条，正在生成的回答不用手动往下翻
  scrollToBottom(true)
})
</script>

<style scoped>
.chat-container {
  display: flex;
  height: calc(100vh - 100px);
  background: #fff;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.06);
}

/* 左侧知识库选择栏 */
.chat-sidebar {
  width: 260px;
  border-right: 1px solid #ebeef5;
  display: flex;
  flex-direction: column;
  background: #fafafa;
}

.sidebar-title {
  padding: 16px 20px;
  font-weight: 600;
  font-size: 15px;
  color: #303133;
  border-bottom: 1px solid #ebeef5;
}

.kb-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.kb-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.2s;
  margin-bottom: 4px;
}

.kb-item:hover {
  background: #ecf5ff;
}

.kb-item.active {
  background: #409eff;
  color: #fff;
}

.kb-item.active .el-tag {
  color: #fff;
  background: rgba(255, 255, 255, 0.2);
  border-color: transparent;
}

.kb-name {
  flex: 1;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.empty-tip {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

/* 右侧对话区域 */
.chat-main {
  position: relative;
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.chat-header {
  padding: 14px 20px;
  border-bottom: 1px solid #ebeef5;
  font-size: 14px;
  font-weight: 500;
  color: #303133;
  display: flex;
  align-items: center;
  gap: 6px;
}

.chat-header .hint {
  color: #909399;
}

/* 提示文案占据剩余空间，把「清空对话」按钮顶到最右侧 */
.keep-hint {
  margin-left: auto;
  font-size: 12px;
  font-weight: 400;
  color: #c0c4cc;
}

.clear-btn {
  margin-left: 12px;
  color: #909399;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
}

/* 「回到底部」按钮：悬浮在消息区右下角，与输入框拉开一点距离 */
.to-bottom-btn {
  position: absolute;
  right: 28px;
  bottom: 100px;
  width: 38px;
  height: 38px;
  color: #409eff;
  background: #fff;
  border-color: #dcdfe6;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.12);
  z-index: 10;
}

.to-bottom-btn:hover {
  color: #fff;
  background: #409eff;
  border-color: #409eff;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

.welcome {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: #c0c4cc;
  gap: 12px;
}

.welcome h3 {
  color: #909399;
  font-size: 18px;
}

.welcome p {
  font-size: 14px;
}

.loading-msg {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  margin-bottom: 20px;
}

.loading-bubble {
  background: #f4f4f5;
  padding: 12px 16px;
  border-radius: 12px;
  border-top-left-radius: 4px;
  display: flex;
  align-items: center;
  gap: 8px;
  color: #909399;
  font-size: 14px;
}

/* 输入区域 */
.chat-input {
  padding: 16px 20px;
  border-top: 1px solid #ebeef5;
  display: flex;
  gap: 12px;
  align-items: flex-end;
  background: #fff;
}

.chat-input :deep(.el-textarea__inner) {
  border-radius: 8px;
}

.send-btn {
  height: 54px;
  border-radius: 8px;
}
</style>
