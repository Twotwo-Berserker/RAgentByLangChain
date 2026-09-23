<template>
  <!-- 对话历史页面 -->
  <div class="page-container">
    <!-- 筛选栏 -->
    <el-card shadow="never">
      <el-row :gutter="16" align="middle">
        <el-col :span="filterSpan">
          <el-select
            v-model="queryParams.kb_id"
            placeholder="按知识库筛选"
            clearable
            @change="handleFilterChange"
            style="width: 100%"
          >
            <!-- 显式的「全部」选项，筛选后可以一键回到全选状态 -->
            <el-option label="全部知识库" :value="null" />
            <el-option
              v-for="kb in kbOptions"
              :key="kb.id"
              :label="kb.kb_name"
              :value="kb.id"
            />
          </el-select>
        </el-col>
        <el-col :span="filterSpan">
          <el-select
            v-model="queryParams.feedback"
            placeholder="按反馈筛选"
            clearable
            @change="handleFilterChange"
            style="width: 100%"
          >
            <el-option label="全部反馈" :value="null" />
            <el-option label="👍 好评" :value="1" />
            <el-option label="👎 差评" :value="-1" />
            <el-option label="未评价" :value="0" />
          </el-select>
        </el-col>
        <!-- 按提问者筛选：仅管理员可见，普通用户只能看到自己的记录，没有筛选的必要 -->
        <el-col v-if="isAdmin" :span="filterSpan">
          <el-select
            v-model="queryParams.user_id"
            placeholder="按提问者筛选"
            clearable
            filterable
            @change="handleFilterChange"
            style="width: 100%"
          >
            <el-option label="全部提问者" :value="null" />
            <el-option
              v-for="u in userOptions"
              :key="u.id"
              :label="u.nickname"
              :value="u.id"
            />
          </el-select>
        </el-col>
      </el-row>
    </el-card>

    <!-- 历史记录表格 -->
    <el-card shadow="never">
      <el-table
        :data="tableData"
        v-loading="loading"
        stripe
        :row-class-name="rowClassName"
      >
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="question" label="问题" min-width="220" show-overflow-tooltip />
        <el-table-column prop="answer" label="回答" min-width="260" show-overflow-tooltip />
        <el-table-column prop="kb_name" label="知识库" width="130" />
        <el-table-column prop="username" label="提问者" width="100" />
        <el-table-column label="反馈" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.feedback === 1" size="small" type="primary">👍 好评</el-tag>
            <el-tag v-else-if="row.feedback === -1" size="small" type="danger">👎 差评</el-tag>
            <span v-else class="text-muted">未评价</span>
          </template>
        </el-table-column>
        <!-- 差评原因：定位"待优化"问答的直接线索，不用点开详情就能看到 -->
        <el-table-column label="差评原因" width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.feedback === -1" class="dislike-reason">
              {{ row.feedback_comment || '未填写原因' }}
            </span>
            <span v-else class="text-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="create_time" label="时间" width="170" />
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link @click="showDetail(row)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination">
        <el-pagination
          v-model:current-page="queryParams.page"
          v-model:page-size="queryParams.page_size"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next"
          @change="loadList"
        />
      </div>
    </el-card>

    <!-- 详情对话框 -->
    <el-dialog v-model="detailVisible" title="对话详情" width="650px">
      <div class="detail-content" v-if="currentChat">
        <div class="detail-item">
          <div class="detail-label">提问：</div>
          <div class="detail-value question">{{ currentChat.question }}</div>
        </div>
        <div class="detail-item">
          <div class="detail-label">回答：</div>
          <!-- 与问答页一致：按 Markdown 呈现，[来源N] 角标点击展开对应来源原文 -->
          <div class="detail-value answer md-body" v-html="answerHtml" @click="onAnswerClick"></div>
        </div>
        <div class="detail-item" v-if="currentChat.source_docs?.length">
          <div class="detail-label">参考来源：</div>
          <div class="detail-value">
            <!-- 知识溯源：点击来源标签可展开原文，答案中引用了该来源的内容会高亮 -->
            <el-collapse v-model="activeSources" class="source-collapse">
              <el-collapse-item
                v-for="(src, i) in currentChat.source_docs"
                :key="i"
                :name="i"
              >
                <template #title>
                  <el-tag size="small" class="source-tag">来源{{ src.index ?? i + 1 }}</el-tag>
                  <span class="source-file">{{ src.file_name }}</span>
                </template>
                <div class="source-content">{{ src.content }}</div>
              </el-collapse-item>
            </el-collapse>
          </div>
        </div>
        <div class="detail-item">
          <div class="detail-label">反馈：</div>
          <div class="detail-value">
            <el-tag v-if="currentChat.feedback === 1" size="small" type="primary">👍 好评</el-tag>
            <el-tag v-else-if="currentChat.feedback === -1" size="small" type="danger">👎 差评</el-tag>
            <span v-else class="text-muted">未评价</span>
            <span v-if="currentChat.feedback_comment" class="feedback-comment">
              {{ currentChat.feedback_comment }}
            </span>
          </div>
        </div>
        <div class="detail-item">
          <div class="detail-label">知识库：</div>
          <div class="detail-value">{{ currentChat.kb_name }}</div>
        </div>
        <div class="detail-item">
          <div class="detail-label">时间：</div>
          <div class="detail-value">{{ currentChat.create_time }}</div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
