<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue'

type RunSummary = {
  model_calls?: number; web_calls?: number; tool_calls?: number; duration_ms?: number
  termination_reason?: string; errors?: { code: string; provider: string; message: string }[]
  token_usage?: { input_tokens: number; output_tokens: number; status: string }
  question_coverage?: { total: number; answered: number; partial: number; unanswered: number; stage: string }
  rejected_claim_count?: number
}
type Coverage = { question_id: string; question: string; status: string; claim_ids: string[]; reason: string }
type Source = { source_id: string; title: string; url?: string; doc_id?: string; snippet: string; retrieval_level?: string; is_simulated?: boolean }
type RunResult = {
  run_id: string; status: string; final: string; run_summary: RunSummary; sources: Source[]
  question_coverage?: Coverage[]; coverage_stage?: string; missing_gaps?: string[]
  verification_rejections?: { claim_id: string; reason: string }[]
}
type StreamEvent = Partial<RunResult> & {
  type: string; seq?: number; message?: string; node?: string; kind?: string; name?: string
}
type SavedRun = { runId: string; query: string; seq: number; userId: string; threadId: string; tenantId: string }

type ChatMessage = {
  id: string
  role: 'user' | 'assistant' | 'status'
  content: string
}

const userId = ref('')
const threadId = ref<string>(crypto.randomUUID())
const tenantId = ref('')
const query = ref('')
const loading = ref(false)
const errorMessage = ref('')
const draftToken = ref('')
const token = ref('')
const authenticating = ref(false)
const profile = ref({ display_name: '', language: 'zh-CN', response_style: '简洁' })
const memoryNotice = ref('')
const memoryRows = ref<{id: string; kind: string; content: string}[]>([])
const sessionSummary = ref('')
const noteText = ref('')
let controller = new AbortController()
let identityVersion = 0
const messageListRef = ref<HTMLElement | null>(null)
const composerRef = ref<HTMLTextAreaElement | null>(null)
const progressLogs = ref<string[]>([])
const runId = ref('')
const runStatus = ref('')
const runSummary = ref<RunSummary>({})
const runSources = ref<Source[]>([])
const runCoverage = ref<Coverage[]>([])
const coverageStage = ref('not_assessed')
const missingGaps = ref<string[]>([])
const rejections = ref<{ claim_id: string; reason: string }[]>([])
const enableMemory = ref(false)
let activeRun: SavedRun | null = null
const storageKey = () => `deepresearch.run.${tenantId.value}.${userId.value}`
const statusLabels: Record<string, string> = { running: '执行中', completed: '已完成', partial: '有限结果', failed: '执行失败' }
const coverageLabels: Record<string, string> = { answered: '已覆盖', partial: '部分覆盖', unanswered: '未覆盖' }
const nodeLabels: Record<string, string> = {
  intent: '识别意图', direct_answer: '快速回答', plan: '规划问题', web_search: '网络取证', local_rag: '知识库取证',
  deep_dive: '审计证据', analyze: '分析结论', reflect: '规划补搜', verify: '检查证据支持', write: '组织报告', finish: '返回有限结果',
}
const saveRun = () => { if (activeRun) sessionStorage.setItem(storageKey(), JSON.stringify({ ...activeRun, query: '' })) }
const safeUrl = (url?: string) => url?.startsWith('https://') || url?.startsWith('http://') ? url : undefined
const starterPrompts = [
  {
    title: '企业平台选型',
    prompt:
      '我们是五人 Python 团队，计划部署内部 Agent 应用，必须支持自部署和调用内部 HTTP 接口。请比较 Dify 与 FastGPT 的部署、接口集成和维护要求，依据官方资料给出有前提的建议，附来源并明确未知项。不要把自部署等同于数据不外发。',
  },
  {
    title: '选型输入模板',
    prompt:
      '业务目标：[要构建的应用]\n候选平台：[候选 A]、[候选 B]\n硬性条件：[必须满足的条件]\n团队与部署环境：[已知条件]\n请比较部署、集成、成本与维护，给出推荐前提、未确认事项和 PoC 验证步骤；费用不明时不要推测免费。请填写方括号后再提交。',
  },
  {
    title: '本地资料比较',
    prompt: '只依据本地知识库，比较 Dify 和 FastGPT 是否有 Docker 自部署方式，附资料引用并说明资料日期和适用范围。不要推测实测性能、总成本或内部网络连通性。',
  },
  {
    title: '快速问答',
    prompt: '你好，请介绍你能帮助我研究什么。',
  },
]
const capabilityHighlights = [
  {
    title: '多智能体编排',
    desc: '自动完成规划、检索、证据裁判、分析与写作，减少手工研究路径。',
  },
  {
    title: '双源检索融合',
    desc: '网络信息与本地知识库并行召回，输出结论同时保留来源可追溯性。',
  },
  {
    title: '研究质量控制',
    desc: '复核结论的证据支持和问题覆盖；资料不足时返回缺口与有限结果。',
  },
]
const landingMetrics = [
  { label: '执行模式', value: 'Quick + Deep' },
  { label: '检索来源', value: 'Web + Local' },
  { label: '输出风格', value: '结论 + 证据' },
]
const messages = ref<ChatMessage[]>([
  {
    id: `m-${Date.now()}`,
    role: 'assistant',
    content: '你好，我是 DeepResearch。请提供业务目标、候选方案和硬性条件，我会研究选型依据、适配理由及资料缺口。也支持普通资料研究和快速问答。',
  },
])

