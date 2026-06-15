<template>
  <n-space vertical :size="24">
    <n-page-header :title="t('schedule.title')" :subtitle="t('schedule.subtitle')">
      <template #extra>
        <n-button type="primary" @click="openCreate">{{ t('schedule.newTask') }}</n-button>
      </template>
    </n-page-header>

    <n-alert type="info" :show-icon="false">
      {{ t('schedule.info') }}
    </n-alert>

    <n-spin :show="loading">
      <n-card>
        <n-data-table
          :columns="columns"
          :data="schedules"
          :pagination="{ pageSize: 20 }"
          :bordered="false"
          size="small"
        />
        <n-empty v-if="!schedules.length" :description="t('schedule.empty')" />
      </n-card>
    </n-spin>

    <!-- Screen schedules: auto-rotating analysis pool -->
    <n-page-header :title="t('screenSchedule.sectionTitle')">
      <template #extra>
        <n-button type="primary" @click="openScreenCreate">{{ t('screenSchedule.newBtn') }}</n-button>
      </template>
    </n-page-header>
    <n-alert type="info" :show-icon="false">{{ t('screenSchedule.sectionDesc') }}</n-alert>
    <n-spin :show="ssLoading">
      <n-card>
        <n-data-table
          :columns="ssColumns"
          :data="screenSchedules"
          :pagination="{ pageSize: 10 }"
          :bordered="false"
          size="small"
        />
        <n-empty v-if="!screenSchedules.length" :description="t('screenSchedule.empty')" />
      </n-card>
    </n-spin>

    <!-- Screen schedule create/edit modal -->
    <n-modal v-model:show="showSsEdit" preset="card" :title="ssEditingId ? t('screenSchedule.editTitle') : t('screenSchedule.createTitle')" style="width: 560px">
      <n-form label-placement="left" label-width="110">
        <n-form-item :label="t('screenSchedule.fields.name')">
          <n-input v-model:value="ssForm.name" :placeholder="t('screenSchedule.fields.namePlaceholder')" />
        </n-form-item>
        <n-form-item :label="t('screenSchedule.fields.goal')">
          <n-input v-model:value="ssForm.text" type="textarea" :autosize="{ minRows: 1, maxRows: 3 }" :placeholder="t('screenSchedule.fields.goalPlaceholder')" />
        </n-form-item>
        <n-form-item :label="t('screenSchedule.fields.topN')">
          <n-space align="center">
            <n-input-number v-model:value="ssForm.top_n" :min="5" :max="500" :step="5" style="width: 130px" />
            <n-checkbox v-model:checked="ssForm.use_llm">{{ t('screenSchedule.fields.useLlm') }}</n-checkbox>
          </n-space>
        </n-form-item>
        <n-form-item :label="t('screenSchedule.fields.screenCadence')">
          <n-radio-group v-model:value="ssForm.schedule_type">
            <n-radio value="daily">{{ t('screenSchedule.cadence.daily') }}</n-radio>
            <n-radio value="weekly">{{ t('screenSchedule.cadence.weekly') }}</n-radio>
          </n-radio-group>
        </n-form-item>
        <n-form-item :label="t('screenSchedule.fields.screenTime')">
          <n-space align="center">
            <n-time-picker v-model:formatted-value="ssForm.time_of_day" format="HH:mm" value-format="HH:mm" placeholder="HH:mm" />
            <n-select v-if="ssForm.schedule_type === 'weekly'" v-model:value="ssForm.day_of_week" :options="dowOptions" style="width: 110px" />
          </n-space>
        </n-form-item>
        <n-form-item :label="t('screenSchedule.fields.subCadence')">
          <n-radio-group v-model:value="ssForm.sub_schedule_type">
            <n-radio value="interval">{{ t('screenSchedule.cadence.interval') }}</n-radio>
            <n-radio value="daily">{{ t('screenSchedule.cadence.daily') }}</n-radio>
            <n-radio value="weekly">{{ t('screenSchedule.cadence.weekly') }}</n-radio>
          </n-radio-group>
        </n-form-item>
        <n-form-item v-if="ssForm.sub_schedule_type === 'interval'" :label="t('screenSchedule.fields.subInterval')">
          <n-input-number v-model:value="ssForm.sub_interval_minutes" :min="5" :step="5" style="width: 130px" />
        </n-form-item>
        <n-form-item v-else :label="t('screenSchedule.fields.subTime')">
          <n-space align="center">
            <n-time-picker v-model:formatted-value="ssForm.sub_time_of_day" format="HH:mm" value-format="HH:mm" placeholder="HH:mm" />
            <n-select v-if="ssForm.sub_schedule_type === 'weekly'" v-model:value="ssForm.sub_day_of_week" :options="dowOptions" style="width: 110px" />
          </n-space>
        </n-form-item>
        <n-form-item :label="t('schedule.fields.enableAnalysts')">
          <n-checkbox-group v-model:value="ssForm.analysts">
            <n-space>
              <n-checkbox value="market">{{ t('holdings.schedule.analystMarket') }}</n-checkbox>
              <n-checkbox value="news">{{ t('holdings.schedule.analystNews') }}</n-checkbox>
              <n-checkbox value="fundamentals">{{ t('holdings.schedule.analystFundamentals') }}</n-checkbox>
              <n-checkbox value="social">{{ t('holdings.schedule.analystSocial') }}</n-checkbox>
              <n-checkbox value="event">{{ t('holdings.schedule.analystEvent') }}</n-checkbox>
            </n-space>
          </n-checkbox-group>
        </n-form-item>
        <n-form-item :label="t('screenSchedule.fields.evictAfter')">
          <n-space vertical :size="2" style="width: 100%">
            <n-input-number v-model:value="ssForm.evict_after_misses" :min="1" :max="20" style="width: 130px" />
            <n-text depth="3" style="font-size: 12px">{{ t('screenSchedule.fields.evictAfterHint') }}</n-text>
          </n-space>
        </n-form-item>
        <n-form-item :label="t('screenSchedule.fields.maxPool')">
          <n-space vertical :size="2" style="width: 100%">
            <n-input-number v-model:value="ssForm.max_pool_size" :min="1" :max="500" clearable style="width: 130px" />
            <n-text depth="3" style="font-size: 12px">{{ t('screenSchedule.fields.maxPoolHint') }}</n-text>
          </n-space>
        </n-form-item>
        <n-form-item :label="t('screenSchedule.fields.autoTrade')">
          <n-space vertical :size="4" style="width: 100%">
            <n-switch v-model:value="ssForm.auto_trade" />
            <n-text depth="3" style="font-size: 12px">{{ t('screenSchedule.fields.autoTradeHint') }}</n-text>
          </n-space>
        </n-form-item>
        <n-form-item v-if="ssForm.auto_trade" :label="t('screenSchedule.fields.autoTradeCashPct')">
          <n-input-number v-model:value="ssForm.auto_trade_cash_pct" :min="1" :max="100" :step="5">
            <template #suffix>%</template>
          </n-input-number>
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showSsEdit = false">{{ t('common.cancel') }}</n-button>
          <n-button type="primary" :loading="ssSaving" @click="saveScreen">{{ t('common.save') }}</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- Managed pool modal -->
    <n-modal v-model:show="showPool" preset="card" :title="t('screenSchedule.poolTitle')" style="width: 480px">
      <n-data-table
        v-if="poolItems.length"
        :columns="poolColumns"
        :data="poolItems"
        :pagination="false"
        :bordered="false"
        size="small"
      />
      <n-empty v-else :description="t('screenSchedule.poolEmpty')" />
    </n-modal>

    <!-- Create / Edit modal -->
    <n-modal v-model:show="showEdit" preset="card" :title="editingId ? t('schedule.editTitle') : t('schedule.createTitle')" style="width: 560px">
      <n-form label-placement="left" label-width="100">
        <n-form-item :label="t('schedule.fields.name')">
          <n-input v-model:value="form.name" :placeholder="t('schedule.fields.namePlaceholder')" />
        </n-form-item>
        <n-form-item :label="t('common.ticker')">
          <n-input v-model:value="form.ticker" :placeholder="t('schedule.fields.tickerPlaceholder')" :disabled="!!editingId" />
        </n-form-item>
        <n-form-item :label="t('schedule.fields.assetType')">
          <n-radio-group v-model:value="form.asset_type" :disabled="!!editingId">
            <n-radio value="stock">{{ t('common.stock') }}</n-radio>
            <n-radio value="crypto">{{ t('common.crypto') }}</n-radio>
          </n-radio-group>
        </n-form-item>
        <n-form-item :label="t('schedule.fields.triggerType')">
          <n-radio-group v-model:value="form.schedule_type">
            <n-radio value="interval">{{ t('schedule.triggerTypes.interval') }}</n-radio>
            <n-radio value="daily">{{ t('schedule.triggerTypes.daily') }}</n-radio>
            <n-radio value="weekly">{{ t('schedule.triggerTypes.weekly') }}</n-radio>
          </n-radio-group>
        </n-form-item>
        <n-form-item v-if="form.schedule_type === 'interval'" :label="t('schedule.fields.intervalMinutes')">
          <n-input-number v-model:value="form.interval_minutes" :min="5" :step="5" :placeholder="t('schedule.fields.intervalPlaceholder')" />
        </n-form-item>
        <n-form-item v-if="form.schedule_type !== 'interval'" :label="t('schedule.fields.timeOfDay')">
          <n-time-picker v-model:formatted-value="form.time_of_day" format="HH:mm" value-format="HH:mm" placeholder="HH:mm" />
        </n-form-item>
        <n-form-item v-if="form.schedule_type === 'weekly'" :label="t('schedule.fields.dayOfWeek')">
          <n-select
            v-model:value="form.day_of_week"
            :options="dowOptions"
            :placeholder="t('schedule.fields.dowPlaceholder')"
          />
        </n-form-item>
        <n-form-item :label="t('schedule.fields.enableAnalysts')">
          <n-checkbox-group v-model:value="form.analysts">
            <n-space>
              <n-checkbox value="market">{{ t('holdings.schedule.analystMarket') }}</n-checkbox>
              <n-checkbox value="news">{{ t('holdings.schedule.analystNews') }}</n-checkbox>
              <n-checkbox value="fundamentals">{{ t('holdings.schedule.analystFundamentals') }}</n-checkbox>
              <n-checkbox value="social">{{ t('holdings.schedule.analystSocial') }}</n-checkbox>
              <n-checkbox value="cn_social">{{ t('holdings.schedule.analystCnSocial') }}</n-checkbox>
              <n-checkbox value="event">{{ t('holdings.schedule.analystEvent') }}</n-checkbox>
            </n-space>
          </n-checkbox-group>
        </n-form-item>
        <n-form-item :label="t('schedule.fields.debateRounds')">
          <n-space>
            <n-input-number v-model:value="form.max_debate_rounds" :min="1" :max="3" />
            <n-text depth="3">{{ t('schedule.fields.researchDebate') }}</n-text>
            <n-input-number v-model:value="form.max_risk_discuss_rounds" :min="1" :max="3" />
            <n-text depth="3">{{ t('schedule.fields.riskDebate') }}</n-text>
          </n-space>
        </n-form-item>
        <n-form-item :label="t('schedule.fields.autoTrade')">
          <n-space vertical :size="4" style="width: 100%">
            <n-switch v-model:value="form.auto_trade" />
            <n-text depth="3" style="font-size: 12px">{{ t('schedule.fields.autoTradeHint') }}</n-text>
          </n-space>
        </n-form-item>
        <n-form-item v-if="form.auto_trade" :label="t('schedule.fields.autoTradeCashPct')">
          <n-input-number
            v-model:value="form.auto_trade_cash_pct"
            :min="1"
            :max="100"
            :step="5"
          >
            <template #suffix>%</template>
          </n-input-number>
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showEdit = false">{{ t('common.cancel') }}</n-button>
          <n-button type="primary" :loading="saving" @click="save">{{ t('common.save') }}</n-button>
        </n-space>
      </template>
    </n-modal>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, h } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { useMessage, useDialog, NButton, NSpace, NTag, NText } from 'naive-ui'