/**
 * 对话历史页面
 * 展示用户的历史问答记录，支持按知识库 / 反馈筛选（管理员可再按提问者筛选）和查看详情
 * 每个筛选项都带一条「全部…」选项，筛选后能一键回到全选状态
 */
import { ref, reactive, computed, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { getChatHistory } from '../api/chat'
import { getAllKB } from '../api/knowledge'
import { getUserOptions } from '../api/user'
import { useUserStore } from '../stores/user'
import { renderMarkdown, CITE_CLASS } from '../utils/markdown'

const userStore = useUserStore()
const { isAdmin } = storeToRefs(userStore)

const loading = ref(false)
const detailVisible = ref(false)
const tableData = ref([])
const total = ref(0)
const kbOptions = ref([])
const userOptions = ref([])
const currentChat = ref(null)
/** 详情里已展开原文的来源（el-collapse 用数组下标作 name） */
const activeSources = ref([])

/** 详情中的回答按 Markdown 渲染（历史记录里存的是原始 Markdown 文本） */
const answerHtml = computed(() =>
  currentChat.value
    ? renderMarkdown(currentChat.value.answer || '', {
        sources: currentChat.value.source_docs || []
      }).html
    : ''
)

const queryParams = reactive({
  page: 1,
  page_size: 10,
  kb_id: null,
  feedback: null,
  user_id: null
})

/** 筛选栏每列宽度：管理员多一列「按提问者筛选」 */
const filterSpan = computed(() => (isAdmin.value ? 6 : 8))

/**
 * 组装查询参数
 * 空值（null / 空串）不下发，避免「全部」选项被当成筛选条件传给后端
 */
function buildParams() {
  const params = { page: queryParams.page, page_size: queryParams.page_size }
  if (queryParams.kb_id !== null && queryParams.kb_id !== '') {
    params.kb_id = queryParams.kb_id
  }
  if (queryParams.feedback !== null && queryParams.feedback !== '') {
    params.feedback = queryParams.feedback
  }
  // 非管理员不传该参数，后端也只认管理员
  if (isAdmin.value && queryParams.user_id) {
    params.user_id = queryParams.user_id
  }
  return params
}

/**
 * 切换筛选条件
 * 必须回到第一页，否则可能停在超出新结果范围的页码上，看到空列表
 */
function handleFilterChange() {
  queryParams.page = 1
  loadList()
}

async function loadKBOptions() {
  const res = await getAllKB()
  kbOptions.value = res.data
}

/** 管理员：加载提问者下拉选项 */
async function loadUserOptions() {
  if (!isAdmin.value) return
  try {
    const res = await getUserOptions()
    userOptions.value = res.data
  } catch (err) {
    // 选项加载失败不影响历史列表展示，下拉里只剩「全部提问者」
  }
}

async function loadList() {
  loading.value = true
  try {
    const res = await getChatHistory(buildParams())
    tableData.value = res.data.list
    total.value = res.data.total
  } finally {
    loading.value = false
  }
}

/** 差评行加淡红底色，方便在长列表里快速定位待优化的问答 */
function rowClassName({ row }) {
  return row.feedback === -1 ? 'row-dislike' : ''
}

function showDetail(row) {
  currentChat.value = row
  activeSources.value = []
  detailVisible.value = true
}

/** 点击回答中的 [来源N] 角标：展开对应来源的原文 */
function onAnswerClick(e) {
  const badge = e.target.closest?.(`.${CITE_CLASS}`)
  if (!badge) return
  const index = Number(badge.dataset.cite)
  const sources = currentChat.value?.source_docs || []
  const pos = sources.findIndex((src, i) => (src.index ?? i + 1) === index)
  if (pos === -1 || activeSources.value.includes(pos)) return
  activeSources.value = [...activeSources.value, pos]
}

onMounted(() => {
  loadKBOptions()
  loadUserOptions()
  loadList()
})
</script>

<style scoped>
.page-container {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.detail-content {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.detail-item {
  display: flex;
  gap: 8px;
}

.detail-label {
  font-weight: 600;
  color: #303133;
  white-space: nowrap;
  min-width: 70px;
}

.detail-value {
  color: #606266;
  line-height: 1.6;
  word-break: break-all;
}

.detail-value.question {
  color: #409eff;
  font-weight: 500;
}

/* 回答由 Markdown 渲染，排版交给 .md-body（assets/markdown.css） */
.detail-value.answer {
  flex: 1;
  min-width: 0;
  background: #f5f7fa;
  padding: 12px;
  border-radius: 6px;
}

.source-tag {
  margin-right: 6px;
  margin-bottom: 4px;
}

.source-file {
  font-size: 13px;
  color: #303133;
}

.source-collapse {
  border-top: none;
}

.source-content {
  font-size: 13px;
  line-height: 1.8;
  color: #606266;
  background: #fafafa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 10px;
  white-space: pre-wrap;
  word-break: break-word;
}

.text-muted {
  color: #c0c4cc;
}

.dislike-reason {
  color: #f56c6c;
  font-size: 13px;
}

/* 差评行底色。斑马纹会给单元格单独上背景色，优先级高于行上的 class，
   所以这里用 !important 覆盖，保证差评行始终能被一眼看到 */
:deep(.el-table__body tr.row-dislike > td.el-table__cell) {
  background-color: #fef0f0 !important;
}

.feedback-comment {
  margin-left: 8px;
  color: #f56c6c;
  font-size: 13px;
}
</style>
