<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import ReportView from './ReportView.vue'

type Observation = { source_name: string; service: string; observed_at: string; content: string }
type Supplement = { parent_run_id: string; revision: number; note: string; observations: Observation[] }
type Request = { request_key: string; execution_mode: string; model_budget?: number; tool_budget?: number; followup: Supplement }
type Summary = { model_calls?: number; tool_calls?: number; token_usage?: { input_tokens?: number; output_tokens?: number } }
type History = { run_id: string; status: string; revision: number; parent_run_id?: string; round?: number; summary: Summary }
type Result = { run_id: string; status: string; final: string; execution_mode?: string;
  followup?: { parent_run_id: string; submissions: { submitted_by: string; submitted_at: string; note: string; observations: Observation[] }[] } }
const props = defineProps<{ incident: { id: string; revision: number; latest_run_id: string | null; business_status: string;
  service: string; start: string; end: string; demo_case_id: string | null }; result: Result | null;
  disabled: boolean; storageKey: string; fetcher: (url: string, options?: RequestInit) => Promise<Response> }>()
const emit = defineEmits<{ started: [runId: string]; busy: [value: boolean] }>()
const note = ref(''), observations = ref<Observation[]>([]), mode = ref('live'), modelBudget = ref(16), toolBudget = ref(8)
const authorized = ref(false), pending = ref<Request | null>(null), busy = ref(false), error = ref('')
const history = ref<History[]>([]), offset = ref(0), totalRuns = ref(0)
const totals = ref({ model_calls: 0, tool_calls: 0, input_tokens_known: 0, output_tokens_known: 0, unmeasured_runs: 0 })
const historicalReport = ref<Result | null>(null)
let alive = true, generation = 0
const eligible = computed(() => !['investigating', 'resolved'].includes(props.incident.business_status)
  && props.result && ['completed', 'partial', 'failed'].includes(props.result.status)
  && props.result.run_id === props.incident.latest_run_id)
const valid = computed(() => (note.value.trim() || observations.value.length)
  && observations.value.every(row => row.source_name.trim() && row.service.trim() && row.content.trim() && Number.isFinite(Date.parse(row.observed_at)))
  && (mode.value !== 'live' || authorized.value && Number.isInteger(modelBudget.value) && modelBudget.value >= 6 && modelBudget.value <= 16
    && Number.isInteger(toolBudget.value) && toolBudget.value >= 1 && toolBudget.value <= 24))
const messages: Record<string, string> = {
  FOLLOWUP_BASE_CONFLICT: '原报告已不是当前最新结果，请刷新工单后重新选择。',
  FOLLOWUP_REVISION_CONFLICT: '工单已修订，请先基于当前工单生成诊断，再追加信息。',
  FOLLOWUP_OBSERVATION_SCOPE_DENIED: '记录的服务或观测时间超出本工单授权范围，请核对。',
  FOLLOWUP_CONTEXT_LIMIT: '累计补充内容过长，请缩减记录，或另建工单描述新的问题。',
  INCIDENT_LOCKED: '当前工单正在运行或已解决，暂不能开启新一轮。',
}
const request = async (path: string, body?: unknown) => {
  const response = await props.fetcher('/api/v1/' + path, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
  if (!response.ok) {
    const data = await response.json().catch(() => ({}))
    const detail = typeof data.detail === 'string' ? data.detail : '请核对补充信息、来源时间及调用预算。'
    throw Object.assign(new Error(messages[detail] || detail), { status: response.status })
  }
  return response.json()
}
const loadHistory = async () => {
  const current = ++generation
  try {
    const data = await request(`incidents/${props.incident.id}/diagnoses?offset=${offset.value}&limit=20`)
    if (alive && current === generation) { history.value = data.items; totals.value = data.totals; totalRuns.value = data.totals.runs }
  } catch (value) { if (alive && current === generation) error.value = value instanceof Error ? value.message : '历史记录读取失败' }
}
watch(() => [props.incident.id, props.incident.revision, props.incident.business_status, offset.value], () => { void loadHistory() }, { immediate: true })
watch(() => props.result?.run_id, () => { authorized.value = false; if (props.result) mode.value = props.result.execution_mode || 'live' })
try {
  const saved = JSON.parse(sessionStorage.getItem(props.storageKey) || 'null')
  if (saved && typeof saved.request_key === 'string' && saved.followup) {
    pending.value = saved; note.value = saved.followup.note; observations.value = saved.followup.observations
    mode.value = saved.execution_mode; modelBudget.value = saved.model_budget || 16; toolBudget.value = saved.tool_budget || 8
  }
} catch { sessionStorage.removeItem(props.storageKey) }
const localTime = (value: string) => {
  const date = new Date(value)
  return Number.isFinite(date.getTime()) ? new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19) : ''
}
const setTime = (row: Observation, event: Event) => {
  const value = (event.target as HTMLInputElement).value
  row.observed_at = value ? new Date(value).toISOString() : ''
}
const addObservation = () => observations.value.push({ source_name: '', service: props.incident.service,
  observed_at: props.incident.end, content: '' })
