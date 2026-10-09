<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { readEvents } from './sse'
import ReportView from './ReportView.vue'

type Fields = { title: string; symptoms: string; service: string; environment: string; service_version: string;
  purpose: 'diagnosis' | 'status_check';
  start: string; end: string; occurred_at: string; impact: string; reported_severity: string; demo_case_id: string | null }
type Incident = Fields & { id: string; revision: number; latest_run_id: string | null; business_status: string }
type Demo = { case_id: string; synthetic: boolean; fields: Fields }
type Service = { service: string; environment: string; service_version: string; metrics: { name: string; unit: string }[] }
type Result = { run_id: string; status: string; business_result: string; review_status: string; final: string; execution_mode?: string;
  run_summary: { model_calls?: number; tool_calls?: number; termination_reason?: string; duration_ms?: number };
  output?: { findings?: unknown[]; hypotheses?: unknown[]; recommended_actions?: unknown[]; missing_information?: string[]; escalation_team?: string };
  evidence?: { evidence_id: string; kind: string; excerpt: string; payload: unknown; locator: string; data_version: string; hash: string }[];
  task_results?: unknown[]; reviews?: unknown[]; negotiation?: unknown[]; incident_snapshot?: Incident }
type RunEvent = { seq: number; type: string; role?: string; name?: string; status?: string; reason?: string; message?: string }
const props = defineProps<{ ready: boolean; tenantId: string; userId: string; role: string;
  fetcher: (url: string, options?: RequestInit) => Promise<Response> }>()
const items = ref<Incident[]>([]), demos = ref<Demo[]>([]), services = ref<Service[]>([]), selected = ref<Incident | null>(null)
const chosenService = ref(''), paidAuthorized = ref(false)
const chosenDemo = ref(''), error = ref(''), notice = ref(''), busy = ref(false), connecting = ref(false)
const result = ref<Result | null>(null), runId = ref(''), runStatus = ref(''), events = ref<RunEvent[]>([])
const offset = ref(0), mode = ref('scripted_control_only'), modelBudget = ref(12), toolBudget = ref(8)
const cause = ref(''), resolution = ref(''), labels = ref(''), codes = ref('')
const emptyFields = (): Fields => {
  const end = new Date()
  return { title: '', symptoms: '', purpose: 'diagnosis', service: '', environment: 'staging', service_version: '',
    start: new Date(end.getTime() - 24 * 3600000).toISOString(), end: end.toISOString(), occurred_at: end.toISOString(),
    impact: '', reported_severity: 'unknown', demo_case_id: null }
}
const form = ref<Fields>(emptyFields())
const key = `incident.workspace.${props.tenantId}.${props.userId}`
type Saved = { incidentId: string; runId?: string; seq?: number; pending?: { request_key: string; execution_mode: string; model_budget?: number; tool_budget?: number }; confirmationKey?: string }
const saved = ref<Saved | null>(null)
let alive = true, generation = 0, streamController: AbortController | null = null
const pageController = new AbortController()
const statuses: Record<string, string> = { open: '待排查', investigating: '诊断中', awaiting_confirmation: '待人工确认', resolved: '已解决',
  running: '运行中', completed: '执行完成', partial: '有限结果', failed: '执行失败', diagnosis_available: '诊断可查看',
  needs_information: '需要补充信息', escalation_recommended: '建议升级', status_checked: '所列观测核查完成',
  passed: '模型复核通过', not_performed: '未复核' }
const locked = computed(() => ['investigating', 'resolved'].includes(selected.value?.business_status || ''))
const bindingKey = (row: { service: string; environment: string; service_version: string }) =>
  JSON.stringify([row.service, row.environment, row.service_version])
const selectedBinding = computed(() => services.value.find(row => bindingKey(row) === chosenService.value))
const validForm = computed(() => {
  const f = form.value, start = Date.parse(f.start), end = Date.parse(f.end), occurred = Date.parse(f.occurred_at)
  return !!f.title.trim() && !!f.symptoms.trim() && /^[A-Za-z0-9_-]{1,80}$/.test(f.service)
    && !!f.service_version.trim() && Number.isFinite(start) && Number.isFinite(end) && Number.isFinite(occurred)
    && end > start && end - start <= 86400000 && occurred >= start && occurred <= end
})
const validBudget = computed(() => Number.isInteger(modelBudget.value) && modelBudget.value >= 6 && modelBudget.value <= 16
  && Number.isInteger(toolBudget.value) && toolBudget.value >= 1 && toolBudget.value <= 24)
