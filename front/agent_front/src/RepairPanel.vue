<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { repairErrors, repairLabels, sourceMatches, type RepairCapability, type RepairIncident,
  type RepairPlan, type RepairResult } from './repairs'

const props = defineProps<{ incident: RepairIncident; result: RepairResult | null; role: string;
  tenantId: string; userId: string; parentBusy: boolean;
  fetcher: (url: string, options?: RequestInit) => Promise<Response> }>()
const emit = defineEmits<{ busy: [value: boolean] }>()
type PendingProposal = { route: string; body: Record<string, unknown> }
type Saved = { pending?: PendingProposal; executionKeys: Record<string, string>; planId?: string }
const storageKey = `incident.repairs.${props.tenantId}.${props.userId}.${props.incident.id}`
let saved: Saved = { executionKeys: {} }
try { saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null') || saved } catch { sessionStorage.removeItem(storageKey) }
saved.executionKeys ||= {}
const pending = ref<PendingProposal | null>(saved.pending || null)
const capabilities = ref<RepairCapability[]>([]), plans = ref<RepairPlan[]>([]), selectedId = ref(saved.planId || '')
const busy = ref(false), loading = ref(false), error = ref(''), notice = ref(''), executingRequest = ref(false)
const target = ref(''), reason = ref(''), evidenceIds = ref<string[]>([]), suggestion = ref('')
const approveConsent = ref(false), executeConsent = ref(false), clock = ref(Date.now())
const controller = new AbortController()
let alive = true, version = 0, polling = false
const persist = () => {
  saved.pending = pending.value || undefined; saved.planId = selectedId.value
  sessionStorage.setItem(storageKey, JSON.stringify(saved))
}
const capabilityKey = (item: { action: string; target: string }) => JSON.stringify([item.action, item.target])
const chosenCapability = computed(() => capabilities.value.find(item => capabilityKey(item) === target.value))
const selectedPlan = computed(() => plans.value.find(plan => plan.id === selectedId.value))
const observations = computed(() => props.result?.evidence?.filter(item => item.kind === 'observation') || [])
const eligible = computed(() => sourceMatches(props.incident, props.result))
const recommendations = computed(() => (props.result?.output?.recommended_actions || [])
  .map((item, index) => ({ item, index })).filter(({ item }) => item.requires_approval && item.repair
    && item.repair.action === 'restart_service' && capabilities.value.some(cap => capabilityKey(cap) === capabilityKey(item.repair!))))
const state = computed(() => {
  const plan = selectedPlan.value
  if (!plan) return ''
  if (['pending_approval', 'approved'].includes(plan.status)) {
    if (Date.parse(plan.expires_at) <= clock.value) return 'expired'
    if (plan.proposal.revision !== props.incident.revision || plan.proposal.run_id !== props.incident.latest_run_id
        || props.incident.business_status !== 'awaiting_confirmation') return 'source_changed'
  }
  return plan.effective_status || plan.status
})
const running = computed(() => executingRequest.value || plans.value.some(plan => plan.status === 'executing'))
const blocked = computed(() => busy.value || loading.value || props.parentBusy || running.value)
const canCreate = computed(() => eligible.value && !!chosenCapability.value && !blocked.value
  && !pending.value && reason.value.trim().length > 0 && evidenceIds.value.length > 0 && evidenceIds.value.length <= 8)
const operator = computed(() => props.role === 'operator')
const canApprove = computed(() => operator.value && state.value === 'pending_approval' && !blocked.value && approveConsent.value)
const canExecute = computed(() => operator.value && state.value === 'approved' && !blocked.value && executeConsent.value)
const label = (value: string) => repairLabels[value] || value
const when = (value: string) => new Date(value).toLocaleString('zh-CN')
const checkLabel = (check: { metric: string; operator: string; threshold: number; max_age_seconds: number; unit?: string }) =>
  `${check.metric} ${{ gte: '≥', lte: '≤', eq: '=' }[check.operator] || check.operator} ${check.threshold} ${check.unit || ''}；数据年龄 ≤ ${check.max_age_seconds} 秒`
const api = async (route: string, method = 'GET', body?: unknown) => {
  const response = await props.fetcher(`/api/v1/incidents/${props.incident.id}/${route}`, {
    method, signal: controller.signal,
    ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : `请求失败（${response.status}），请核对输入。`
    throw new Error(repairErrors[detail] || detail)
  }
  return data
}
const acceptPlan = (plan: RepairPlan, select = true) => {
  const index = plans.value.findIndex(item => item.id === plan.id)
  if (index < 0) plans.value.push(plan); else plans.value[index] = plan
  if (select) selectedId.value = plan.id
  persist()
}
const load = async () => {
  const current = ++version
  loading.value = true
  try {
    const [available, history] = await Promise.all([api('repair-capabilities'), api('repairs')])
    if (!alive || current !== version) return
    capabilities.value = available.items.filter((item: RepairCapability) => item.action === 'restart_service')
    plans.value = history.items
    const recovered = pending.value && plans.value.find(item => item.proposal.request_key === pending.value?.body.request_key)
    if (recovered) { selectedId.value = recovered.id; pending.value = null; notice.value = '已找到上次创建的方案，可继续查看和审批。' }
    if (!plans.value.some(item => item.id === selectedId.value)) selectedId.value = plans.value[plans.value.length - 1]?.id || ''
    if (!capabilities.value.some(item => capabilityKey(item) === target.value)) target.value = capabilities.value[0] ? capabilityKey(capabilities.value[0]) : ''
    persist()
  } finally { if (alive && current === version) loading.value = false }
}
const guarded = async (work: () => Promise<void>) => {
  if (busy.value || props.parentBusy) return
  busy.value = true; emit('busy', true); error.value = ''; notice.value = ''
  try { await work() } catch (e) {
    if (alive && !controller.signal.aborted) error.value = e instanceof TypeError
      ? '网络连接中断，操作结果尚未确认。请先刷新方案；再次确认执行会使用原请求编号。'
      : e instanceof Error ? e.message : '操作结果尚未确认，请刷新方案查看状态。'
  } finally { if (alive) { busy.value = false; emit('busy', false) } }
}
const refresh = () => guarded(load)
const useSuggestion = () => {
  const choice = recommendations.value.find(item => String(item.index) === suggestion.value)?.item
  if (!choice?.repair) return
  target.value = capabilityKey(choice.repair); reason.value = choice.action
  evidenceIds.value = choice.repair.evidence_ids.filter(id => observations.value.some(item => item.evidence_id === id))
}
const postProposal = async () => {
  if (!pending.value) return
  // Retrying an uncertain creation uses the exact stored request, never a fresh allowance.
  const plan = await api(pending.value.route, 'POST', pending.value.body) as RepairPlan
  if (!alive) return
  pending.value = null; acceptPlan(plan); approveConsent.value = false; executeConsent.value = false
  notice.value = '方案已创建，尚未审批或执行。'
}
const create = () => guarded(async () => {
  if (!eligible.value || !chosenCapability.value || !reason.value.trim() || !evidenceIds.value.length) return
  const recommended = recommendations.value.find(item => String(item.index) === suggestion.value)
  const intent = recommended?.item.repair
  const exactSuggestion = intent && capabilityKey(intent) === target.value && reason.value === recommended.item.action
    && JSON.stringify(evidenceIds.value) === JSON.stringify(intent.evidence_ids)
  pending.value = exactSuggestion ? { route: 'repairs/from-recommendation', body: {
    request_key: crypto.randomUUID(), revision: props.incident.revision, run_id: props.result!.run_id, action_index: recommended.index,
  }} : { route: 'repairs', body: {
    request_key: crypto.randomUUID(), revision: props.incident.revision, run_id: props.result!.run_id,
    action: chosenCapability.value.action, target: chosenCapability.value.target, parameters: {},
    reason: reason.value.trim(), evidence_ids: [...evidenceIds.value],
  }}
  persist(); await postProposal()
})
const retryProposal = () => guarded(postProposal)
const abandonPending = () => { pending.value = null; persist(); notice.value = '已放弃本地创建请求；如后端已创建方案，它仍保留在方案列表中。' }
const approve = (decision: 'approve' | 'reject') => guarded(async () => {
  const plan = selectedPlan.value
  if (!plan || !operator.value || (decision === 'approve' && (!approveConsent.value || state.value !== 'pending_approval'))) return
  acceptPlan(await api(`repairs/${plan.id}/approval`, 'POST', { digest: plan.digest, decision }))
  approveConsent.value = false; executeConsent.value = false
  notice.value = decision === 'approve' ? '方案已批准。需要单独确认并点击执行，才会重启目标。' : '方案已拒绝。'
})
const execute = () => guarded(async () => {
  const plan = selectedPlan.value
  if (!plan || !operator.value || !executeConsent.value || state.value !== 'approved') return
  saved.executionKeys[plan.id] ||= plan.execution_key || crypto.randomUUID(); persist()
  executeConsent.value = false; executingRequest.value = true
  try { acceptPlan(await api(`repairs/${plan.id}/execute`, 'POST', { request_key: saved.executionKeys[plan.id] })) }
  finally { executingRequest.value = false }
  notice.value = '执行结果已返回，请查看恢复验证及阶段记录；工单不会自动关闭。'
})
watch(() => [props.incident.revision, props.incident.latest_run_id, props.incident.business_status], () => {
  approveConsent.value = false; executeConsent.value = false; evidenceIds.value = []; suggestion.value = ''
  void load().catch(e => { if (alive) error.value = e.message })
}, { immediate: true })
watch(() => [selectedId.value, state.value], () => { approveConsent.value = false; executeConsent.value = false })
const timer = window.setInterval(async () => {
  clock.value = Date.now()
  if (!alive || polling || !running.value) return
  polling = true
  const active = plans.value.filter(plan => plan.status === 'executing').map(plan => plan.id)
  if (executingRequest.value && selectedId.value && !active.includes(selectedId.value)) active.push(selectedId.value)
  try {
    const updates = await Promise.all(active.map(id => api(`repairs/${id}`))) as RepairPlan[]
    if (alive) for (const plan of updates) acceptPlan(plan, false)
  } catch (e) { if (alive && !controller.signal.aborted) error.value = e instanceof Error ? e.message : '读取执行状态失败，请手动刷新。' }
  finally { polling = false }
}, 2000)
onUnmounted(() => { alive = false; version++; controller.abort(); window.clearInterval(timer); emit('busy', false) })
</script>

<template>
  <section class="repair-panel" aria-label="修复方案与执行">
    <header><div><h3>修复方案与执行</h3><p class="hint">先创建方案，再审批，最后单独执行。读取方案和刷新结果不会启动诊断或重启服务。</p></div>
      <button :disabled="busy || loading || parentBusy" @click="refresh">{{ loading ? '读取中…' : '刷新修复方案' }}</button></header>
    <p v-if="error" class="error" role="alert">{{ error }}</p><p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <p v-if="!eligible" class="hint">创建真实修复方案需要：故障诊断完成且复核通过、来自真实服务，并与当前工单修订和最新运行一致。请先查看诊断结果。</p>
    <p v-else-if="!loading && !capabilities.length" class="hint">此服务没有可用的已登记重启目标，请核对本地服务配置。</p>
    <div v-if="pending" class="pending-request"><p>上次创建方案的结果尚未确认。请先刷新；重试会使用原请求内容。</p>
      <div class="actions"><button :disabled="blocked" @click="retryProposal">重试原创建请求</button><button :disabled="blocked" @click="abandonPending">放弃本地请求</button></div></div>
    <form v-if="eligible && capabilities.length" @submit.prevent="create">
      <fieldset :disabled="blocked || !!pending">
        <div class="grid">
          <label>已登记动作<select v-model="target" @change="suggestion = ''"><option v-for="cap in capabilities" :key="capabilityKey(cap)" :value="capabilityKey(cap)">{{ cap.target }} · {{ label(cap.action) }}</option></select></label>
          <label v-if="recommendations.length">使用诊断建议<select v-model="suggestion" @change="useSuggestion"><option value="">人工提案</option><option v-for="row in recommendations" :key="row.index" :value="String(row.index)">{{ row.item.action }}</option></select></label>
        </div>
        <p v-if="chosenCapability" class="impact">影响：{{ chosenCapability.impact }}</p>
        <label>处理依据<textarea v-model="reason" rows="2" maxlength="500" placeholder="说明为何需要对该目标执行重启" required /></label>
        <fieldset class="evidence-list"><legend>选择支持本次动作的当前观测（1–8 条）</legend>
          <label v-for="item in observations" :key="item.evidence_id" class="check"><input v-model="evidenceIds" type="checkbox" :value="item.evidence_id" :disabled="!evidenceIds.includes(item.evidence_id) && evidenceIds.length >= 8" /><span>{{ item.excerpt }}<small>{{ item.evidence_id }}</small></span></label>
          <p v-if="!observations.length" class="hint">没有可用于修复的当前观测证据。</p>
        </fieldset>
        <button class="primary" type="submit" :disabled="!canCreate">创建待审批方案</button>
      </fieldset>
    </form>
    <p v-if="!loading && !plans.length" class="hint">暂无修复方案。</p>
    <label v-if="plans.length">已有方案<select v-model="selectedId" :disabled="busy"><option v-for="plan in plans" :key="plan.id" :value="plan.id">{{ when(plan.created_at) }} · {{ plan.proposal.target }} · {{ label(plan.effective_status || plan.status) }}</option></select></label>
    <article v-if="selectedPlan" class="plan-card">
      <div class="plan-heading"><h4>{{ label(selectedPlan.proposal.action) }} · {{ selectedPlan.proposal.target }}</h4><span class="status" :class="state">{{ label(state) }}</span></div>
      <p>{{ selectedPlan.proposal.reason }}</p><p class="hint">{{ selectedPlan.live ? '真实操作' : '模拟操作' }} · 有效期至 {{ when(selectedPlan.expires_at) }}</p>
      <dl><dt>影响</dt><dd>{{ selectedPlan.impact }}</dd><dt>失败处理</dt><dd>{{ selectedPlan.rollback }}</dd><dt>平台验证</dt><dd>{{ selectedPlan.success_condition }}</dd>
        <dt>症状验证</dt><dd><p v-for="check in selectedPlan.symptom_checks" :key="check.metric">{{ checkLabel(check) }}</p></dd>
        <dt>验证预算</dt><dd>最多 {{ selectedPlan.verification_budget.attempts }} 次，间隔 {{ selectedPlan.verification_budget.interval_seconds }} 秒</dd></dl>
      <p v-if="!operator" class="hint">当前身份可以查看和提案；审批与执行需要本工单所属的 operator。</p>
      <template v-if="operator && state === 'pending_approval'">
        <label class="check consent"><input v-model="approveConsent" type="checkbox" :disabled="blocked" /><span>我已核对目标、证据、影响和恢复条件，同意此方案。</span></label>
        <div class="actions"><button class="primary" :disabled="!canApprove" @click="approve('approve')">批准方案</button><button :disabled="blocked" @click="approve('reject')">拒绝方案</button></div>
      </template>
      <template v-if="operator && state === 'approved'">
        <label class="check consent"><input v-model="executeConsent" type="checkbox" :disabled="blocked" /><span>我确认现在对 {{ selectedPlan.proposal.target }} 执行{{ label(selectedPlan.proposal.action) }}，已知晓上述影响。</span></label>
        <button class="execute" :disabled="!canExecute" @click="execute">{{ saved.executionKeys[selectedPlan.id] ? '确认原执行请求 / 读取结果' : '执行已批准方案' }}</button>
      </template>
      <p v-if="running" class="notice" role="status">正在执行或验证恢复，页面只自动读取阶段进度；切勿通过其他方式重复重启。</p>
      <p v-if="state === 'verified'" class="success">恢复验证通过。请再确认业务结果，之后在下方人工确认解决工单。</p>
      <p v-if="['expired', 'source_changed', 'configuration_changed'].includes(state)" class="hint">此方案已失效，需按当前工单和服务配置创建新方案并重新审批。</p>
      <p v-if="state === 'manual_required'" class="error">恢复未得到确认或执行已中断，请根据阶段记录人工处理。</p>
      <details open><summary>修复阶段记录（{{ selectedPlan.events.length }} 条）</summary><ol class="events"><li v-for="event in selectedPlan.events" :key="event.seq"><strong>{{ label(event.type) }}</strong><span>{{ when(event.at) }}{{ event.attempt ? ` · 第 ${event.attempt} 次` : '' }}</span>
        <p v-if="event.code">{{ repairErrors[event.code] || event.code }}</p><p v-if="event.passed !== undefined">{{ event.passed ? '验证通过' : '尚未满足恢复条件' }}</p>
        <details v-if="event.observation || event.symptoms"><summary>验证观测</summary><pre>{{ JSON.stringify({ observation: event.observation, symptoms: event.symptoms }, null, 2) }}</pre></details></li></ol></details>
      <details><summary>方案编号与绑定证据</summary><p>方案 {{ selectedPlan.id }} · 工单修订 {{ selectedPlan.proposal.revision }}</p><p>诊断 {{ selectedPlan.proposal.run_id }}</p><p>{{ selectedPlan.proposal.evidence_ids.join('、') }}</p></details>
    </article>
  </section>
</template>

<style scoped>
.repair-panel { background: #fff; border: 1px solid #dce3ed; border-radius: 12px; padding: 20px; margin-bottom: 18px; }
header, .plan-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; } h3, h4 { margin: 0 0 12px; }
.hint, small { font-size: 13px; color: #5b6a7d; line-height: 1.7; overflow-wrap: anywhere; } small { display: block; }
button, input, select, textarea { font: inherit; } button { border: 1px solid #bdc9d9; background: #eef4fc; color: #203047; border-radius: 6px; padding: 9px 14px; }
label { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; font-size: 14px; } input, select, textarea { min-width: 0; width: 100%; border: 1px solid #bdc9d9; border-radius: 6px; padding: 9px; }
fieldset { min-width: 0; border: 0; padding: 0; } .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.check { flex-direction: row; gap: 10px; align-items: flex-start; line-height: 1.7; } .check input { width: 17px; height: 17px; margin-top: 4px; flex-shrink: 0; }
.evidence-list { max-height: 260px; overflow: auto; margin: 15px 0; padding: 12px; border: 1px solid #dce3ed; border-radius: 8px; } legend { font-size: 14px; }
.actions { display: flex; flex-wrap: wrap; gap: 10px; margin: 12px 0; }.primary { background: #346bd6; color: #fff; border-color: #346bd6; }.execute { background: #b74428; border-color: #b74428; color: #fff; }
.plan-card { margin-top: 18px; border: 1px solid #dce3ed; border-radius: 9px; padding: 18px; }.status { border-radius: 6px; padding: 5px 8px; background: #eef3fb; font-size: 13px; }.verified { background: #e5f5eb; color: #256545; }
.consent, .impact, .pending-request { padding: 12px; border: 1px solid #f0dbb3; border-radius: 8px; background: #fff7e8; line-height: 1.8; }
dl { display: grid; grid-template-columns: 80px minmax(0, 1fr); gap: 10px; font-size: 14px; line-height: 1.8; } dt { color: #5b6a7d; } dd { margin: 0; overflow-wrap: anywhere; } dd p { margin: 0; }
.error, .notice, .success { padding: 12px; border-radius: 8px; line-height: 1.8; overflow-wrap: anywhere; }.error { background: #fff1ed; color: #a32a23; }.notice { background: #eef4fc; }.success { background: #e5f5eb; color: #256545; }
details { margin-top: 14px; } summary { cursor: pointer; font-size: 14px; }.events { padding-left: 22px; }.events li { margin: 12px 0; }.events span { display: block; color: #5b6a7d; font-size: 12px; margin-top: 5px; }.events p { margin: 5px 0; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 300px; overflow: auto; background: #f0f4f9; padding: 12px; font-size: 12px; }
@media(max-width: 850px) { header, .plan-heading { flex-direction: column; }.grid { grid-template-columns: 1fr; } dl { grid-template-columns: 1fr; gap: 4px; } dd { margin-bottom: 8px; } }
</style>
