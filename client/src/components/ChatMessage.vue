<template>
  <!-- 对话消息气泡组件 -->
  <div class="message-wrapper" :class="{ 'is-user': isUser }">
    <div class="avatar">
      <el-avatar :size="36" :icon="isUser ? UserFilled : Monitor" :style="avatarStyle" />
    </div>
    <div class="bubble" :class="{ 'user-bubble': isUser, 'ai-bubble': !isUser }">
      <!-- 用户消息按纯文本展示 -->
      <div v-if="isUser" class="message-text">{{ message.content }}</div>

      <!-- AI回答：Markdown 渲染成 HTML；[来源N] 为可点击角标，有出处的内容块加淡黄底纹 -->
      <div v-else class="message-text">
        <!-- 标注这条回答基于哪个知识库：对话可以跨知识库延续，不标注容易看混 -->
        <div v-if="message.kbName" class="answer-kb">{{ message.kbName }}</div>
        <div class="md-body" v-html="rendered.html" @click="onContentClick"></div>
      </div>

      <!-- AI回答时显示参考来源（编号与回答中的 [来源N] 角标对应，点击可看原文） -->
      <div v-if="!isUser && message.sources?.length" class="sources">
        <div class="sources-title">参考来源（点击查看原文）：</div>
        <el-tag
          v-for="(src, i) in message.sources"
          :key="i"
          size="small"
          type="info"
          class="source-tag"
          @click="openSource(src.index ?? i + 1)"
        >
          {{ src.index ?? i + 1 }}. {{ src.file_name }}
        </el-tag>
      </div>

      <!-- 反馈区：有 chatId 才可反馈（说明后端已落库） -->
      <div v-if="!isUser && message.chatId" class="feedback-bar">
        <el-tooltip content="回答有帮助" placement="top">
          <el-button
            text
            size="small"
            class="feedback-btn"
            :class="{ 'is-active-like': message.feedback === 1 }"
            @click="onLike"
          >
            <el-icon><CaretTop /></el-icon>赞
          </el-button>
        </el-tooltip>
        <el-tooltip content="回答不准确" placement="top">
          <el-button
            text
            size="small"
            class="feedback-btn"
            :class="{ 'is-active-dislike': message.feedback === -1 }"
            @click="onDislike"
          >
            <el-icon><CaretBottom /></el-icon>踩
          </el-button>
        </el-tooltip>
        <span v-if="message.feedback" class="feedback-done">
          已反馈{{ message.feedbackComment ? `：${message.feedbackComment}` : '' }}
        </span>
      </div>
    </div>

    <!-- 原文溯源抽屉：展示被引用的完整分块，并高亮答案引用的片段 -->
    <el-drawer
      v-model="sourceVisible"
      :title="activeSource ? `溯源 · ${activeSource.file_name}` : '溯源'"
      size="480px"
      append-to-body
    >
      <div v-if="activeSource" class="source-detail">
        <div class="source-meta">
          <el-tag size="small" type="primary">来源 {{ activeSource.index }}</el-tag>
          <span class="source-file">{{ activeSource.file_name }}</span>
          <span v-if="activeSource.chunk_index !== undefined && activeSource.chunk_index !== null" class="source-chunk">
            第 {{ activeSource.chunk_index + 1 }} 分块
          </span>
        </div>

        <div class="source-hint" :class="{ 'is-empty': !fragment }">
          {{ fragment ? '黄色底纹为答案引用的原文片段' : '未在原文中定位到逐字片段，以下为完整参考内容' }}
        </div>

        <div class="source-content">
          <span
            v-for="(part, i) in highlightedContent"
            :key="i"
            :class="{ 'is-mark': part.mark }"
          >{{ part.text }}</span>
        </div>
      </div>
    </el-drawer>

    <!-- 踩的原因（可选） -->
    <el-dialog v-model="dislikeVisible" title="反馈问题原因" width="460px" append-to-body>
      <el-input
        v-model="dislikeComment"
        type="textarea"
        :rows="3"
        maxlength="200"
        show-word-limit
        placeholder="可选：回答哪里不对？例如「内容不准确」「未回答问题」「答案过时」"
      />
      <template #footer>
        <el-button @click="dislikeVisible = false">取消</el-button>
        <el-button type="danger" @click="confirmDislike">提交</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