const purposeSaved = computed(() => !!selected.value && form.value.purpose === (selected.value.purpose || 'diagnosis'))
const permittedStart = computed(() => purposeSaved.value && (mode.value === 'live' ? paidAuthorized.value && validBudget.value : !!selected.value?.demo_case_id))
const businessLabel = (row: Incident) => row.purpose === 'status_check' && row.business_status === 'awaiting_confirmation'
  ? '待查看核查结果' : statuses[row.business_status] || row.business_status
const localTime = (value: string) => {
  const date = new Date(value)
  return Number.isFinite(date.getTime()) ? new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19) : ''
}
const setTime = (field: 'start' | 'end' | 'occurred_at', event: Event) => {
  const value = (event.target as HTMLInputElement).value
  form.value[field] = value ? new Date(value).toISOString() : ''
}
const setWindow = (minutes: number) => {
  const end = new Date()
  form.value.start = new Date(end.getTime() - minutes * 60000).toISOString()
  form.value.end = end.toISOString(); form.value.occurred_at = end.toISOString()
}
const confirmable = computed(() => props.role === 'operator' && selected.value?.business_status === 'awaiting_confirmation'
  && selected.value?.purpose !== 'status_check'
  && result.value && ['completed', 'partial'].includes(result.value.status)
  && result.value.incident_snapshot?.revision === selected.value.revision && selected.value.latest_run_id === result.value.run_id)
