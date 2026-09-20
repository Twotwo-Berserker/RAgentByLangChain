/**
 * 问答状态管理（Pinia）
 *
 * 把对话状态从 Chat.vue 组件提升到 store，修复「切到其他页面再回来，对话内容消失」的问题：
 * 组件卸载不会中断流式请求，回答片段会继续写入 store，回到问答页即可看到完整结果。
 * 同时用 sessionStorage 缓存已完成的对话，刷新页面也能恢复。
 *
 * 清空时机只有两个：点击页面右上角的「清空对话」按钮、退出登录。
 * 切换知识库、切换页面都不会清空，保证一次登录过程中对话始终连续。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { askQuestionStream, submitFeedback } from '../api/chat'

/** sessionStorage 存储键 */
const STORAGE_KEY = 'chatState'

/** 最多缓存的对话条数：一轮问答含来源原文，缓存太多会超出 sessionStorage 配额 */
const MAX_CACHED_MESSAGES = 40

/** 从 sessionStorage 恢复上次的会话状态 */
function loadState() {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) : null
  } catch (e) {
    // 数据损坏时忽略，按全新会话处理
    return null
  }
}

export const useChatStore = defineStore('chat', () => {
  const saved = loadState()

  /** 当前会话ID */
  const sessionId = ref(saved?.sessionId || '')
  /** 当前选中的知识库ID */
  const kbId = ref(saved?.kbId ?? null)
  /** 对话消息列表：{ role, content, sources, kbId, kbName, chatId, feedback, feedbackComment } */
  const messages = ref(saved?.messages || [])
  /** 是否正在请求中 */
  const asking = ref(false)

  /**
   * 流式请求序号：切换知识库时会自增，
   * 旧请求的回调据此判断自己已过期，不再写入消息列表。
   */
  let streamSeq = 0

  /** 生成会话ID */
  function generateSessionId() {
    return 'sess_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8)
  }

  /** 持久化当前对话（流式进行中不写入，避免存下半截回答） */
  function persist() {
    if (asking.value) return
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
        sessionId: sessionId.value,
        kbId: kbId.value,
        messages: messages.value.slice(-MAX_CACHED_MESSAGES)
      }))
    } catch (e) {
      // 存储失败（如超出配额）忽略，不影响正常对话
    }
  }

  /**
   * 切换知识库：只切换后续提问的检索范围，已有对话原样保留
   *
   * 注意这里既不清空 messages，也不自增 streamSeq：
   *  - 不清空：一次登录过程中，除了右上角「清空对话」按钮，任何操作都不该让对话消失；
   *  - 不自增：正在流式输出的回答属于上一个知识库，自增会让回调全部失效，
   *    页面上半截回答卡住不动，而结果其实已经落库——这正是之前"对话框不见了、
   *    过一会儿又能在对话历史里看到结果"的另一半原因。
   */
  function selectKb(kb) {
    if (kbId.value === kb.id) return
    kbId.value = kb.id
    persist()
  }

  /** 清空对话状态（点击「清空对话」或退出登录时调用） */
  function reset() {
    streamSeq += 1
    messages.value = []
    sessionId.value = generateSessionId()
    asking.value = false
    persist()
  }

  /**
   * 发送问题并以流式方式写入回答
   * 状态保存在 store 中，因此组件卸载后回答仍会继续写入
   * @param {string} question 用户问题
   * @param {object} [kb] 当前知识库，用于记录这条回答是基于哪个知识库的
   *                      （对话可跨知识库延续，答案上标注来源知识库才不会混淆）
   */
  async function sendQuestion(question, kb = null) {
    const targetKbId = kb?.id ?? kbId.value
    if (!question || !targetKbId || asking.value) return

    const seq = ++streamSeq

    messages.value.push({ role: 'user', content: question })
    messages.value.push({
      role: 'ai',
      content: '',
      sources: [],
      kbId: targetKbId,
      kbName: kb?.kb_name || ''
    })
    const aiIndex = messages.value.length - 1
    asking.value = true

    try {
      const result = await askQuestionStream(
        { question, kb_id: targetKbId, session_id: sessionId.value },
        {
          onSources: (sources) => {
            if (seq !== streamSeq) return
            const msg = messages.value[aiIndex]
            if (msg) msg.sources = sources
          },
          onToken: (token) => {
            if (seq !== streamSeq) return
            const msg = messages.value[aiIndex]
            if (msg) msg.content += token
          }
        }
      )

      if (seq === streamSeq) {
        const msg = messages.value[aiIndex]
        if (msg) {
          msg.chatId = result.chat_id
          msg.feedback = 0
        }
      }
    } catch (err) {
      if (seq === streamSeq) {
        const msg = messages.value[aiIndex]
        if (msg) {
          if (err.code === 401) {
            // 登录失效：接口层已提示并跳转登录页，这里撤掉占位的空回答
            messages.value.splice(aiIndex, 1)
          } else if (!msg.content) {
            msg.content = err.fromServer
              ? err.message
              : '抱歉，服务出现异常，请稍后重试。'
          }
        }
      }
    } finally {
      if (seq === streamSeq) {
        asking.value = false
        persist()
      }
    }
  }

  /**
   * 提交回答反馈（赞 / 踩 / 取消）
   * @param {number} index 消息在 messages 中的下标
   * @param {number} value 1-赞，-1-踩，0-取消
   * @param {string} comment 可选说明
   */
  async function sendFeedback(index, value, comment = '') {
    const msg = messages.value[index]
    if (!msg || msg.role !== 'ai' || !msg.chatId) return

    const prev = { feedback: msg.feedback, comment: msg.feedbackComment }
    // 乐观更新，失败时回滚
    msg.feedback = value
    msg.feedbackComment = value === -1 ? comment : ''

    try {
      const res = await submitFeedback({
        chat_id: msg.chatId,
        feedback: value,
        comment
      })
      msg.feedback = res.data.feedback
      msg.feedbackComment = res.data.feedback_comment
      persist()
    } catch (err) {
      msg.feedback = prev.feedback
      msg.feedbackComment = prev.comment
      throw err
    }
  }

  return {
    sessionId,
    kbId,
    messages,
    asking,
    selectKb,
    reset,
    sendQuestion,
    sendFeedback,
    persist
  }
})