import api from '../api'
import { formatDateTime } from '../utils/datetime'

interface Schedule {
  id: number
  name: string | null
  ticker: string
  asset_type: string
  schedule_type: 'interval' | 'daily' | 'weekly'
  interval_minutes: number | null
  time_of_day: string | null
  day_of_week: number | null
  analysts: string  // JSON string from backend
  config_json: string
  status: 'active' | 'paused' | 'disabled'
  fail_count: number
  last_run_at: string | null
  last_analysis_id: string | null
  next_run_at: string
  from_holding: number
  auto_trade: number
  auto_trade_cash_fraction: number | null
}

const { t } = useI18n()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()

const loading = ref(false)
const schedules = ref<Schedule[]>([])
const showEdit = ref(false)
const editingId = ref<number | null>(null)
const saving = ref(false)

const form = reactive({
  name: '' as string | null,
  ticker: '',
  asset_type: 'stock',
  schedule_type: 'daily' as 'interval' | 'daily' | 'weekly',
  interval_minutes: 60 as number | null,
  time_of_day: '09:30' as string | null,
  day_of_week: 0 as number | null,
  analysts: ['market', 'news', 'fundamentals'] as string[],
  max_debate_rounds: 1,
  max_risk_discuss_rounds: 1,
  auto_trade: false,
  auto_trade_cash_pct: 10,
})