const submit = async () => {
  if (busy.value || props.disabled || (!pending.value && (!eligible.value || !valid.value || !props.result))) return
  busy.value = true; emit('busy', true); error.value = ''
  pending.value ||= { request_key: crypto.randomUUID(), execution_mode: mode.value,
    ...(mode.value === 'live' ? { model_budget: modelBudget.value, tool_budget: toolBudget.value } : {}),
    followup: { parent_run_id: props.result!.run_id, revision: props.incident.revision, note: note.value,
      observations: JSON.parse(JSON.stringify(observations.value)) } }
  sessionStorage.setItem(props.storageKey, JSON.stringify(pending.value))
  try {
    const data = await request(`incidents/${props.incident.id}/followups`, pending.value)
    pending.value = null; sessionStorage.removeItem(props.storageKey)
    note.value = ''; observations.value = []; authorized.value = false; historicalReport.value = null
    emit('started', data.run_id)
  } catch (value) {
    if (alive) error.value = value instanceof Error ? value.message : '启动结果尚未确认，请沿用原请求重试。'
    const status = (value as { status?: number }).status
    if (status && status >= 400 && status < 500) { pending.value = null; sessionStorage.removeItem(props.storageKey) }
  } finally { busy.value = false; emit('busy', false) }
}
const openReport = async (runId: string) => {
  try {
    const record = await request('runs/' + runId)
    if (alive) historicalReport.value = record.result
  } catch (value) { if (alive) error.value = value instanceof Error ? value.message : '报告读取失败' }
}
onUnmounted(() => { alive = false; generation++; emit('busy', false) })
</script>

