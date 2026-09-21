<template>
  <!-- 文档管理页面 -->
  <div class="page-container">
    <!-- 操作栏 -->
    <el-card shadow="never" class="search-bar">
      <el-row :gutter="16" align="middle">
        <el-col :span="8">
          <el-select
            v-model="queryParams.kb_id"
            placeholder="选择知识库筛选"
            clearable
            @change="loadList"
            style="width: 100%"
          >
            <el-option
              v-for="kb in kbOptions"
              :key="kb.id"
              :label="kb.kb_name"
              :value="kb.id"
            />
          </el-select>
        </el-col>
        <el-col :span="16" style="text-align: right">
          <el-button type="primary" :icon="Upload" @click="uploadVisible = true">上传文档</el-button>
        </el-col>
      </el-row>
    </el-card>

    <!-- 数据表格 -->
    <el-card shadow="never">
      <el-table :data="tableData" v-loading="loading" stripe>
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="file_name" label="文件名" min-width="200" show-overflow-tooltip />
        <el-table-column prop="kb_name" label="所属知识库" width="150" />
        <el-table-column prop="file_type" label="类型" width="80" align="center">
          <template #default="{ row }">
            <el-tag size="small">{{ row.file_type }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="100" align="center">
          <template #default="{ row }">
            {{ formatSize(row.file_size) }}
          </template>
        </el-table-column>
        <el-table-column prop="chunk_count" label="分块数" width="80" align="center" />
        <el-table-column prop="status" label="状态" width="120" align="center">
          <template #default="{ row }">
            <el-tag :type="statusMap[row.status]?.type" size="small">
              {{ statusText(row) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="create_time" label="上传时间" width="170" />
        <el-table-column label="操作" width="80" fixed="right">
          <template #default="{ row }">
            <el-popconfirm title="确认删除该文档？" @confirm="handleDelete(row.id)">
              <template #reference>
                <el-button type="danger" link>删除</el-button>
              </template>
            </el-popconfirm>
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

    <!-- 上传对话框 -->
    <el-dialog v-model="uploadVisible" title="上传文档" width="500px">
      <el-form label-width="100px">
        <el-form-item label="选择知识库" required>
          <el-select v-model="uploadKbId" placeholder="请选择知识库" style="width: 100%">
            <el-option
              v-for="kb in kbOptions"
              :key="kb.id"
              :label="kb.kb_name"
              :value="kb.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="选择文件" required>
          <el-upload
            ref="uploadRef"
            v-model:file-list="fileList"
            :auto-upload="false"
            :limit="1"
            :on-exceed="() => ElMessage.warning('只能上传一个文件')"
            accept=".txt,.pdf,.md,.docx"
          >
            <el-button type="primary" plain>选择文件</el-button>
            <template #tip>
              <div class="el-upload__tip">支持 txt、pdf、md、docx 格式，最大50MB</div>
            </template>
          </el-upload>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="handleUpload">上传</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
/**
 * 文档管理页面
 * 支持按知识库筛选文档、上传新文档和删除文档
 */
import { ref, reactive, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { Upload } from '@element-plus/icons-vue'
import { getDocList, uploadDoc, deleteDoc, getDocStatus } from '../api/document'
import { getAllKB } from '../api/knowledge'

const loading = ref(false)
const uploading = ref(false)
const uploadVisible = ref(false)
const tableData = ref([])
const total = ref(0)
const kbOptions = ref([])
const uploadKbId = ref(null)
const uploadRef = ref(null)
const fileList = ref([])

/** 向量化进度：doc_id -> { done, total, stage } */
const progressMap = reactive({})

/** 轮询配置：向量化在后台进行，需要轮询状态直到不再有"处理中"的文档 */
const POLL_INTERVAL = 2000
const MAX_POLL_ATTEMPTS = 150
let pollTimer = null
let pollAttempts = 0

/** 查询参数 */
const queryParams = reactive({ page: 1, page_size: 10, kb_id: null })

/** 文档状态映射 */
const statusMap = {
  uploading: { label: '处理中', type: 'warning' },
  vectorized: { label: '已就绪', type: 'success' },
  failed: { label: '失败', type: 'danger' }
}

/** 状态列文案：处理中的文档带上向量化进度 */
function statusText(row) {
  if (row.status !== 'uploading') {
    return statusMap[row.status]?.label ?? row.status
  }
  const progress = progressMap[row.id]
  if (!progress || !progress.total) {
    return '处理中'
  }
  return `处理中 ${progress.done}/${progress.total}`
}

/** 格式化文件大小 */
function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB'
}

/** 加载知识库下拉选项 */
async function loadKBOptions() {
  const res = await getAllKB()
  kbOptions.value = res.data
}

/** 加载文档列表 */
async function loadList() {
  loading.value = true
  try {
    const res = await getDocList(queryParams)
    tableData.value = res.data.list
    total.value = res.data.total
  } finally {
    loading.value = false
  }
}

/** 是否还有文档在后台处理 */
function hasProcessing(rows = tableData.value) {
  return rows.some(row => row.status === 'uploading')
}

/** 拉取处理中文档的向量化进度 */
async function refreshProgress() {
  const pending = tableData.value.filter(row => row.status === 'uploading')
  await Promise.all(pending.map(async row => {
    try {
      const res = await getDocStatus(row.id)
      progressMap[row.id] = res.data.progress
    } catch (err) {
      // 单次进度查询失败不影响轮询主流程
    }
  }))
}

/** 轮询期间的静默刷新：不动 loading，避免表格每2秒闪一次 */
async function refreshQuietly() {
  const res = await getDocList(queryParams)
  tableData.value = res.data.list
  total.value = res.data.total
}

/** 停止轮询 */
function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  pollAttempts = 0
}

/**
 * 启动状态轮询。
 * 上传接口现在立即返回、向量化在后台进行，因此必须轮询才能看到状态变化；
 * 同时设有次数上限，避免后台异常时无限轮询。
 */
function startPolling() {
  if (pollTimer) return
  pollAttempts = 0
  pollTimer = setInterval(async () => {
    pollAttempts += 1
    await refreshQuietly()
    await refreshProgress()
    if (!hasProcessing() || pollAttempts >= MAX_POLL_ATTEMPTS) {
      stopPolling()
    }
  }, POLL_INTERVAL)
}

/** 处理文档上传 */
async function handleUpload() {
  if (!uploadKbId.value) {
    return ElMessage.warning('请选择知识库')
  }
  if (fileList.value.length === 0) {
    return ElMessage.warning('请选择文件')
  }

  const formData = new FormData()
  formData.append('file', fileList.value[0].raw)
  formData.append('kb_id', uploadKbId.value)

  uploading.value = true
  try {
    await uploadDoc(formData)
    ElMessage.success('上传成功，正在后台向量化')
    uploadVisible.value = false
    fileList.value = []
    await loadList()
    // 只要列表里还有"处理中"的文档就开轮询
    if (hasProcessing()) {
      startPolling()
    }
  } finally {
    uploading.value = false
  }
}

/** 删除文档 */
async function handleDelete(id) {
  await deleteDoc(id)
  ElMessage.success('删除成功')
  delete progressMap[id]
  await loadList()
}

onMounted(async () => {
  loadKBOptions()
  await loadList()
  // 覆盖"上传后刷新页面"的场景：此时后台可能仍在处理
  if (hasProcessing()) {
    startPolling()
  }
})

onBeforeUnmount(stopPolling)
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
</style>