/**
 * 对话消息气泡组件
 * 区分用户消息和AI回答，AI回答支持：
 * 1. Markdown 渲染：标题、列表、表格、代码块等按 Markdown 呈现，而不是显示语法符号
 * 2. 知识溯源：回答中的 [来源N] 渲染为可点击角标，点击查看原文并高亮被引用片段
 * 3. 反馈：赞 / 踩（踩可附原因），提交给后端用于后续优化
 */
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { UserFilled, Monitor, CaretTop, CaretBottom } from '@element-plus/icons-vue'
import { renderMarkdown, CITE_CLASS } from '../utils/markdown'

const props = defineProps({
  /** 消息对象 { role, content, sources?, kbId?, kbName?, chatId?, feedback?, feedbackComment? } */
  message: { type: Object, required: true },
  /** 是否正在流式输出（决定 Markdown 实时渲染的节流与代码围栏补全） */
  streaming: { type: Boolean, default: false }
})

const emit = defineEmits(['feedback'])

/** 是否为用户消息 */
const isUser = computed(() => props.message.role === 'user')

/** 头像样式 */
const avatarStyle = computed(() => ({
  backgroundColor: isUser.value ? '#409eff' : '#67c23a'
}))

/**
 * 渲染结果：{ html, citeMap }
 * citeMap 记录每个来源第一次被引用时的整块文本，用于在原文里定位片段
 */
const rendered = ref({ html: '', citeMap: {} })

/** 流式渲染节流间隔：每个 token 都重排整篇 Markdown 会明显掉帧 */
const STREAM_RENDER_INTERVAL = 80
let renderTimer = null

/** 立即按当前内容渲染 */
function doRender() {
  rendered.value = renderMarkdown(props.message.content || '', {
    sources: props.message.sources || [],
    streaming: props.streaming
  })
}

/**
 * 内容/来源变化时重新渲染
 * 流式输出期间按固定间隔合并渲染，回答结束后立即渲染最终版本
 * （最终版本会补全未闭合的代码块围栏，避免结束时样式停在一半）
 */
watch(
  [() => props.message.content, () => props.message.sources, () => props.streaming],
  () => {
    if (!props.streaming) {
      clearTimeout(renderTimer)
      renderTimer = null
      doRender()
      return
    }
    if (renderTimer) return
    renderTimer = setTimeout(() => {
      renderTimer = null
      doRender()
    }, STREAM_RENDER_INTERVAL)
  },
  { immediate: true }
)

onBeforeUnmount(() => clearTimeout(renderTimer))

/** 点击回答中的来源角标：事件委托，避免给每个角标单独挂监听 */
function onContentClick(e) {
  const badge = e.target.closest?.(`.${CITE_CLASS}`)
  if (!badge) return
  openSource(Number(badge.dataset.cite))
}

/** 溯源抽屉 */
const sourceVisible = ref(false)
const activeSource = ref(null)

/** 当前来源在答案中被引用的那段文本 */
const activeSentence = computed(() =>
  activeSource.value ? rendered.value.citeMap[activeSource.value.index] || '' : ''
)

/**
 * 在原文中查找答案引用到的连续片段（最长公共子串）
 * 逐字引用命中时结果较准；模型改写过的句子可能匹配不到，此时返回空串
 */
const fragment = computed(() => {
  const content = activeSource.value?.content || ''
  const sentence = activeSentence.value.replace(/\[来源\s*\d+\]/g, '').trim()
  if (!content || !sentence) return ''

  let best = ''
  for (let i = 0; i < sentence.length; i++) {
    for (let j = i + best.length + 1; j <= sentence.length; j++) {
      const sub = sentence.slice(i, j)
      if (content.includes(sub)) {
        if (sub.length > best.length) best = sub
      } else {
        break
      }
    }
  }
  // 片段过短（少于4字）多半是"的""公司"之类的巧合，不作为引用高亮
  return best.length >= 4 ? best : ''
})

