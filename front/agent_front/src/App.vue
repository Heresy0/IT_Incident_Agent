<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import IncidentPanel from './IncidentPanel.vue'

type Identity = { tenant_id: string; user_id: string; role: string }
const draftToken = ref(''), token = ref(''), identity = ref<Identity | null>(null)
const authenticating = ref(false), error = ref(''), health = ref('检查中'), identityVersion = ref(0)
const authController = new AbortController()
const disconnect = () => { identity.value = null; token.value = ''; draftToken.value = ''; identityVersion.value++ }
const authenticate = async () => {
  if (authenticating.value) return
  const candidate = draftToken.value.trim().replace(/^Bearer\s+/i, '')
  if (!candidate) { error.value = '请填写本项目的访问令牌。'; return }
  authenticating.value = true; error.value = ''; identity.value = null; token.value = ''; identityVersion.value++
  try {
    const response = await fetch('/api/v1/auth/me', {
      headers: { Authorization: `Bearer ${candidate}` }, signal: authController.signal,
    })
    if (!response.ok) {
      const data = await response.json().catch(() => ({}))
      throw new Error(typeof data.detail === 'string' ? data.detail : `身份验证失败（${response.status}）`)
    }
    identity.value = await response.json()
    token.value = candidate; draftToken.value = ''
  } catch (e) {
    if (!authController.signal.aborted) error.value = e instanceof Error ? e.message : '无法连接后端'
  } finally { authenticating.value = false }
}
const authenticatedFetch = async (url: string, options: RequestInit = {}) => {
  const headers = new Headers(options.headers)
  headers.set('Authorization', `Bearer ${token.value}`)
  const response = await fetch(url, { ...options, headers })
  if (response.status === 401) { disconnect(); error.value = '访问令牌失效，请重新验证身份。' }
  return response
}
onMounted(async () => {
  try {
    const response = await fetch('/health', { signal: authController.signal })
    health.value = response.ok ? '后端已连接' : '后端未就绪'
  } catch { health.value = '后端未连接' }
})
onUnmounted(() => authController.abort())
</script>

<template>
  <div class="debug-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-icon">IT</span><div><strong>故障调试台</strong><small>Incident Agent</small></div></div>
      <p class="connection" :class="{ online: health === '后端已连接' }"><span></span>{{ health }}</p>
      <section class="auth-card">
        <h2>连接身份</h2>
        <template v-if="identity">
          <dl><dt>租户</dt><dd>{{ identity.tenant_id }}</dd><dt>用户</dt><dd>{{ identity.user_id }}</dd><dt>权限</dt><dd>{{ identity.role }}</dd></dl>
          <button class="secondary" @click="disconnect">切换身份 / 退出</button>
        </template>
        <form v-else @submit.prevent="authenticate">
          <label for="access-token">访问令牌</label>
          <input id="access-token" v-model="draftToken" type="password" autocomplete="off" spellcheck="false" maxlength="512" placeholder="粘贴本项目 token" :disabled="authenticating" />
          <button class="primary" type="submit" :disabled="authenticating">{{ authenticating ? '正在验证…' : '验证身份并连接' }}</button>
          <p class="muted">使用本地凭据文件中的 token。令牌只保存在当前页面内存中，刷新后重新输入。</p>
        </form>
        <p v-if="error" class="auth-error" role="alert">{{ error }}</p>
      </section>
      <section class="steps"><h2>调试流程</h2><ol><li>验证身份，选择服务</li><li>填写问题，保存工单</li><li>设置预算，启动诊断</li><li>查看报告、证据和事件</li></ol></section>
      <p class="sidebar-note">真实服务使用付费模型诊断，只有明确授权并点击启动才会调用。页面刷新与事件重连只读取已有运行。</p>
      <a class="docs-link" href="/docs" target="_blank" rel="noopener">打开后端接口文档 ↗</a>
    </aside>
    <div class="workspace">
      <header class="topbar"><div><span class="eyebrow">IT INCIDENT AGENT</span><h1>故障工单与诊断</h1></div><span class="badge">开发调试版</span></header>
      <IncidentPanel v-if="identity" :key="identityVersion" :ready="true" :tenant-id="identity.tenant_id" :user-id="identity.user_id" :role="identity.role" :fetcher="authenticatedFetch" />
      <main v-else class="welcome"><span class="welcome-mark">IT</span><h2>从一张故障工单开始</h2><p>在左侧验证访问令牌，然后选择已登记服务，查看五个 Agent 的排查过程与诊断结果。</p><div class="welcome-tags"><span>范围受限的只读工具</span><span>证据引用</span><span>显式调用预算</span></div></main>
    </div>
  </div>
</template>