const save = () => { if (saved.value) sessionStorage.setItem(key, JSON.stringify(saved.value)) }
const api = async (url: string, options: RequestInit = {}) => {
  const response = await props.fetcher('/api/v1/' + url, { ...options, signal: options.signal || pageController.signal })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(typeof body.detail === 'string' ? body.detail : `请求失败 ${response.status}，请检查表单字段`)
  }
  return response
}
const jsonOptions = (method: string, body: unknown) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
const guarded = async (work: () => Promise<void>) => {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try { await work() } catch (e) { if (alive) error.value = e instanceof Error ? e.message : '操作失败' }
  finally { if (alive) busy.value = false }
}
const refreshList = async () => {
  const data = await (await api(`incidents?offset=${offset.value}&limit=20`)).json()
  if (alive) items.value = data.items
}
const fieldsOf = (row: Incident): Fields => {
  const fields = emptyFields()
  for (const name of Object.keys(fields) as (keyof Fields)[]) fields[name] = row[name] as never
  fields.purpose = row.purpose || 'diagnosis'
  return fields
}
const stopStream = () => { generation++; streamController?.abort(); streamController = null; connecting.value = false }
const reconnect = async () => {
  if (!selected.value) return
  stopStream()
  const current = generation, iid = selected.value.id
  const row: Incident = await (await api(`incidents/${iid}`)).json()
  if (!alive || current !== generation) return
  selected.value = row
  runId.value = row.latest_run_id || ''
  if (!runId.value) return
  if (!saved.value || saved.value.incidentId !== iid) saved.value = { incidentId: iid }
  if (saved.value.runId !== runId.value) { saved.value.runId = runId.value; saved.value.seq = 0; events.value = [] }
  saved.value.pending = undefined; save()
  const record = await (await api(`runs/${runId.value}`)).json()
  if (!alive || current !== generation) return
  runStatus.value = record.status; result.value = record.result
  // Terminal runs replay the complete process; active runs resume from the last acknowledged sequence.
  const after = record.status === 'running' ? saved.value.seq || 0 : 0
  if (!after) events.value = []
  streamController = new AbortController(); connecting.value = true
  try {
    const response = await api(`runs/${runId.value}/events?after_seq=${after}`, { signal: streamController.signal })
    await readEvents<RunEvent & Partial<Result>>(response, event => {
      if (!alive || current !== generation) return
      events.value.push(event)
      if (events.value.length > 300) events.value.shift()
      if (saved.value) { saved.value.seq = event.seq; save() }
      if (event.type === 'final') { result.value = event as Result; runStatus.value = event.status || ''; }
    })
    if (alive && current === generation) {
      selected.value = await (await api(`incidents/${iid}`)).json()
      await refreshList()
      if (runStatus.value === 'running') throw new Error('事件连接已断开，点击重新连接继续查看。')
    }
  } finally { if (current === generation) connecting.value = false }
}
const view = async (row: Incident) => {
  stopStream(); selected.value = row; form.value = fieldsOf(row); chosenDemo.value = row.demo_case_id || ''
  chosenService.value = row.demo_case_id ? '' : bindingKey(row)
  paidAuthorized.value = false
  result.value = null; events.value = []; runId.value = ''; runStatus.value = ''; cause.value = ''; resolution.value = ''
  if (saved.value?.incidentId !== row.id) saved.value = { incidentId: row.id }
  mode.value = saved.value.pending?.execution_mode || (row.demo_case_id ? 'scripted_control_only' : 'live')
  if (saved.value.pending?.model_budget) modelBudget.value = saved.value.pending.model_budget
  if (saved.value.pending?.tool_budget) toolBudget.value = saved.value.pending.tool_budget
  save()
  // Keep admission buttons usable while the subscription is open.
  void reconnect().catch(e => { if (alive && e.name !== 'AbortError') error.value = e.message })
}
const newTicket = () => {
  stopStream(); selected.value = null; form.value = emptyFields(); chosenDemo.value = ''
  chosenService.value = ''; paidAuthorized.value = false
  result.value = null; events.value = []; runId.value = ''; runStatus.value = ''; saved.value = null
  sessionStorage.removeItem(key); error.value = ''; notice.value = ''
  if (services.value.length === 1) { chosenService.value = bindingKey(services.value[0]!); fillService() }
}
const fillService = () => {
  const service = selectedBinding.value
  if (!service) return
  chosenDemo.value = ''; form.value.demo_case_id = null; paidAuthorized.value = false; mode.value = 'live'
  form.value.service = service.service; form.value.environment = service.environment; form.value.service_version = service.service_version
  if (!form.value.title && !form.value.symptoms) form.value.purpose = 'status_check'
  if (!form.value.title) form.value.title = `${service.service} 状态核查`
  if (!form.value.symptoms) form.value.symptoms = '请核对实际指标和运行日志，依据证据判断是否存在异常，不预设故障或强行提出修复。'
}
const fillDemo = () => {
  const demo = demos.value.find(d => d.case_id === chosenDemo.value)
  if (demo) { form.value = { ...demo.fields, purpose: demo.fields.purpose || 'diagnosis' }; chosenService.value = ''; mode.value = 'scripted_control_only'; paidAuthorized.value = false }
  else form.value.demo_case_id = null
}
const persist = () => guarded(async () => {
  if (!validForm.value) throw new Error('请填写标题、症状、服务和版本；时间窗口须有序且不超过24小时，发生时间须在窗口内。')
  const row: Incident = await (await api(selected.value ? `incidents/${selected.value.id}` : 'incidents',
    jsonOptions(selected.value ? 'PATCH' : 'POST', selected.value ? { revision: selected.value.revision, changes: form.value } : form.value))).json()
  await refreshList(); await view(row); notice.value = '工单已保存'
})
const start = () => guarded(async () => {
  if (!selected.value || locked.value || !permittedStart.value) return
  stopStream()
  if (!saved.value || saved.value.incidentId !== selected.value.id) saved.value = { incidentId: selected.value.id }
  // Persist before POST. If the 202 response is lost, retry the same key and exact execution allowance.
  saved.value.pending ||= { request_key: crypto.randomUUID(), execution_mode: mode.value,
    ...(mode.value === 'live' ? { model_budget: modelBudget.value, tool_budget: toolBudget.value } : {}) }
  save()
  const started = await (await api(`incidents/${selected.value.id}/diagnoses`, jsonOptions('POST', saved.value.pending))).json()
  saved.value.runId = started.run_id; saved.value.seq = 0; saved.value.pending = undefined; save()
  runId.value = started.run_id; result.value = null; events.value = []
  paidAuthorized.value = false
  void reconnect().catch(e => { if (alive && e.name !== 'AbortError') error.value = e.message })
})
const confirm = () => guarded(async () => {
  if (!selected.value || !result.value || !saved.value) return
  saved.value.confirmationKey ||= crypto.randomUUID(); save()
  await api(`incidents/${selected.value.id}/resolution`, jsonOptions('POST', { request_key: saved.value.confirmationKey,
    revision: selected.value.revision, run_id: result.value.run_id, confirmed_cause: cause.value.trim(), resolution: resolution.value.trim(),
    tags: labels.value.split(',').map(s => s.trim()).filter(Boolean), error_codes: codes.value.split(',').map(s => s.trim()).filter(Boolean) }))
  selected.value = await (await api(`incidents/${selected.value.id}`)).json()
  await refreshList(); notice.value = '已人工确认解决，经验可在当前身份和适用版本下检索'
})
onMounted(() => {
  if (!props.ready) return
  void guarded(async () => {
    demos.value = (await (await api('incident-demo-cases')).json()).items
    services.value = (await (await api('incident-services')).json()).items
    await refreshList()
    try { saved.value = JSON.parse(sessionStorage.getItem(key) || 'null') } catch { sessionStorage.removeItem(key) }
    if (saved.value?.incidentId) {
      const row = await (await api(`incidents/${saved.value.incidentId}`)).json()
      await view(row)
    } else if (services.value.length === 1) {
      chosenService.value = bindingKey(services.value[0]!); fillService()
    }
  })
})
onUnmounted(() => { alive = false; stopStream(); pageController.abort() })
</script>