/** 原文按引用片段切分为 [前缀, 高亮, 后缀]，避免使用 v-html */
const highlightedContent = computed(() => {
  const content = activeSource.value?.content || ''
  const frag = fragment.value
  if (!frag) return [{ text: content, mark: false }]

  const idx = content.indexOf(frag)
  if (idx === -1) return [{ text: content, mark: false }]

  return [
    { text: content.slice(0, idx), mark: false },
    { text: frag, mark: true },
    { text: content.slice(idx + frag.length), mark: false }
  ]
})

/** 打开溯源抽屉 */
function openSource(index) {
  const src = (props.message.sources || []).find((s) => s.index === index)
    || props.message.sources?.[index - 1]
  if (!src) return
  activeSource.value = { index, ...src }
  sourceVisible.value = true
}

/** 赞：再次点击可取消反馈 */
function onLike() {
  // 再次点击取消反馈
  emit('feedback', props.message.feedback === 1 ? 0 : 1, '')
}

/** 踩：弹出原因输入 */
const dislikeVisible = ref(false)
const dislikeComment = ref('')

function onDislike() {
  if (props.message.feedback === -1) {
    emit('feedback', 0, '')
    return
  }
  dislikeComment.value = ''
  dislikeVisible.value = true
}

/** 确认提交踩的反馈 */
function confirmDislike() {
  dislikeVisible.value = false
  emit('feedback', -1, dislikeComment.value.trim())
}
</script>

<style scoped>
.message-wrapper {
  display: flex;
  gap: 12px;
  margin-bottom: 20px;
  align-items: flex-start;
}

.message-wrapper.is-user {
  flex-direction: row-reverse;
}

.bubble {
  max-width: 70%;
  padding: 12px 16px;
  border-radius: 12px;
  line-height: 1.6;
  word-break: break-word;
}

.user-bubble {
  background: #409eff;
  color: #fff;
  border-top-right-radius: 4px;
  /* 用户提问是纯文本，保留换行；AI回答走 Markdown，由块级元素排版 */
  white-space: pre-wrap;
}

.ai-bubble {
  background: #f4f4f5;
  color: #303133;
  border-top-left-radius: 4px;
}

.message-text {
  font-size: 14px;
}

/* 回答所属知识库标签 */
.answer-kb {
  display: inline-block;
  margin-bottom: 6px;
  padding: 0 6px;
  font-size: 12px;
  line-height: 18px;
  color: #909399;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
}

/* AI回答的 Markdown 排版样式在 assets/markdown.css 中统一维护（问答气泡与对话历史详情共用） */

.sources {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid #e4e7ed;
}

.sources-title {
  font-size: 12px;
  color: #909399;
  margin-bottom: 4px;
}

.source-tag {
  margin-right: 4px;
  margin-bottom: 4px;
  cursor: pointer;
}

/* 反馈区 */
.feedback-bar {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: 8px;
  padding-top: 6px;
  border-top: 1px dashed #e4e7ed;
}

.feedback-btn {
  color: #909399;
  padding: 2px 6px;
}

.feedback-btn:hover {
  color: #409eff;
}

.feedback-btn.is-active-like {
  color: #409eff;
  font-weight: 600;
}

.feedback-btn.is-active-dislike {
  color: #f56c6c;
  font-weight: 600;
}

.feedback-done {
  font-size: 12px;
  color: #c0c4cc;
  margin-left: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 260px;
}

/* 溯源抽屉 */
.source-detail {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.source-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.source-file {
  font-weight: 600;
  color: #303133;
  font-size: 14px;
}

.source-chunk {
  font-size: 12px;
  color: #909399;
}

.source-hint {
  font-size: 12px;
  color: #e6a23c;
  background: #fdf6ec;
  padding: 6px 10px;
  border-radius: 4px;
}

.source-hint.is-empty {
  color: #909399;
  background: #f4f4f5;
}

.source-content {
  font-size: 13px;
  line-height: 1.9;
  color: #606266;
  background: #fafafa;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 12px;
  white-space: pre-wrap;
  word-break: break-word;
}

.source-content .is-mark {
  background: #ffe08a;
  border-radius: 2px;
  padding: 1px 0;
}
</style>
