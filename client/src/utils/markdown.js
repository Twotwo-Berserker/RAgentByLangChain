/**
 * 回答内容 Markdown 渲染
 *
 * 模型返回的回答本身就是 Markdown（标题、有序/无序列表、表格、代码块、加粗等），
 * 按纯文本展示会把语法符号一并显示出来、多行列表也会挤成一坨。
 * 这里用 marked 转成 HTML，渲染前经 DOMPurify 消毒：回答内容来自模型，属于不可信输入，
 * 直接 v-html 会有 XSS 风险（模型可能被知识库里的文档内容诱导输出脚本）。
 *
 * 同时保留知识溯源能力：
 *  - [来源N] 转成可点击角标（点击后由调用方打开溯源抽屉）；
 *  - 角标所在的整块内容加 is-cited 标记（淡黄底纹），体现"这句答案有出处"；
 *  - 返回 citeMap（来源编号 -> 引用它的那段文本），供调用方在原文里定位被引用的片段。
 */
import { marked } from 'marked'
import DOMPurify from 'dompurify'

/** 引用标记：允许 [来源1] / [来源 1] 两种写法 */
const CITE_RE = /\[来源\s*(\d+)\]/g

/** 引用角标类名 */
export const CITE_CLASS = 'cite-badge'

/** 被引用内容所在块的标记类名 */
export const CITED_CLASS = 'is-cited'

/** 会被打上 is-cited 的块级元素：整句底纹打在最小的完整块上，避免染色范围过大 */
const BLOCK_SELECTOR = 'p, li, td, th, h1, h2, h3, h4, h5, h6, blockquote, dt, dd'

/** 渲染选项：gfm 支持表格/删除线，breaks 让单个换行也生效（模型常靠单个换行分行） */
const MARKED_OPTIONS = { gfm: true, breaks: true }

/**
 * 消毒配置
 * img：知识库文档切分后只剩纯文本，模型给出的图片地址无法对应真实配图，
 *      却会让浏览器去访问外部地址（可被用作追踪像素），直接禁止；
 * style 属性：模型若输出行内样式，容易把气泡排版撑坏，一并去掉
 */
const SANITIZE_OPTIONS = { FORBID_TAGS: ['img'], FORBID_ATTR: ['style'] }

/** 外部链接一律新窗口打开，避免把问答页面顶掉 */
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A' && node.getAttribute('href')) {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

/**
 * 渲染回答内容为可安全 v-html 的 HTML
 * @param {string} content Markdown 文本
 * @param {object} [options]
 * @param {Array} [options.sources] 参考来源列表，用于校验 [来源N] 是否有效
 * @param {boolean} [options.streaming] 是否正在流式输出（未闭合的代码块需要补全）
 * @returns {{ html: string, citeMap: Object<number, string> }}
 *          html 可直接用于 v-html；citeMap 为「来源编号 -> 引用它的那段文本」
 */
export function renderMarkdown(content, { sources = [], streaming = false } = {}) {
  if (!content) return { html: '', citeMap: {} }

  let text = content
  // 流式输出时代码围栏（```）可能只写了一半，补上收尾标记，
  // 否则后半段回答会被当成代码块，界面上是一片等宽字体
  if (streaming && (text.match(/```/g) || []).length % 2 === 1) {
    text += '\n```'
  }

  const root = document.createElement('div')
  root.innerHTML = DOMPurify.sanitize(marked.parse(text, MARKED_OPTIONS), SANITIZE_OPTIONS)

  decorateCites(root, sources)
  // 先标记、再取 innerHTML：is-cited 是加在块级元素上的，必须序列化前完成
  const citeMap = markCitedBlocks(root)

  return { html: root.innerHTML, citeMap }
}

/**
 * 把文本节点里的 [来源N] 换成可点击角标
 *
 * 之所以在渲染后的 DOM 上做、而不是在 Markdown 源码里替换：
 * 直接拼 HTML 字符串会被 [来源N] 所在位置的 Markdown 语法影响，
 * 且在文本节点上处理可以顺手跳过代码块里的示例文本。
 * @param {HTMLElement} root 已消毒的渲染结果
 * @param {Array} sources 参考来源列表
 */
function decorateCites(root, sources) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const textNodes = []
  let node
  while ((node = walker.nextNode())) {
    textNodes.push(node)
  }

  textNodes.forEach((textNode) => {
    const parent = textNode.parentElement
    // 代码块里的 [来源1] 是文档示例，不做成角标
    if (!parent || parent.closest('code, pre')) return

    const text = textNode.nodeValue
    CITE_RE.lastIndex = 0
    let matched = false
    let last = 0
    let m
    const frag = document.createDocumentFragment()

    while ((m = CITE_RE.exec(text)) !== null) {
      const index = Number(m[1])
      const source = sources[index - 1]
      // 无效引用：模型可能写出检索结果之外的编号（如 [来源9]），点开无内容可看，原样保留
      if (!source) continue

      frag.appendChild(document.createTextNode(text.slice(last, m.index)))
      const badge = document.createElement('span')
      badge.className = CITE_CLASS
      badge.dataset.cite = String(index)
      badge.textContent = m[0]
      badge.title = `查看原文：${source.file_name || `来源${index}`}`
      frag.appendChild(badge)

      last = m.index + m[0].length
      matched = true
    }

    if (!matched) return
    frag.appendChild(document.createTextNode(text.slice(last)))
    textNode.parentNode.replaceChild(frag, textNode)
  })
}

/**
 * 标记被引用内容所在的块，并记录每个来源第一次被引用时的那段文本
 * @param {HTMLElement} root 已插入角标的渲染结果
 * @returns {Object<number, string>} 来源编号 -> 引用它的那段文本
 */
function markCitedBlocks(root) {
  const citeMap = {}
  root.querySelectorAll(`.${CITE_CLASS}`).forEach((badge) => {
    const block = badge.closest(BLOCK_SELECTOR)
    if (!block) return

    block.classList.add(CITED_CLASS)

    const index = Number(badge.dataset.cite)
    if (citeMap[index] === undefined) {
      // 取整块文本作为"被引用的句子"，调用方再据此在原文里做最长公共子串定位
      citeMap[index] = block.textContent
    }
  })
  return citeMap
}
