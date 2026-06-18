<template>
  <n-space vertical :size="24">
    <n-page-header :title="t('dashboard.title')" :subtitle="t('dashboard.subtitle')" />

    <!-- System Status -->
    <n-card :title="t('dashboard.sysStatus')" size="small">
      <n-space>
        <n-tag type="success">{{ t('dashboard.apiConnected') }}</n-tag>
        <n-tag>{{ settings?.llm_provider || 'N/A' }}</n-tag>
        <n-tag>{{ settings?.deep_think_llm || 'N/A' }}</n-tag>
      </n-space>
    </n-card>

    <!-- LLM usage / cost -->
    <n-card :title="t('dashboard.usage.title')" size="small" v-if="usage">
      <n-grid :cols="4" :x-gap="12">
        <n-gi>
          <n-statistic :label="t('dashboard.usage.today')" :value="fmtNum(usage.today.tokens_total)">
            <template #suffix>
              <n-text depth="3" style="font-size: 11px">tok</n-text>
            </template>
          </n-statistic>
          <template v-if="usage.daily_budget > 0">
            <n-progress
              type="line"
              :percentage="Math.min(100, Math.round(usage.today.tokens_total / usage.daily_budget * 100))"
              :status="usage.over_budget ? 'error' : 'success'"
              :height="6"
              :show-indicator="false"
              style="margin-top: 6px"
            />
            <n-text depth="3" style="font-size: 11px">
              {{ fmtNum(usage.today.tokens_total) }} / {{ fmtNum(usage.daily_budget) }}
              <n-tag v-if="usage.over_budget" size="tiny" type="error" :bordered="false">
                {{ t('dashboard.usage.overBudget') }}
              </n-tag>
            </n-text>
          </template>
          <n-text v-else depth="3" style="font-size: 11px">{{ t('dashboard.usage.noBudget') }}</n-text>
        </n-gi>
        <n-gi>
          <n-statistic :label="t('dashboard.usage.todayCalls')" :value="usage.today.llm_calls" />
          <n-text depth="3" style="font-size: 11px">{{ t('dashboard.usage.analysesN', { n: usage.today.analyses }) }}</n-text>
        </n-gi>
        <n-gi>
          <n-statistic :label="t('dashboard.usage.month')" :value="fmtNum(usage.month.tokens_total)">
            <template #suffix><n-text depth="3" style="font-size: 11px">tok</n-text></template>
          </n-statistic>
        </n-gi>
        <n-gi>
          <n-statistic :label="t('dashboard.usage.all')" :value="fmtNum(usage.all.tokens_total)">
            <template #suffix><n-text depth="3" style="font-size: 11px">tok</n-text></template>
          </n-statistic>
        </n-gi>
      </n-grid>
    </n-card>

    <!-- Quick Action -->
    <n-button type="primary" size="large" @click="$router.push('/analyze')">
      {{ t('dashboard.quickNew') }}
    </n-button>

    <!-- Recent Analyses -->
    <n-card :title="t('dashboard.recent')">
      <n-grid :cols="5" :x-gap="12" v-if="store.recent.length">
        <n-gi v-for="item in store.recent" :key="item.id">
          <n-card size="small" hoverable @click="$router.push(`/report/${item.id}`)">
            <n-statistic :label="item.ticker" :value="item.signal || item.status" />
            <n-text depth="3" style="font-size: 12px">{{ item.trade_date }}</n-text>
          </n-card>
        </n-gi>
      </n-grid>
      <n-empty v-else :description="t('dashboard.noRecent')" />
    </n-card>

    <!-- Signal Distribution -->
    <n-card :title="t('dashboard.signalDist')" v-if="Object.keys(store.signalDistribution).length > 0">
      <div style="max-width: 300px">
        <Pie :data="chartData" :options="{ responsive: true, maintainAspectRatio: true }" />
      </div>
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { onMounted, computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Pie } from 'vue-chartjs'
import { Chart as ChartJS, ArcElement, Tooltip, Legend } from 'chart.js'
import { useAnalysisStore } from '../stores/analysis'
import { useSettingsStore } from '../stores/settings'
import api from '../api'

ChartJS.register(ArcElement, Tooltip, Legend)

const { t } = useI18n()
const store = useAnalysisStore()
const settingsStore = useSettingsStore()
const settings = computed(() => settingsStore.settings)

const usage = ref<any>(null)
function fmtNum(n: number): string {
  return (n ?? 0).toLocaleString()
}

const chartData = computed(() => {
  const dist = store.signalDistribution
  if (!dist || Object.keys(dist).length === 0) {
    return { labels: [], datasets: [] }
  }
  return {
    labels: Object.keys(dist),
    datasets: [{
      data: Object.values(dist),
      backgroundColor: ['#18a058', '#f0a020', '#d03050'],
    }],
  }
})

async function fetchUsage() {
  try {
    const { data } = await api.get('/api/usage')
    usage.value = data
  } catch { /* non-fatal */ }
}

onMounted(() => {
  store.fetchDashboard()
  settingsStore.fetch()
  fetchUsage()
})
</script>