const DOW_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] as const

const dowOptions = computed(() =>
  DOW_KEYS.map((k, i) => ({ label: t(`schedule.days.${k}`), value: i })),
)

const statusLabel = computed<Record<string, { label: string; type: 'success' | 'warning' | 'error' | 'default' }>>(() => ({
  active: { label: t('schedule.status.active'), type: 'success' },
  paused: { label: t('schedule.status.paused'), type: 'warning' },
  disabled: { label: t('schedule.status.disabled'), type: 'error' },
}))

function describePattern(s: Schedule): string {
  if (s.schedule_type === 'interval') {
    return t('schedule.pattern.interval', { n: s.interval_minutes })
  }
  if (s.schedule_type === 'daily') {
    return t('schedule.pattern.daily', { time: s.time_of_day })
  }
  const dow = dowOptions.value.find(d => d.value === s.day_of_week)?.label || ''
  return t('schedule.pattern.weekly', { dow, time: s.time_of_day })
}

function formatDate(s: string | null): string {
  // Was a raw ISO slice (UTC, off by the local offset). Use the shared
  // local-time formatter and trim the seconds for a compact schedule cell.
  if (!s) return '—'
  return formatDateTime(s).slice(0, 16)
}

const columns = computed(() => [
  { title: t('schedule.cols.name'), key: 'name', width: 140, render(r: Schedule) { return r.name || r.ticker } },
  { title: t('schedule.cols.ticker'), key: 'ticker', width: 100 },
  {
    title: t('schedule.cols.trigger'),
    key: 'pattern',
    width: 160,
    render(r: Schedule) {
      const parts: any[] = [describePattern(r)]
      if (r.auto_trade) {
        parts.push(
          h(NTag, { size: 'tiny', type: 'warning', bordered: false, style: { marginLeft: '6px' } },
            () => t('schedule.autoTradeBadge')),
        )
      }
      return h(NSpace, { size: 2, align: 'center', wrapItem: false }, () => parts)
    },
  },
  {
    title: t('schedule.cols.analysts'),
    key: 'analysts',
    width: 200,
    render(r: Schedule) {
      let arr: string[] = []
      try { arr = JSON.parse(r.analysts) } catch { /* ignore */ }
      return arr.join(', ')
    },
  },
  {
    title: t('schedule.cols.status'),
    key: 'status',
    width: 110,
    render(r: Schedule) {
      const cfg = statusLabel.value[r.status] || { label: r.status, type: 'default' as const }
      const tag = h(NTag, { size: 'small', type: cfg.type, bordered: false }, () => cfg.label)
      if (r.fail_count > 0 && r.status === 'active') {
        return h(NSpace, { size: 4 }, () => [
          tag,
          h(NText, { depth: 3, style: { fontSize: '12px' } }, () => t('schedule.failCount', { n: r.fail_count })),
        ])
      }
      return tag
    },
  },
  { title: t('schedule.cols.nextRun'), key: 'next_run_at', width: 140, render(r: Schedule) { return formatDate(r.next_run_at) } },
  { title: t('schedule.cols.lastRun'), key: 'last_run_at', width: 140, render(r: Schedule) { return formatDate(r.last_run_at) } },
  {
    title: t('schedule.cols.actions'),
    key: 'actions',
    width: 240,
    render(r: Schedule) {
      const buttons: any[] = [
        h(NButton, { size: 'tiny', type: 'primary', onClick: () => triggerNow(r) }, () => t('schedule.btn.runNow')),
      ]
      if (r.last_analysis_id) {
        buttons.push(
          h(NButton, { size: 'tiny', onClick: () => router.push(`/report/${r.last_analysis_id}`) }, () => t('schedule.btn.viewLatest')),
        )
      }
      if (r.status === 'active') {
        buttons.push(h(NButton, { size: 'tiny', onClick: () => setStatus(r, 'paused') }, () => t('schedule.btn.pause')))
      } else {
        buttons.push(h(NButton, { size: 'tiny', onClick: () => setStatus(r, 'active') }, () => t('schedule.btn.enable')))
      }
      buttons.push(h(NButton, { size: 'tiny', type: 'error', onClick: () => confirmDelete(r) }, () => t('schedule.btn.delete')))
      return h(NSpace, { size: 4 }, () => buttons)
    },
  },
])

