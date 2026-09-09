/**
 * 问答对话API
 */
import request from './index'

/** 发送问题（RAG问答） */
export function askQuestion(data) {
  return request.post('/chat/ask', data, {
    timeout: 3600000
  })
}

/**
 * 流式问答
 * 使用 fetch 读取 NDJSON 流，逐行解析后端事件：
 *  - sources: 参考来源
 *  - token: 回答片段
 *  - done: 完成（携带最终 answer/source_docs/session_id/chat_id）
 *  - error: 出错
 * @param {object} data 请求参数 { question, kb_id, session_id }
 * @param {object} handlers 回调 { onSources, onToken }
 * @returns {Promise<object>} resolve 为 done 事件的 data
 */
export async function askQuestionStream(data, { onSources, onToken } = {}) {
  const token = localStorage.getItem('token')
  const res = await fetch('/api/chat/ask/stream', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {})
    },
    body: JSON.stringify(data)
  })

  // 非流式错误响应（如 401/400），解析统一错误结构
  if (!res.ok || res.status !== 200) {
    let message = '网络异常'
    try {
      const body = await res.json()
      message = body.message || message
    } catch (e) {
      // 忽略解析失败，使用默认错误信息
    }
    throw new Error(message)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let finalData = null
  let errorMsg = null

  const handleLine = (line) => {
    let event
    try {
      event = JSON.parse(line)
    } catch (e) {
      return
    }
    if (event.type === 'sources') {
      onSources && onSources(event.data)
    } else if (event.type === 'token') {
      onToken && onToken(event.data)
    } else if (event.type === 'done') {
      finalData = event.data
    } else if (event.type === 'error') {
      errorMsg = event.data
    }
  }

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, idx).trim()
      buffer = buffer.slice(idx + 1)
      if (line) handleLine(line)
    }
  }

  // 处理末尾可能未换行的最后一行
  const rest = buffer.trim()
  if (rest) handleLine(rest)

  if (errorMsg) throw new Error(errorMsg)
  if (!finalData) throw new Error('响应异常结束')

  return finalData
}

/** 获取对话历史列表 */
export function getChatHistory(params) {
  return request.get('/chat/history', { params })
}

/** 获取指定会话的对话记录 */
export function getSessionChats(sessionId) {
  return request.get(`/chat/session/${sessionId}`)
}
