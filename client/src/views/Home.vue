<template>
  <!-- 管理员首页 - 数据统计概览 -->
  <div class="home-container">
    <!-- 统计卡片区域 -->
    <el-row :gutter="20" class="stat-cards">
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-content">
            <div class="stat-info">
              <span class="stat-label">用户总数</span>
              <span class="stat-value">{{ stats.user_count }}</span>
            </div>
            <el-icon class="stat-icon" :size="48" color="#409eff"><User /></el-icon>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-content">
            <div class="stat-info">
              <span class="stat-label">知识库数量</span>
              <span class="stat-value">{{ stats.kb_count }}</span>
            </div>
            <el-icon class="stat-icon" :size="48" color="#67c23a"><FolderOpened /></el-icon>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-content">
            <div class="stat-info">
              <span class="stat-label">文档总数</span>
              <span class="stat-value">{{ stats.doc_count }}</span>
            </div>
            <el-icon class="stat-icon" :size="48" color="#e6a23c"><Document /></el-icon>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-content">
            <div class="stat-info">
              <span class="stat-label">今日提问</span>
              <span class="stat-value">{{ stats.today_chat_count }}</span>
            </div>
            <el-icon class="stat-icon" :size="48" color="#f56c6c"><ChatDotRound /></el-icon>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 图表区域 -->
    <el-row :gutter="20" class="chart-row">
      <el-col :span="14">
        <el-card shadow="hover">
          <template #header>
            <span class="card-title">近7天提问趋势</span>
          </template>
          <div ref="trendChartRef" class="chart-box"></div>
        </el-card>
      </el-col>
      <el-col :span="10">
        <el-card shadow="hover">
          <template #header>
            <span class="card-title">知识库文档占比</span>
          </template>
          <div ref="pieChartRef" class="chart-box"></div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 回答反馈概览：定位需要优化的知识库 -->
    <el-card shadow="hover">
      <template #header>
        <span class="card-title">回答反馈</span>
        <el-button
          text
          size="small"
          class="more-btn"
          @click="$router.push('/chat-history')"
        >
          查看差评记录
        </el-button>
      </template>
      <div class="feedback-row">
        <div class="feedback-item">
          <span class="feedback-label">好评率</span>
          <span class="feedback-value like">{{ stats.feedback_stats.like_rate }}%</span>
        </div>
        <div class="feedback-item">
          <span class="feedback-label">👍 好评</span>
          <span class="feedback-value">{{ stats.feedback_stats.like_count }}</span>
        </div>
        <div class="feedback-item">
          <span class="feedback-label">👎 差评</span>
          <span class="feedback-value dislike">{{ stats.feedback_stats.dislike_count }}</span>
        </div>
        <div class="feedback-item">
          <span class="feedback-label">未评价</span>
          <span class="feedback-value">{{ stats.feedback_stats.unrated_count }}</span>
        </div>
      </div>
      <div v-if="stats.feedback_stats.bad_kb_data.length" class="bad-kb">
        <span class="bad-kb-title">差评最多的知识库：</span>
        <el-tag
          v-for="item in stats.feedback_stats.bad_kb_data"
          :key="item.name"
          size="small"
          type="danger"
          class="bad-kb-tag"
        >
          {{ item.name }}（{{ item.value }}）
        </el-tag>
      </div>
      <el-empty
        v-else
        description="暂无差评数据"
        :image-size="50"
      />
    </el-card>

    <!-- 缓存概览：命中率反映重复提问被省掉的检索与生成开销 -->
    <el-card shadow="hover">
      <template #header>
        <span class="card-title">答案缓存</span>
        <span class="card-hint">命中率统计自本次服务启动以来</span>
      </template>
      <div class="feedback-row">
        <div class="feedback-item">
          <span class="feedback-label">命中率</span>
          <span class="feedback-value like">{{ stats.cache_stats.hit_rate }}%</span>
        </div>
        <div class="feedback-item">
          <span class="feedback-label">内存命中</span>
          <span class="feedback-value">{{ stats.cache_stats.l1_hit }}</span>
        </div>
        <div class="feedback-item">
          <span class="feedback-label">数据库命中</span>
          <span class="feedback-value">{{ stats.cache_stats.l2_hit }}</span>
        </div>
        <div class="feedback-item">
          <span class="feedback-label">未命中</span>
          <span class="feedback-value">{{ stats.cache_stats.miss }}</span>
        </div>
      </div>
      <div v-if="!stats.cache_stats.l2_available" class="cache-warning">
        持久层不可用，当前仅使用内存缓存（请确认已执行 sql/migrate_v3_cache.sql）
      </div>
    </el-card>
  </div>
</template>

<script setup>
/**
 * 管理员首页
 * 展示统计卡片和ECharts图表（提问趋势折线图 + 知识库文档占比饼图）
 */