const escapeHtml = (value: string): string =>
  value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')

const markdownToHtml = (markdown: string): string => {
  const codeBlocks: string[] = []
  let text = markdown.replace(/```([\s\S]*?)```/g, (_, block) => {
    const index = codeBlocks.length
    codeBlocks.push(`<pre><code>${escapeHtml(String(block).trim())}</code></pre>`)
    return `@@CODE_BLOCK_${index}@@`
  })
  const lines = text.split('\n')
  const out: string[] = []
  let inList = false
  const closeList = () => {
    if (inList) {
      out.push('</ul>')
      inList = false
    }
  }
  for (const rawLine of lines) {
    const line = rawLine.trim()
    if (!line) {
      closeList()
      continue
    }
    if (line.startsWith('# ')) {
      closeList()
      out.push(`<h1>${escapeHtml(line.slice(2))}</h1>`)
      continue
    }
    if (line.startsWith('## ')) {
      closeList()
      out.push(`<h2>${escapeHtml(line.slice(3))}</h2>`)
      continue
    }
    if (line.startsWith('### ')) {
      closeList()
      out.push(`<h3>${escapeHtml(line.slice(4))}</h3>`)
      continue
    }
    if (line.startsWith('- ') || line.startsWith('* ')) {
      if (!inList) {
        out.push('<ul>')
        inList = true
      }
      out.push(`<li>${escapeHtml(line.slice(2))}</li>`)
      continue
    }
    closeList()
    out.push(`<p>${escapeHtml(line)}</p>`)
  }
  closeList()
  let html = out.join('')
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>')
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>')
  html = html.replace(/\[([^[\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
  html = html.replace(/@@CODE_BLOCK_(\d+)@@/g, (_, idx) => codeBlocks[Number(idx)] || '')
  return html
}

const renderMessageHtml = (message: ChatMessage) => markdownToHtml(message.content || '')

const scrollToBottom = async () => {
  await nextTick()
  const el = messageListRef.value
  if (el) {
    el.scrollTop = el.scrollHeight
  }
}

const createNewChat = () => {
  sessionSummary.value = ''
  memoryRows.value = []
  memoryNotice.value = ''
  messages.value = [
    {
      id: `m-${Date.now()}`,
      role: 'assistant',
      content: '已开始新会话。你可以继续提问。',
    },
  ]
  progressLogs.value = []
  errorMessage.value = ''
  query.value = ''
  threadId.value = crypto.randomUUID()
  activeRun = null
  runId.value = ''
  runStatus.value = ''
  runSummary.value = {}
  runSources.value = []
  runCoverage.value = []
  coverageStage.value = 'not_assessed'
  missingGaps.value = []
  rejections.value = []
  sessionStorage.removeItem(storageKey())
}

const usePrompt = async (prompt: string) => {
  query.value = prompt
  errorMessage.value = ''
  await nextTick()
  composerRef.value?.focus()
}

const applyStarterByIndex = (index: number) => {
  const target = starterPrompts[index]
  if (!target) return
  usePrompt(target.prompt)
}

const pushProgress = (message: string) => {
  const msg = message.trim()
  if (!msg) return
  const last = progressLogs.value[progressLogs.value.length - 1]
  if (last === msg) return
  progressLogs.value.push(msg)
  if (progressLogs.value.length > 6) {
    progressLogs.value = progressLogs.value.slice(-6)
  }
}

const showProgress = (statusId: string) => {
  const message = messages.value.find(m => m.id === statusId)
  if (message) message.content = ['研究正在执行...', ...progressLogs.value].map(line => `- ${line}`).join('\n')
}

const applyResult = (result: RunResult, statusId: string) => {
  runId.value = result.run_id
  runStatus.value = result.status
  runSummary.value = result.run_summary || {}
  runSources.value = result.sources || []
  runCoverage.value = result.question_coverage || []
  coverageStage.value = result.coverage_stage || 'not_assessed'
  missingGaps.value = result.missing_gaps || []
  rejections.value = result.verification_rejections || []
  messages.value = messages.value.filter(m => m.id !== statusId && m.id !== `a-${result.run_id}`)
  messages.value.push({ id: `a-${result.run_id}`, role: 'assistant', content: result.final })
  errorMessage.value = ''
}

const consume = async (response: Response, statusId: string): Promise<boolean> => {
  await checkResponse(response)
  if (!response.body) throw new Error('流式响应不可用')
  const headerRun = response.headers.get('X-Research-Run-ID')
  if (headerRun && activeRun) { activeRun.runId = headerRun; runId.value = headerRun; saveRun() }
  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = '', finished = false
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (controller.signal.aborted) throw new DOMException('身份已退出', 'AbortError')
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const parts = buffer.split(/\r?\n\r?\n/)
      buffer = parts.pop() || ''
      for (const part of parts) {
        const data = part.split(/\r?\n/).find(line => line.startsWith('data: '))
        if (!data) continue
        const event = JSON.parse(data.slice(6)) as StreamEvent
        if (event.run_id && activeRun) {
          activeRun.runId = event.run_id
          activeRun.seq = event.seq || activeRun.seq
          runId.value = event.run_id
          saveRun()
        }
        if (event.type === 'call_start' && event.kind === 'node') pushProgress(`开始：${nodeLabels[event.name || ''] || event.name}`)
        if (event.type === 'phase') pushProgress(`完成：${nodeLabels[event.node || ''] || event.node}`)
        if (event.type === 'warning') pushProgress(event.message || '执行出现问题，正在返回可用结果')
        if (event.type === 'status') pushProgress(event.message || '研究已接收')
        if (event.type === 'coverage') pushProgress('证据复核及问题覆盖检查已完成')
        showProgress(statusId)
        if (event.type === 'final') {
          applyResult(event as RunResult, statusId)
          finished = true
        }
      }
      await scrollToBottom()
    }
  } finally { reader.releaseLock() }
  return finished
}

const reconnect = async (statusId: string) => {
  if (!activeRun?.runId) throw new Error('连接中断且未取得运行 ID；不会自动重复提交研究。')
  let lastError: unknown
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const response = await authorizedFetch(`/api/v1/research/runs/${activeRun.runId}`)
      await checkResponse(response)
      const record = await response.json() as { status: string; result: RunResult | null }
      if (record.status !== 'running' && record.result) { applyResult(record.result, statusId); return }
      const events = await authorizedFetch(`/api/v1/research/runs/${activeRun.runId}/events?after_seq=${activeRun.seq}`)
      if (await consume(events, statusId)) return
    } catch (error) { if (error instanceof AccessError || !token.value) throw error; lastError = error }
    await new Promise(resolve => setTimeout(resolve, 500 * (attempt + 1)))
  }
  throw lastError || new Error('连接暂不可用；研究可能仍在执行，可稍后点击重新连接。')
}

const reportError = (error: unknown, statusId: string) => {
  if (!token.value) return
  errorMessage.value = error instanceof Error ? error.message : '请求失败'
  messages.value = messages.value.filter(m => m.id !== statusId)
}

const runResearch = async () => {
  const userText = query.value.trim()
  if (!userText || loading.value) return
  if (!token.value) { errorMessage.value = '请先验证访问令牌。'; return }
  loading.value = true
  errorMessage.value = ''
  progressLogs.value = []
  runSummary.value = {}
  runSources.value = []
  runCoverage.value = []
  coverageStage.value = 'not_assessed'
  missingGaps.value = []
  rejections.value = []
  runStatus.value = 'running'
  runId.value = ''
  query.value = ''
  activeRun = { runId: '', query: userText, seq: 0, userId: userId.value, threadId: threadId.value, tenantId: tenantId.value }
  sessionStorage.removeItem(storageKey())
  messages.value.push({ id: `u-${Date.now()}`, role: 'user', content: userText })
  const statusId = `s-${Date.now()}`
  messages.value.push({ id: statusId, role: 'status', content: '正在提交研究...' })
  await scrollToBottom()
  try {
    const response = await authorizedFetch('/api/v1/research/stream', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: userText, thread_id: threadId.value.trim() || 'default_thread',
        max_iterations: 1, enable_memory: enableMemory.value }),
    })
    const acceptedRun = response.headers.get('X-Research-Run-ID')
    if (acceptedRun && activeRun) { activeRun.runId = acceptedRun; runId.value = acceptedRun; saveRun() }
    if (!await consume(response, statusId)) await reconnect(statusId)
  } catch (error) {
    if (token.value && activeRun?.runId) {
      try { await reconnect(statusId) } catch (reconnectError) { reportError(reconnectError, statusId) }
    } else { runStatus.value = ''; reportError(error, statusId) }
  } finally { loading.value = false; await scrollToBottom() }
}