<template>
  <section aria-label="追加信息与诊断历史">
    <h3>追加信息与关联诊断</h3>
    <p class="hint">补充新情况、指出反证或粘贴有来源的记录，重新调查并审阅。每轮单独确认预算，原报告保留。</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <details v-if="result?.followup"><summary>本轮采用的用户补充</summary>
      <div v-for="item in result.followup.submissions" :key="item.submitted_at">
        <p>{{ item.submitted_by }} · {{ item.submitted_at }} · 未经独立核验</p><p>{{ item.note }}</p>
        <details v-for="row in item.observations" :key="row.source_name + row.observed_at"><summary>{{ row.source_name }} · {{ row.service }} · {{ row.observed_at }}</summary><pre>{{ row.content }}</pre></details>
      </div>
      <button :disabled="disabled || busy" @click="openReport(result!.followup!.parent_run_id)">查看本轮基于的原报告</button>
    </details>
    <form v-if="eligible || pending" @submit.prevent="submit">
      <fieldset :disabled="disabled || busy || !!pending">
        <label>补充情况或争议点<textarea v-model="note" rows="3" maxlength="2000" placeholder="例如：回滚后故障仍存在，请重新核查发布导致故障的解释。" /></label>
        <p class="hint">用户说明与粘贴记录会标明来源，尚未独立核验；提交信息不代表结论必须改变。</p>
        <div v-for="(row, index) in observations" :key="index">
          <div class="grid"><label>记录来源<input v-model="row.source_name" maxlength="120" required placeholder="如 pod 日志导出" /></label>
            <label>记录所属服务<input v-model="row.service" maxlength="80" required /></label>
            <label>记录观测时间<input type="datetime-local" step="1" :value="localTime(row.observed_at)" @input="setTime(row, $event)" required /></label></div>
          <label>记录原文<textarea v-model="row.content" rows="3" maxlength="2000" required /></label>
          <button type="button" @click="observations.splice(index, 1)">移除此条记录</button>
        </div>
        <button type="button" :disabled="observations.length >= 3" @click="addObservation">添加有来源的记录（最多3条）</button>
        <p class="hint">记录所属服务需在本工单授权范围内，观测时间需位于 {{ incident.start }} 至 {{ incident.end }}。</p>
        <label>本轮执行模式<select v-model="mode" @change="authorized = false"><option v-if="incident.demo_case_id" value="scripted_control_only">免费流程演示（不评价模型判断）</option><option value="live">真实模型（付费）</option></select></label>
        <template v-if="mode === 'live'"><div class="grid">
          <label>本轮模型调用上限<input v-model.number="modelBudget" type="number" min="6" max="16" step="1" @input="authorized = false" /></label>
          <label>本轮工具调用上限<input v-model.number="toolBudget" type="number" min="1" max="24" step="1" @input="authorized = false" /></label></div>
          <label class="check"><input v-model="authorized" type="checkbox" />我同意为本轮关联诊断单独使用上述付费预算。</label>
        </template>
      </fieldset>
      <p v-if="pending" class="notice">上次启动结果尚未确认；重试会沿用原补充内容及预算。</p>
      <button class="primary-action" :disabled="disabled || busy || (!pending && !valid)">{{ pending ? '重试原追加请求' : '提交补充并启动新一轮诊断' }}</button>
    </form>
    <p v-else class="hint">当前运行结束且工单未解决时，可在这里追加信息。</p>
    <h3>诊断历史</h3>
    <p>已交付报告累计：模型 {{ totals.model_calls }} 次，工具 {{ totals.tool_calls }} 次；已知 Token 输入 {{ totals.input_tokens_known }}、输出 {{ totals.output_tokens_known }}。金额未估算。</p>
    <p v-if="totals.unmeasured_runs" class="hint">另有 {{ totals.unmeasured_runs }} 轮正在执行或用量不完整，累计数不代表最终账单。</p>
    <div v-for="row in history" :key="row.run_id" class="ticket-row">
      <span>修订 {{ row.revision }} · {{ row.status }} · {{ row.parent_run_id ? '关联重诊' : '独立诊断' }} · {{ row.run_id }}</span>
      <span>模型 {{ row.summary.model_calls ?? '待统计' }} 次／工具 {{ row.summary.tool_calls ?? '待统计' }} 次</span>
      <button type="button" :disabled="disabled || busy || row.status === 'running'" @click="openReport(row.run_id)">查看此轮报告</button>
    </div>
    <div class="actions"><button :disabled="!offset || busy" @click="offset -= 20">上一页诊断</button><button :disabled="offset + history.length >= totalRuns || busy" @click="offset += 20">下一页诊断</button></div>
    <article v-if="historicalReport" aria-label="历史诊断报告"><h3>历史报告（只读） · {{ historicalReport.run_id }}</h3>
      <button @click="historicalReport = null">关闭历史报告</button><ReportView :text="historicalReport.final" /></article>
  </section>
</template>

<style scoped>
h3 { margin: 0 0 16px } h3:not(:first-child) { margin-top: 22px }
label { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; font-size: 14px }
input, select, textarea { width: 100%; box-sizing: border-box; border: 1px solid #bdc9d9; border-radius: 6px; background: #fff; color: #203047; padding: 9px; font: inherit }
fieldset { border: 0; padding: 0; min-width: 0 }
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px }
button { cursor: pointer; border: 1px solid #bdc9d9; border-radius: 6px; padding: 9px 14px; color: #203047; background: #eef4fc }
button:disabled { opacity: .5; cursor: default } .primary-action { background: #346bd6; color: white; border-color: #346bd6 }
.check { flex-direction: row; align-items: flex-start; background: #fff7e8; border: 1px solid #f0dbb3; padding: 12px; border-radius: 8px }
.check input { width: auto; margin-top: 3px }
.hint { color: #5b6a7d; font-size: 13px; overflow-wrap: anywhere }
.error { color: #a32a23; background: #fff1ed; padding: 12px; border-radius: 8px }
.notice { padding: 12px; background: #eef4fc; border-radius: 8px }
.ticket-row { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin: 10px 0; padding: 12px; border: 1px solid #dce3ed; border-radius: 8px }
.ticket-row span:first-child { width: 100%; overflow-wrap: anywhere } .ticket-row button { margin-left: auto }
.actions { display: flex; gap: 10px; margin: 12px 0 }
details { margin: 12px 0; border-top: 1px solid #e3e8ef; padding-top: 10px } summary { cursor: pointer }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f0f4f9; padding: 12px; max-height: 320px; overflow: auto }
@media (max-width: 720px) { .grid { grid-template-columns: 1fr } .ticket-row button { margin-left: 0 } }
</style>