async function load() {
  loading.value = true
  try {
    const { data } = await api.get('/api/schedules')
    schedules.value = data.items || []
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = null
  form.name = ''
  form.ticker = ''
  form.asset_type = 'stock'
  form.schedule_type = 'daily'
  form.interval_minutes = 60
  form.time_of_day = '09:30'
  form.day_of_week = 0
  form.analysts = ['market', 'news', 'fundamentals']
  form.max_debate_rounds = 1
  form.max_risk_discuss_rounds = 1
  form.auto_trade = false
  form.auto_trade_cash_pct = 10
  showEdit.value = true
}

async function save() {
  if (!form.ticker) {
    message.warning(t('schedule.validation.ticker'))
    return
  }
  if (form.schedule_type === 'interval' && (!form.interval_minutes || form.interval_minutes < 5)) {
    message.warning(t('schedule.validation.intervalMin'))
    return
  }
  if (form.schedule_type !== 'interval' && !form.time_of_day) {
    message.warning(t('schedule.validation.timeOfDay'))
    return
  }
  saving.value = true
  try {
    const payload = {
      name: form.name || null,
      ticker: form.ticker.trim().toUpperCase(),
      asset_type: form.asset_type,
      schedule_type: form.schedule_type,
      interval_minutes: form.schedule_type === 'interval' ? form.interval_minutes : null,
      time_of_day: form.schedule_type !== 'interval' ? form.time_of_day : null,
      day_of_week: form.schedule_type === 'weekly' ? form.day_of_week : null,
      analysts: form.analysts,
      max_debate_rounds: form.max_debate_rounds,
      max_risk_discuss_rounds: form.max_risk_discuss_rounds,
      auto_trade: form.auto_trade,
      auto_trade_cash_fraction: form.auto_trade ? form.auto_trade_cash_pct / 100 : null,
    }
    await api.post('/api/schedules', payload)
    message.success(t('schedule.msg.created'))
    showEdit.value = false
    await load()
  } catch (e: any) {
    message.error(t('schedule.msg.saveFailed') + (e?.response?.data?.detail || e?.message || t('common.unknownError')))
  } finally {
    saving.value = false
  }
}

async function triggerNow(row: Schedule) {
  try {
    const { data } = await api.post(`/api/schedules/${row.id}/trigger`)
    message.success(t('schedule.msg.started'))
    if (data.analysis_id) {
      router.push(`/progress/${data.analysis_id}`)
    }
  } catch (e: any) {
    message.error(t('schedule.msg.triggerFailed') + (e?.response?.data?.detail || e?.message || t('common.unknownError')))
  }
}

async function setStatus(row: Schedule, status: 'active' | 'paused') {
  try {
    await api.put(`/api/schedules/${row.id}`, { status })
    message.success(status === 'active' ? t('schedule.msg.enabled') : t('schedule.msg.paused'))
    await load()
  } catch (e: any) {
    message.error(t('schedule.msg.actionFailed') + (e?.response?.data?.detail || e?.message || t('common.unknownError')))
  }
}

function confirmDelete(row: Schedule) {
  dialog.warning({
    title: t('schedule.confirmDeleteTitle'),
    content: t('schedule.confirmDeleteContent', { name: row.name || row.ticker }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      await api.delete(`/api/schedules/${row.id}`)
      message.success(t('common.deleted'))
      await load()
    },
  })
}

// --- Screen schedules (auto-rotating analysis pool) ---

interface ScreenSchedule {
  id: number
  name: string | null
  text: string | null
  top_n: number
  use_llm: number
  schedule_type: 'daily' | 'weekly'
  time_of_day: string | null
  day_of_week: number | null
  sub_schedule_type: 'interval' | 'daily' | 'weekly'
  sub_interval_minutes: number | null
  sub_time_of_day: string | null
  sub_day_of_week: number | null
  evict_after_misses: number
  max_pool_size: number | null
  auto_trade: number
  auto_trade_cash_fraction: number | null
  status: 'active' | 'disabled'
  fail_count: number
  next_run_at: string
  managed_count?: number
}

const ssLoading = ref(false)
const screenSchedules = ref<ScreenSchedule[]>([])
const showSsEdit = ref(false)
const ssEditingId = ref<number | null>(null)
const ssSaving = ref(false)

const ssForm = reactive({
  name: '' as string | null,
  text: '',
  top_n: 20,
  use_llm: false,
  schedule_type: 'daily' as 'daily' | 'weekly',
  time_of_day: '09:20' as string | null,
  day_of_week: 0 as number | null,
  analysts: ['market', 'news', 'fundamentals'] as string[],
  sub_schedule_type: 'daily' as 'interval' | 'daily' | 'weekly',
  sub_interval_minutes: 60 as number | null,
  sub_time_of_day: '09:35' as string | null,
  sub_day_of_week: 0 as number | null,
  evict_after_misses: 3,
  max_pool_size: null as number | null,
  auto_trade: false,
  auto_trade_cash_pct: 10,
})

function describeScreenCadence(s: ScreenSchedule): string {
  if (s.schedule_type === 'weekly') {
    const dow = dowOptions.value.find(d => d.value === s.day_of_week)?.label || ''
    return t('schedule.pattern.weekly', { dow, time: s.time_of_day })
  }
  return t('schedule.pattern.daily', { time: s.time_of_day })
}

function describeSubCadence(s: ScreenSchedule): string {
  if (s.sub_schedule_type === 'interval') return t('schedule.pattern.interval', { n: s.sub_interval_minutes })
  if (s.sub_schedule_type === 'weekly') {
    const dow = dowOptions.value.find(d => d.value === s.sub_day_of_week)?.label || ''
    return t('schedule.pattern.weekly', { dow, time: s.sub_time_of_day })
  }
  return t('schedule.pattern.daily', { time: s.sub_time_of_day })
}

const ssColumns = computed(() => [
  { title: t('screenSchedule.cols.name'), key: 'name', width: 130, render(r: ScreenSchedule) { return r.name || r.text || '—' } },
  { title: t('screenSchedule.cols.screen'), key: 'screen', width: 130, render: (r: ScreenSchedule) => describeScreenCadence(r) },
  { title: t('screenSchedule.cols.sub'), key: 'sub', width: 130, render: (r: ScreenSchedule) => describeSubCadence(r) },
  {
    title: t('screenSchedule.cols.pool'), key: 'pool', width: 90,
    render(r: ScreenSchedule) {
      const parts: any[] = [t('screenSchedule.poolCount', { n: r.managed_count ?? 0 })]
      if (r.auto_trade) parts.push(h(NTag, { size: 'tiny', type: 'warning', bordered: false, style: { marginLeft: '4px' } }, () => t('schedule.autoTradeBadge')))
      return h(NSpace, { size: 2, align: 'center', wrapItem: false }, () => parts)
    },
  },
  {
    title: t('screenSchedule.cols.status'), key: 'status', width: 100,
    render(r: ScreenSchedule) {
      const cfg = statusLabel.value[r.status] || { label: r.status, type: 'default' as const }
      return h(NTag, { size: 'small', type: cfg.type, bordered: false }, () => cfg.label)
    },
  },
  { title: t('screenSchedule.cols.nextRun'), key: 'next_run_at', width: 130, render(r: ScreenSchedule) { return formatDate(r.next_run_at) } },
  {
    title: t('screenSchedule.cols.actions'), key: 'actions', width: 260,
    render(r: ScreenSchedule) {
      const buttons: any[] = [
        h(NButton, { size: 'tiny', type: 'primary', loading: ssRunning.value === r.id, onClick: () => runScreenNow(r) }, () => t('screenSchedule.btn.runNow')),
        h(NButton, { size: 'tiny', onClick: () => viewPool(r) }, () => t('screenSchedule.btn.viewPool')),
        h(NButton, { size: 'tiny', onClick: () => openScreenEdit(r) }, () => t('screenSchedule.btn.edit')),
      ]
      if (r.status === 'active') buttons.push(h(NButton, { size: 'tiny', onClick: () => setScreenStatus(r, 'disabled') }, () => t('screenSchedule.btn.pause')))
      else buttons.push(h(NButton, { size: 'tiny', onClick: () => setScreenStatus(r, 'active') }, () => t('screenSchedule.btn.enable')))
      buttons.push(h(NButton, { size: 'tiny', type: 'error', onClick: () => confirmDeleteScreen(r) }, () => t('screenSchedule.btn.delete')))
      return h(NSpace, { size: 4 }, () => buttons)
    },
  },
])

async function loadScreens() {
  ssLoading.value = true
  try {
    const { data } = await api.get('/api/screen-schedules')
    screenSchedules.value = data.items || []
  } finally {
    ssLoading.value = false
  }
}

function resetSsForm() {
  ssForm.name = ''
  ssForm.text = ''
  ssForm.top_n = 20
  ssForm.use_llm = false
  ssForm.schedule_type = 'daily'
  ssForm.time_of_day = '09:20'
  ssForm.day_of_week = 0
  ssForm.analysts = ['market', 'news', 'fundamentals']
  ssForm.sub_schedule_type = 'daily'
  ssForm.sub_interval_minutes = 60
  ssForm.sub_time_of_day = '09:35'
  ssForm.sub_day_of_week = 0
  ssForm.evict_after_misses = 3
  ssForm.max_pool_size = null
  ssForm.auto_trade = false
  ssForm.auto_trade_cash_pct = 10
}

function openScreenCreate() {
  ssEditingId.value = null
  resetSsForm()
  showSsEdit.value = true
}

function openScreenEdit(r: ScreenSchedule) {
  ssEditingId.value = r.id
  ssForm.name = r.name
  ssForm.text = r.text || ''
  ssForm.top_n = r.top_n
  ssForm.use_llm = !!r.use_llm
  ssForm.schedule_type = r.schedule_type
  ssForm.time_of_day = r.time_of_day || '09:20'
  ssForm.day_of_week = r.day_of_week ?? 0
  ssForm.sub_schedule_type = r.sub_schedule_type
  ssForm.sub_interval_minutes = r.sub_interval_minutes ?? 60
  ssForm.sub_time_of_day = r.sub_time_of_day || '09:35'
  ssForm.sub_day_of_week = r.sub_day_of_week ?? 0
  ssForm.evict_after_misses = r.evict_after_misses
  ssForm.max_pool_size = r.max_pool_size
  ssForm.auto_trade = !!r.auto_trade
  ssForm.auto_trade_cash_pct = Math.round((r.auto_trade_cash_fraction ?? 0.1) * 100)
  // analysts aren't returned flat here; keep current defaults unless edited.
  showSsEdit.value = true
}

async function saveScreen() {
  if (!ssForm.text.trim()) { message.warning(t('screenSchedule.validation.goal')); return }
  if (!ssForm.time_of_day) { message.warning(t('screenSchedule.validation.time')); return }
  ssSaving.value = true
  try {
    const payload: any = {
      name: ssForm.name || null,
      text: ssForm.text.trim(),
      top_n: ssForm.top_n,
      use_llm: ssForm.use_llm,
      schedule_type: ssForm.schedule_type,
      time_of_day: ssForm.time_of_day,
      day_of_week: ssForm.schedule_type === 'weekly' ? ssForm.day_of_week : null,
      analysts: ssForm.analysts,
      sub_schedule_type: ssForm.sub_schedule_type,
      sub_interval_minutes: ssForm.sub_schedule_type === 'interval' ? ssForm.sub_interval_minutes : null,
      sub_time_of_day: ssForm.sub_schedule_type !== 'interval' ? ssForm.sub_time_of_day : null,
      sub_day_of_week: ssForm.sub_schedule_type === 'weekly' ? ssForm.sub_day_of_week : null,
      evict_after_misses: ssForm.evict_after_misses,
      max_pool_size: ssForm.max_pool_size,
      auto_trade: ssForm.auto_trade,
      auto_trade_cash_fraction: ssForm.auto_trade ? ssForm.auto_trade_cash_pct / 100 : null,
    }
    if (ssEditingId.value) await api.put(`/api/screen-schedules/${ssEditingId.value}`, payload)
    else await api.post('/api/screen-schedules', payload)
    message.success(ssEditingId.value ? t('screenSchedule.msg.saved') : t('screenSchedule.msg.created'))
    showSsEdit.value = false
    await loadScreens()
  } catch (e: any) {
    message.error(t('screenSchedule.msg.saveFailed') + (e?.response?.data?.detail || e?.message || t('common.unknownError')))
  } finally {
    ssSaving.value = false
  }
}

const ssRunning = ref<number | null>(null)

async function runScreenNow(r: ScreenSchedule) {
  ssRunning.value = r.id
  try {
    const { data } = await api.post(`/api/screen-schedules/${r.id}/trigger`)
    message.success(t('screenSchedule.msg.reconciled', { n: data.managed_count ?? 0 }))
    await loadScreens()
  } catch (e: any) {
    message.error(t('screenSchedule.msg.runFailed') + (e?.response?.data?.detail || e?.message || t('common.unknownError')))
  } finally {
    ssRunning.value = null
  }
}

async function setScreenStatus(r: ScreenSchedule, status: 'active' | 'disabled') {
  try {
    await api.put(`/api/screen-schedules/${r.id}`, { status })
    await loadScreens()
  } catch (e: any) {
    message.error(t('schedule.msg.actionFailed') + (e?.response?.data?.detail || e?.message || t('common.unknownError')))
  }
}

function confirmDeleteScreen(r: ScreenSchedule) {
  dialog.warning({
    title: t('screenSchedule.confirmDeleteTitle'),
    content: t('screenSchedule.confirmDeleteContent', { name: r.name || r.text || r.id }),
    positiveText: t('common.delete'),
    negativeText: t('common.cancel'),
    onPositiveClick: async () => {
      await api.delete(`/api/screen-schedules/${r.id}?cascade=true`)
      message.success(t('screenSchedule.msg.deleted'))
      await loadScreens()
      await load()
    },
  })
}

// --- managed pool modal ---
const showPool = ref(false)
const poolItems = ref<Schedule[]>([])

const poolColumns = computed(() => [
  { title: t('schedule.cols.ticker'), key: 'ticker', width: 120 },
  {
    title: t('schedule.cols.status'), key: 'miss', width: 120,
    render(r: any) {
      if (r.miss_count > 0) return h(NText, { depth: 3 }, () => t('screenSchedule.poolMiss', { n: r.miss_count }))
      return h(NTag, { size: 'tiny', type: 'success', bordered: false }, () => t('schedule.status.active'))
    },
  },
  { title: t('schedule.cols.nextRun'), key: 'next_run_at', width: 150, render(r: any) { return formatDate(r.next_run_at) } },
])

async function viewPool(r: ScreenSchedule) {
  try {
    const { data } = await api.get(`/api/screen-schedules/${r.id}/managed`)
    poolItems.value = data.items || []
    showPool.value = true
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e?.message || t('common.unknownError'))
  }
}

onMounted(() => { load(); loadScreens() })
</script>