const reconnectLatest = async () => {
  if (loading.value || !activeRun?.runId) return
  loading.value = true
  errorMessage.value = ''
  const statusId = `s-${Date.now()}`
  messages.value.push({ id: statusId, role: 'status', content: '正在读取运行记录，不会重新执行研究...' })
  try { await reconnect(statusId) } catch (error) { reportError(error, statusId) }
  finally { loading.value = false; await scrollToBottom() }
}

class AccessError extends Error {}
const checkResponse = async (response: Response) => {
  if (response.ok) return
  const body = await response.json().catch(() => ({})) as { detail?: string }
  const message = typeof body.detail === 'string' ? body.detail : `请求失败：${response.status}`
  if ([401, 403, 404].includes(response.status)) throw new AccessError(message)
  throw new Error(message)
}
const authorizedFetch = (url: string, options: RequestInit = {}) => fetch(url, {
  ...options, signal: controller.signal,
  headers: { ...options.headers, Authorization: `Bearer ${token.value}` },
})
const logout = () => {
  identityVersion++
  controller.abort()
  createNewChat()
  sessionStorage.removeItem('deepresearch.access-token')
  token.value = ''; draftToken.value = ''; userId.value = ''; tenantId.value = ''
  profile.value = { display_name: '', language: 'zh-CN', response_style: '简洁' }
  memoryRows.value = []; sessionSummary.value = ''; noteText.value = ''; memoryNotice.value = ''
  enableMemory.value = false
}
const authenticate = async () => {
  if (loading.value || authenticating.value) return
  authenticating.value = true
  errorMessage.value = ''
  const candidate = draftToken.value.trim()
  try {
    const response = await fetch('/api/v1/auth/me', { headers: { Authorization: `Bearer ${candidate}` } })
    await checkResponse(response)
    const identity = await response.json() as { user_id: string; tenant_id: string }
    identityVersion++
    controller.abort(); controller = new AbortController()
    token.value = candidate
    sessionStorage.setItem('deepresearch.access-token', candidate)
    userId.value = identity.user_id; tenantId.value = identity.tenant_id
    const saved = sessionStorage.getItem(storageKey())
    createNewChat()
    if (saved) {
      try {
        const run = JSON.parse(saved) as SavedRun
        if (/^[0-9a-f-]{36}$/i.test(run.runId) && Number.isInteger(run.seq) && run.seq >= 0
            && run.userId === identity.user_id && run.tenantId === identity.tenant_id
            && /^[A-Za-z0-9_-]{1,80}$/.test(run.threadId)) {
          activeRun = run; runId.value = run.runId; threadId.value = run.threadId
          saveRun(); await reconnectLatest()
        }
      } catch { sessionStorage.removeItem(storageKey()) }
    }
  } catch (error) {
    logout()
    errorMessage.value = error instanceof Error ? error.message : '身份验证失败'
  } finally { authenticating.value = false }
}
const memoryAction = async (action: 'read' | 'save' | 'note' | 'session' | 'all' | 'entry', id = '') => {
  const version = identityVersion
  memoryNotice.value = ''
  try {
    if (action !== 'read') {
      let url = '/api/v1/memory'
      let options: RequestInit = { method: 'DELETE' }
      if (action === 'save') {
        url += '/profile'; options = { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(profile.value) }
      } else if (action === 'note') {
        url += '/notes'; options = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content: noteText.value, thread_id: threadId.value }) }
      } else {
        url += `?target=${action}&thread_id=${encodeURIComponent(threadId.value)}`
        if (id) url += `&memory_id=${id}`
      }
      await checkResponse(await authorizedFetch(url, options))
      if (action === 'note') noteText.value = ''
    }
    const response = await authorizedFetch(`/api/v1/memory?thread_id=${encodeURIComponent(threadId.value)}`)
    await checkResponse(response)
    const data = await response.json() as { profile: Partial<typeof profile.value>; session: { summary: string }; memories: typeof memoryRows.value }
    if (version !== identityVersion) return
    profile.value = { display_name: '', language: 'zh-CN', response_style: '简洁', ...data.profile }
    memoryRows.value = data.memories; sessionSummary.value = data.session.summary
    memoryNotice.value = action === 'read' ? '已读取本人记忆' : '已更新本人记忆'
  } catch (error) { if (token.value && version === identityVersion) memoryNotice.value = error instanceof Error ? error.message : '记忆操作失败' }
}