import { ref, reactive, onMounted, onBeforeUnmount } from 'vue'
// ECharts 按需引入：整包 import * as echarts from 'echarts' 会把全部图表类型、组件和
// 渲染器打进产物（实测该路由分包 1.1MB / gzip 367kB）。这里只注册本页真正用到的
// 折线图、饼图与 tooltip/grid/legend 三个组件，其余交给 tree-shaking 剔除。
import * as echarts from 'echarts/core'
import { LineChart, PieChart } from 'echarts/charts'
import { TooltipComponent, GridComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { getOverview } from '../api/stats'

echarts.use([
  LineChart,
  PieChart,
  TooltipComponent,
  GridComponent,
  LegendComponent,
  CanvasRenderer
])

/** 统计数据 */
const stats = reactive({
  user_count: 0,
  kb_count: 0,
  doc_count: 0,
  today_chat_count: 0,
  trend_data: [],
  kb_doc_data: [],
  feedback_stats: {
    like_count: 0,
    dislike_count: 0,
    unrated_count: 0,
    rated_count: 0,
    like_rate: 0,
    bad_kb_data: []
  },
  cache_stats: {
    enabled: true,
    l2_available: true,
    l1_hit: 0,
    l2_hit: 0,
    miss: 0,
    total: 0,
    hit_rate: 0,
    l1_size: 0,
    l1_maxsize: 0
  }
})

/** 图表DOM引用 */
const trendChartRef = ref(null)
const pieChartRef = ref(null)
let trendChart = null
let pieChart = null

/** 加载统计数据 */
async function loadStats() {
  try {
    const res = await getOverview()
    Object.assign(stats, res.data)
    renderTrendChart()
    renderPieChart()
  } catch (err) {
    // 错误已在拦截器中处理
  }
}

/** 渲染近7天提问趋势折线图 */
function renderTrendChart() {
  if (!trendChartRef.value) return
  trendChart = echarts.init(trendChartRef.value)

  const dates = stats.trend_data.map(item => item.date)
  const counts = stats.trend_data.map(item => item.count)

  trendChart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
    xAxis: {
      type: 'category',
      data: dates,
      boundaryGap: false
    },
    yAxis: {
      type: 'value',
      minInterval: 1
    },
    series: [{
      name: '提问次数',
      type: 'line',
      smooth: true,
      data: counts,
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(64, 158, 255, 0.3)' },
          { offset: 1, color: 'rgba(64, 158, 255, 0.05)' }
        ])
      },
      lineStyle: { color: '#409eff', width: 2 },
      itemStyle: { color: '#409eff' }
    }]
  })
}

/** 渲染知识库文档占比饼图 */
function renderPieChart() {
  if (!pieChartRef.value) return
  pieChart = echarts.init(pieChartRef.value)

  const data = stats.kb_doc_data.length > 0
    ? stats.kb_doc_data
    : [{ name: '暂无数据', value: 1 }]

  pieChart.setOption({
    tooltip: {
      trigger: 'item',
      formatter: '{b}: {c} ({d}%)'
    },
    legend: {
      orient: 'vertical',
      right: '5%',
      top: 'center'
    },
    series: [{
      type: 'pie',
      radius: ['40%', '70%'],
      center: ['40%', '50%'],
      avoidLabelOverlap: false,
      label: { show: false },
      emphasis: {
        label: { show: true, fontSize: 14, fontWeight: 'bold' }
      },
      data: data
    }]
  })
}

/** 窗口大小变化时重绘图表 */
function handleResize() {
  trendChart?.resize()
  pieChart?.resize()
}

onMounted(() => {
  loadStats()
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  trendChart?.dispose()
  pieChart?.dispose()
})
</script>

<style scoped>
.home-container {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.stat-card {
  border-radius: 8px;
}

.stat-content {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.stat-info {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.stat-label {
  font-size: 14px;
  color: #909399;
}

.stat-value {
  font-size: 28px;
  font-weight: 700;
  color: #303133;
}

.stat-icon {
  opacity: 0.8;
}

.card-title {
  font-weight: 600;
  font-size: 15px;
  color: #303133;
}

/* 卡片标题右侧的补充说明，用于澄清统计口径 */
.card-hint {
  margin-left: 10px;
  font-size: 12px;
  color: #909399;
}

.cache-warning {
  margin-top: 14px;
  font-size: 12px;
  color: #e6a23c;
}

.chart-box {
  width: 100%;
  height: 320px;
}

/* 回答反馈概览 */
.more-btn {
  float: right;
  color: #409eff;
}

.feedback-row {
  display: flex;
  gap: 48px;
  flex-wrap: wrap;
}

.feedback-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.feedback-label {
  font-size: 14px;
  color: #909399;
}

.feedback-value {
  font-size: 24px;
  font-weight: 700;
  color: #303133;
}

.feedback-value.like {
  color: #67c23a;
}

.feedback-value.dislike {
  color: #f56c6c;
}

.bad-kb {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid #ebeef5;
}

.bad-kb-title {
  font-size: 13px;
  color: #909399;
  margin-right: 8px;
}

.bad-kb-tag {
  margin-right: 6px;
  margin-bottom: 6px;
}
</style>