<template>
  <main class="incident-panel">
    <header><h2>工单工作区</h2><p class="hint">选择真实服务或开发案例，保存问题后再启动诊断。报告、证据与执行事件会保留在后端。</p></header>
    <p v-if="!ready" class="notice">请先在左侧验证访问令牌。</p>
    <template v-else>
      <p v-if="error" class="error" role="alert">{{ error }}</p><p v-if="notice" role="status">{{ notice }}</p>
      <section class="ticket-list">
        <div class="actions"><button :disabled="busy" @click="newTicket">新建工单</button><button :disabled="busy" @click="guarded(refreshList)">刷新列表</button>
          <button :disabled="!offset || busy" @click="offset -= 20; guarded(refreshList)">上一页</button>
          <button :disabled="items.length < 20 || busy" @click="offset += 20; guarded(refreshList)">下一页</button></div>
        <p v-if="!items.length" class="hint">暂无工单。下面可直接选择已登记服务，或用免费开发案例检查流程。</p>
        <button v-for="row in items" :key="row.id" class="ticket-row" :class="{ selected: row.id === selected?.id }" @click="guarded(() => view(row))">
          <strong>{{ row.title }}</strong><span>{{ row.service }} · {{ businessLabel(row) }} · v{{ row.revision }}</span>
        </button>
      </section>
      <section>
        <h3>{{ selected ? '工单详情' : '新建工单' }}</h3>
        <p v-if="selected">{{ selected.id }} · {{ businessLabel(selected) }} · 修订 {{ selected.revision }}</p>
        <form @submit.prevent="persist"><fieldset :disabled="locked || busy">
          <div class="grid">
            <label>核查目的<select v-model="form.purpose" @change="paidAuthorized = false"><option value="status_check">状态核查（观测与覆盖范围）</option><option value="diagnosis">故障诊断（原因与处理建议）</option></select></label>
            <label>已登记服务<select v-model="chosenService" @change="fillService"><option value="">请选择真实服务</option><option v-for="service in services" :key="bindingKey(service)" :value="bindingKey(service)">{{ service.service }} · {{ service.environment }} · {{ service.service_version }}</option></select></label>
            <label>开发案例<select v-model="chosenDemo" @change="fillDemo"><option value="">真实服务 / 自由工单</option><option v-for="demo in demos" :key="demo.case_id" :value="demo.case_id">{{ demo.case_id }} · {{ demo.fields.title }}（合成）</option></select></label>
          </div>
          <p v-if="!services.length" class="hint">当前身份没有可见服务。请核对服务登记、用户名大小写，并重启后端加载配置。</p>
          <p v-if="selectedBinding" class="hint">可用指标：{{ selectedBinding.metrics.map(metric => `${metric.name} (${metric.unit})`).join('、') || '暂无登记指标' }}</p>
          <div class="grid">
            <label>标题<input v-model="form.title" maxlength="160" required /></label><label>服务<input v-model="form.service" maxlength="80" required readonly /></label>
            <label>环境<select v-model="form.environment"><option>staging</option><option>production</option></select></label>
            <label>服务版本<input v-model="form.service_version" maxlength="40" required readonly /></label>
            <label>观测开始（本地时间）<input type="datetime-local" step="1" :value="localTime(form.start)" @input="setTime('start', $event)" required /></label>
            <label>观测结束（最长24小时）<input type="datetime-local" step="1" :value="localTime(form.end)" @input="setTime('end', $event)" required /></label>
            <label>发生时间（窗口内）<input type="datetime-local" step="1" :value="localTime(form.occurred_at)" @input="setTime('occurred_at', $event)" required /></label>
            <label>报告严重程度<select v-model="form.reported_severity"><option v-for="level in ['unknown','low','medium','high','critical']" :key="level">{{ level }}</option></select></label>
          </div>
          <div v-if="!form.demo_case_id" class="actions"><span class="hint">快速设置窗口</span><button type="button" @click="setWindow(15)">最近15分钟</button><button type="button" @click="setWindow(60)">最近1小时</button><button type="button" @click="setWindow(1440)">最近24小时</button></div>
          <label>症状 / 需要核查的问题<textarea v-model="form.symptoms" rows="4" maxlength="2000" required /></label><label>影响范围<textarea v-model="form.impact" rows="2" maxlength="500" placeholder="没有确认影响时，可以留空" /></label>
          <button class="primary-action" type="submit" :disabled="!validForm">{{ selected ? '保存修订' : '创建工单' }}</button>
        </fieldset></form>
        <p class="hint">真实服务使用已登记的数据源。开发案例使用合成观测，服务和时间必须匹配案例。创建工单不会调用模型。</p>
      </section>
      <section v-if="selected">
        <h3>自主诊断</h3>
        <p class="hint">已保存目的：{{ selected.purpose === 'status_check' ? '状态核查；完成仅表示所列观测已复核，不证明服务全面正常。' : '故障诊断；分析证据支持的原因及处理建议。' }}</p>
        <p v-if="!purposeSaved" class="notice">核查目的已修改，请先保存修订再启动。</p>
        <div class="actions"><label>执行模式<select v-model="mode" :disabled="busy || locked || !!saved?.pending" @change="paidAuthorized = false"><option value="scripted_control_only" :disabled="!selected.demo_case_id">免费流程演示（开发案例）</option><option value="live">真实模型（付费）</option></select></label>
          <template v-if="mode === 'live'"><label>模型调用上限<input v-model.number="modelBudget" type="number" min="6" max="16" :disabled="busy || locked || !!saved?.pending" @input="paidAuthorized = false" /></label><label>工具调用上限<input v-model.number="toolBudget" type="number" min="1" max="24" :disabled="busy || locked || !!saved?.pending" @input="paidAuthorized = false" /></label></template>
        </div>
        <label v-if="mode === 'live'" class="paid-consent"><input v-model="paidAuthorized" type="checkbox" :disabled="busy || locked" />我同意按上述预算执行本次付费诊断（模型最多 {{ modelBudget }} 次，工具最多 {{ toolBudget }} 次）。</label>
        <div class="actions"><button class="primary-action" :disabled="busy || locked || !permittedStart" @click="start">{{ saved?.pending ? '重试原启动请求' : mode === 'live' ? '启动付费诊断' : '启动免费演示' }}</button>
          <button :disabled="busy || !runId" @click="reconnect().catch(e => error = e.message)">读取结果 / 事件回放</button></div>
        <p v-if="saved?.pending" class="hint">上次启动请求结果尚未确认，重试会沿用原请求编号和预算。</p>
        <p v-if="mode === 'live'" class="hint">仅点击启动才会使用模型额度。报告查看、页面刷新与事件回放不会重新发起诊断。</p>
        <p v-if="runId">运行 {{ runId }} · {{ statuses[runStatus] || runStatus }} {{ connecting ? '· 正在接收事件' : '' }}</p>
        <details v-if="events.length" open><summary>角色 / 工具执行过程（{{ events.length }} 条）</summary><div class="event-log">
          <p v-for="event in events" :key="event.seq">#{{ event.seq }} · {{ event.role }} {{ event.name }} · {{ event.type }} {{ event.status }} {{ event.message }} {{ event.reason }}</p>
        </div></details>
        <template v-if="result">
          <p>业务结果：{{ statuses[result.business_result] || result.business_result }} · 审查：{{ result.execution_mode === 'scripted_control_only' ? '脚本控制演示 · ' : '' }}{{ statuses[result.review_status] || result.review_status }}</p>
          <p>模型 {{ result.run_summary.model_calls || 0 }} 次 · 工具 {{ result.run_summary.tool_calls || 0 }} 次 · 停止原因 {{ result.run_summary.termination_reason }}</p>
          <p class="hint">诊断完成不等于工单解决。当前语义复核仍可能误判，人工确认前请核对原始引用。</p>
          <p class="hint">以下为诊断运行时生成的报告；人工确认后的状态以工单详情为准。</p><ReportView :text="result.final" />
          <details><summary>报告原文与运行统计</summary><pre>{{ result.final }}</pre><pre>{{ JSON.stringify(result.run_summary, null, 2) }}</pre></details>
          <details v-for="(rows, title) in { '事实与引用': result.output?.findings, '候选假设与状态': result.output?.hypotheses, '建议动作、风险与负责人': result.output?.recommended_actions, '任务结果': result.task_results, '逐项复核': result.reviews, '协作与补查': result.negotiation }" :key="title">
            <summary>{{ title }}</summary><pre>{{ JSON.stringify(rows || [], null, 2) }}</pre>
          </details>
          <p v-for="gap in result.output?.missing_information || []" :key="gap">缺口：{{ gap }}</p>
          <p v-if="result.output?.escalation_team">建议升级至：{{ result.output.escalation_team }}</p>
          <details v-for="e in result.evidence || []" :key="e.evidence_id"><summary>{{ e.kind }} · {{ e.evidence_id }}</summary>
            <p>{{ e.locator }} · 数据版本 {{ e.data_version }}</p><p>{{ e.excerpt }}</p><pre>{{ JSON.stringify(e.payload, null, 2) }}</pre><small>SHA256 {{ e.hash }}</small>
          </details>
        </template>
      </section>
      <section v-if="confirmable">
        <h3>人工确认解决</h3><p>请填写实际核实的原因及处理结果；确认后作为当前身份的历史经验供知识检索。</p>
        <label>已确认原因<textarea v-model="cause" maxlength="1000" /></label><label>实际处理与验证结果<textarea v-model="resolution" maxlength="2000" /></label>
        <div class="grid"><label>标签（逗号分隔，最多 8 个）<input v-model="labels" /></label><label>错误码（逗号分隔，最多 8 个）<input v-model="codes" /></label></div>
        <button :disabled="busy || !cause.trim() || !resolution.trim()" @click="confirm">确认已解决并保存经验</button>
      </section>
      <p v-else-if="selected && role !== 'operator'" class="hint">当前身份可诊断；人工确认需要拥有本工单的 operator 身份。</p>
    </template>
  </main>