onMounted(async () => {
  // Remove the legacy unscoped browser cache.
  localStorage.removeItem('deepresearch.latest-run')
  const savedToken = sessionStorage.getItem('deepresearch.access-token')
  if (savedToken) { draftToken.value = savedToken; await authenticate() }
})
</script>

<template>
  <div class="chat-shell">
    <aside class="chat-sidebar">
      <div class="sidebar-brand">
        <p class="brand-badge">AI Copilot</p>
        <h1>DeepResearch</h1>
        <p class="brand-desc">多智能体研究工作台，支持快速回答与深度调研。</p>
      </div>
      <div class="sidebar-head">
        <button class="new-chat-btn" :disabled="loading" @click="createNewChat">新建会话</button>
      </div>
      <div class="quick-entry">
        <p class="section-title">推荐起手问题</p>
        <button
          v-for="item in starterPrompts.slice(0, 3)"
          :key="item.title"
          class="quick-entry-btn"
          @click="usePrompt(item.prompt)"
        >
          {{ item.title }}
        </button>
      </div>
      <div class="settings-group">
        <label>演示访问令牌</label>
        <input v-model="draftToken" type="password" autocomplete="off" class="sidebar-input" :disabled="!!token || authenticating" placeholder="从本地凭据文件复制" />
        <button v-if="!token" :disabled="authenticating || !draftToken.trim()" @click="authenticate">{{ authenticating ? '验证中…' : '验证身份' }}</button>
        <button v-else @click="logout">退出当前身份</button>
      </div>
      <div class="settings-group">
        <label>User ID（服务端绑定）</label>
        <input :value="userId" readonly class="sidebar-input" />
      </div>
      <div class="settings-group">
        <label>Thread ID</label>
        <input v-model="threadId" :disabled="loading" class="sidebar-input" />
      </div>
      <div class="settings-group">
        <label>Tenant ID</label>
        <input :value="tenantId" readonly class="sidebar-input" />
      </div>
      <label class="memory-toggle"><input v-model="enableMemory" type="checkbox" />启用会话记忆（默认关闭）</label>
      <p class="hint-text">当前身份：{{ tenantId || '未验证' }} / {{ userId }}</p>
      <details v-if="token" class="settings-group">
        <summary>管理我的记忆</summary>
        <button @click="memoryAction('read')">读取记忆</button>
        <label>称呼</label><input v-model="profile.display_name" maxlength="40" class="sidebar-input" />
        <label>语言</label><select v-model="profile.language"><option value="zh-CN">中文</option><option value="en">English</option></select>
        <label>表达风格</label><select v-model="profile.response_style"><option v-for="style in ['通俗', '专业', '简洁', '详细']" :key="style">{{ style }}</option></select>
        <button @click="memoryAction('save')">保存偏好</button>
        <label>明确记住的背景</label><textarea v-model="noteText" maxlength="1600" class="sidebar-input" />
        <button :disabled="!noteText.trim()" @click="memoryAction('note')">保存背景</button>
        <p class="hint-text">历史背景不能替代本次研究证据。</p>
        <p v-if="sessionSummary">会话摘要：{{ sessionSummary }}</p>
        <article v-for="item in memoryRows" :key="item.id"><p>{{ item.kind }}：{{ item.content }}</p><button @click="memoryAction('entry', item.id)">删除此条</button></article>
        <button @click="memoryAction('session')">清除本会话记忆</button>
        <button @click="memoryAction('all')">清除本人全部记忆与偏好</button>
        <p role="status">{{ memoryNotice }}</p>
      </details>
    </aside>

    <main class="chat-main">
      <header class="main-header">
        <div>
          <h2>DeepResearch Enterprise Workspace</h2>
          <p>技术选型与资料研究：展示结论、证据与执行限制。网络来源基于搜索摘要。</p>
        </div>
        <div class="header-tags">
          <span>Evidence-Driven</span>
          <span>Structured Output</span>
          <span>Coverage Review</span>
        </div>
      </header>
      <details v-if="runId" class="run-details" open>
        <summary>本次运行 · {{ statusLabels[runStatus] || '连接中' }}</summary>
        <p>运行 ID：{{ runId }}</p>
        <p v-if="runSummary.model_calls !== undefined">
          模型调用 {{ runSummary.model_calls }} · 网络检索请求 {{ runSummary.web_calls }} · 工具调用 {{ runSummary.tool_calls }} ·
          耗时 {{ ((runSummary.duration_ms || 0) / 1000).toFixed(1) }} 秒
        </p>
        <p v-if="runSummary.token_usage">已知 Token：输入 {{ runSummary.token_usage.input_tokens }} / 输出 {{ runSummary.token_usage.output_tokens }}（{{ runSummary.token_usage.status }}）</p>
        <p v-if="runSummary.termination_reason">停止原因：{{ runSummary.termination_reason }}</p>
        <p v-for="(error, index) in runSummary.errors || []" :key="index">{{ error.provider }} · {{ error.code }}：{{ error.message }}</p>
        <details v-if="runCoverage.length" open>
          <summary>问题覆盖 · {{ coverageStage === 'verified' ? '已复核' : '尚未完成复核' }}</summary>
          <p v-for="row in runCoverage" :key="row.question_id">
            {{ row.question_id }} · {{ row.question }}：{{ coverageLabels[row.status] || row.status }}。{{ row.reason }}
          </p>
        </details>
        <details v-if="missingGaps.length"><summary>仍待解决的缺口（{{ missingGaps.length }}）</summary>
          <p v-for="(gap, index) in missingGaps" :key="index">{{ gap }}</p>
        </details>
        <details v-if="rejections.length"><summary>未通过复核的结论（{{ rejections.length }}）</summary>
          <p v-for="item in rejections" :key="item.claim_id">{{ item.claim_id }}：{{ item.reason }}</p>
        </details>
        <button v-if="errorMessage && !loading" @click="reconnectLatest">重新连接本次运行</button>
        <details v-if="runSources.length"><summary>查看报告引用的证据片段（{{ runSources.length }}）</summary>
          <article v-for="source in runSources" :key="source.source_id" class="source-card">
            <strong>[{{ source.source_id }}] {{ source.title }}</strong>
            <span v-if="source.is_simulated"> · 模拟资料</span>
            <p><a v-if="safeUrl(source.url)" :href="safeUrl(source.url)" target="_blank" rel="noreferrer">打开来源</a><span v-else>{{ source.doc_id }}</span>
              · {{ source.retrieval_level === 'summary_only' ? '搜索摘要，未读取全文' : '本地资料片段' }}</p>
            <p>{{ source.snippet }}</p>
          </article>
        </details>
      </details>
      <div ref="messageListRef" class="message-list">
        <section v-if="messages.length <= 1" class="onboarding-panel">
          <div class="hero-panel">
            <p class="hero-badge">企业选型 · 证据比较 · 资料研究</p>
            <h3>第一步先讲清目标，再交给 DeepResearch 自动推进</h3>
            <p class="hero-desc">
              推荐提问结构：目标 + 背景约束 + 期望输出。系统会自动选择快速回答或深度研究链路。
            </p>
            <div class="hero-actions">
              <button class="hero-btn primary" @click="applyStarterByIndex(0)">填写选型示例</button>
              <button class="hero-btn" @click="applyStarterByIndex(1)">使用选型模板</button>
            </div>
            <div class="metric-grid">
              <article v-for="item in landingMetrics" :key="item.label">
                <p>{{ item.label }}</p>
                <strong>{{ item.value }}</strong>
              </article>
            </div>
          </div>
          <div class="capability-grid">
            <article v-for="item in capabilityHighlights" :key="item.title" class="capability-card">
              <h4>{{ item.title }}</h4>
              <p>{{ item.desc }}</p>
            </article>
          </div>
          <div class="guide-panel">
            <h4>提问指南</h4>
            <div class="guide-grid">
              <article>
                <h5>1. 说明目标</h5>
                <p>你要解决什么问题、面向谁、希望达到什么结果。</p>
              </article>
              <article>
                <h5>2. 提供上下文</h5>
                <p>给出已知信息、时间范围、数据口径、业务限制。</p>
              </article>
              <article>
                <h5>3. 指定输出</h5>
                <p>例如“表格输出”“附来源链接”“分点行动清单”。</p>
              </article>
            </div>
          </div>
          <div class="prompt-list">
            <button v-for="item in starterPrompts" :key="item.prompt" class="prompt-chip" @click="usePrompt(item.prompt)">
              {{ item.prompt }}
            </button>
          </div>
        </section>
        <div
          v-for="message in messages"
          :key="message.id"
          class="message-row"
          :class="`role-${message.role}`"
        >
          <div class="avatar">{{ message.role === 'user' ? '你' : message.role === 'status' ? '...' : 'AI' }}</div>
          <div class="bubble markdown-body" v-html="renderMessageHtml(message)"></div>
        </div>
      </div>
      <div class="composer">
        <textarea
          v-model="query"
          ref="composerRef"
          class="composer-input"
          :disabled="loading"
          placeholder="输入你的问题，回车发送（Shift + Enter 换行）"
          @keydown.enter.exact.prevent="runResearch"
        />
        <button class="send-btn" :disabled="loading || !query.trim()" @click="runResearch">
          {{ loading ? '处理中...' : '发送' }}
        </button>
      </div>
      <p v-if="errorMessage" class="error">{{ errorMessage }}</p>
    </main>
  </div>
</template>

<style scoped>
.run-details { margin: 0 24px 12px; padding: 12px 16px; border: 1px solid #dce4ee; border-radius: 12px; background: #f8fafc; max-height: 260px; overflow: auto; font-size: 13px; }
.run-details p { margin: 6px 0; overflow-wrap: anywhere; }
.run-details summary { cursor: pointer; font-weight: 600; }
.source-card { margin-top: 10px; padding: 10px; background: white; border-radius: 8px; }
.memory-toggle { display: flex; align-items: center; gap: 6px; font-size: 12px; margin: 12px 0; }
</style>