</template>

<style scoped>
.incident-panel { min-width: 0; padding: 24px 28px; background: #f5f7fb; color: #203047; max-width: 1320px; margin: 0 auto }
h2 { margin: 0; font-size: 26px } h3 { margin: 0 0 16px } header { margin-bottom: 24px }
section { background: white; border: 1px solid #dce3ed; border-radius: 12px; padding: 20px; margin-bottom: 18px }
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px }
label { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; font-size: 14px }
input, select, textarea { width: 100%; box-sizing: border-box; border: 1px solid #bdc9d9; border-radius: 6px; background: #fff; color: #203047; padding: 9px; font: inherit }
fieldset { border: 0; padding: 0; min-width: 0 } button { cursor: pointer; border: 1px solid #bdc9d9; border-radius: 6px; padding: 9px 14px; color: #203047; background: #eef4fc }
button:disabled { opacity: .5; cursor: default } .actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 10px }
.ticket-row { display: flex; justify-content: space-between; text-align: left; width: 100%; gap: 10px; margin-top: 8px; background: #fff }
.selected { border-color: #3977c1; background: #eef4fc } .hint, small { color: #5b6a7d; font-size: 13px; overflow-wrap: anywhere }
.error { color: #a32a23; background: #fff1ed; padding: 12px; border-radius: 8px } .notice { padding: 24px; background: white }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f0f4f9; padding: 12px; max-height: 480px; overflow: auto; font-size: 13px }
details { margin: 12px 0; border-top: 1px solid #e3e8ef; padding-top: 10px } summary { cursor: pointer }
.event-log { max-height: 230px; overflow: auto; font-size: 13px } .event-log p { margin: 6px 0; overflow-wrap: anywhere }
.primary-action { background: #346bd6; color: white; border-color: #346bd6; }
.paid-consent { flex-direction: row; align-items: flex-start; gap: 9px; background: #fff7e8; border: 1px solid #f0dbb3; padding: 12px; border-radius: 8px; line-height: 1.6; }
.paid-consent input { width: 17px; height: 17px; margin: 3px 0 0; flex-shrink: 0; }
@media(max-width: 850px) { .incident-panel { padding: 14px } .grid { grid-template-columns: 1fr } .ticket-row { flex-direction: column } }
</style>
