<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

type Binding = { service: string; environment: string; service_version: string; chunk_size: number; chunk_overlap: number }
type Document = { document_id: string; source_name: string | null; category: string; updated_at: string; chunks: number }
type Preview = { document_id: string; source_name: string; expected_revision: string | null; preview_digest: string;
  replaces_existing: boolean; chunks: number; embedding_requests: number; chunk_size: number; chunk_overlap: number;
  strategy: string; samples: { index: number; characters: number; text: string }[]; samples_truncated: boolean }
const props = defineProps<{ fetcher: (url: string, options?: RequestInit) => Promise<Response> }>()
const bindings = ref<Binding[]>([]), selected = ref(''), category = ref('resource'), file = ref<File | null>(null)
const documents = ref<Document[]>([]), preview = ref<Preview | null>(null), authorized = ref(false)
const busy = ref(false), error = ref(''), notice = ref(''), listingError = ref(''), indexReady = ref(false)
const controller = new AbortController()
const bindingKey = (item: Binding) => JSON.stringify([item.service, item.environment, item.service_version])
const binding = computed(() => bindings.value.find(item => bindingKey(item) === selected.value))
const categories: Record<string, string> = { resource: '资源', dependency: '依赖', configuration: '配置' }
const invalidate = () => { preview.value = null; authorized.value = false; notice.value = ''; error.value = '' }
const params = () => {
  if (!binding.value) throw new Error('请先选择已配置 RAG 的服务。')
  return new URLSearchParams({ service: binding.value.service, environment: binding.value.environment,
    service_version: binding.value.service_version })
}
const request = async (url: string, options: RequestInit = {}) => {
  const response = await props.fetcher(url, { ...options, signal: controller.signal })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败（${response.status}）`)
  return data
}
const refresh = async () => {
  documents.value = []; listingError.value = ''; indexReady.value = false
  if (!binding.value) return
  try {
    const data = await request(`/api/v1/runbooks?${params()}`)
    documents.value = data.items; indexReady.value = data.index_ready
  } catch (e) { if (!controller.signal.aborted) listingError.value = e instanceof Error ? e.message : '资料列表读取失败' }
}
const guarded = async (work: () => Promise<void>) => {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { await work() } catch (e) {
    if (!controller.signal.aborted) error.value = e instanceof Error ? e.message : '无法连接后端'
  } finally { busy.value = false }
}
const selectFile = (event: Event) => {
  invalidate(); file.value = null
  const candidate = (event.target as HTMLInputElement).files?.[0]
  if (!candidate) return
  if (!/\.(md|txt)$/i.test(candidate.name)) { error.value = '仅支持 .md 和 .txt 文件。'; return }
  if (!candidate.size || candidate.size > 256 * 1024) { error.value = '请选择非空文件，大小不超过 256 KiB。'; return }
  file.value = candidate
}
const previewFile = () => guarded(async () => {
  invalidate()
  if (!file.value) throw new Error('请先选择手册文件。')
  const query = params(); query.set('filename', file.value.name); query.set('category', category.value)
  preview.value = await request(`/api/v1/runbooks/preview?${query}`, {
    method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file.value,
  })
})
const upload = () => guarded(async () => {
  if (!file.value || !preview.value || !authorized.value) throw new Error('请先预览并确认向量化费用。')
  const query = params(); query.set('filename', file.value.name); query.set('category', category.value)
  query.set('preview_digest', preview.value.preview_digest)
  query.set('expected_revision', preview.value.expected_revision ?? '')
  query.set('embedding_authorized', 'true')
  try {
    const result = await request(`/api/v1/runbooks/upload?${query}`, {
      method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file.value,
    })
    preview.value = null; authorized.value = false
    const usage = result.usage
    const tokens = usage.token_usage.status === 'known' ? `${usage.token_usage.input_tokens} 个输入词元` : '词元用量部分未知'
    notice.value = `已导入 ${result.source_name}，共 ${result.chunks} 个片段；向量请求 ${usage.model_calls} 次，${tokens}。`
    await refresh()
  } catch (e) {
    preview.value = null; authorized.value = false
    await refresh()
    throw new Error(`${e instanceof Error ? e.message : '导入请求中断'} 请核对下方资料列表；连接中断时服务端可能仍在处理，请勿立即重复提交。`)
  }
})
watch(category, invalidate)
watch(selected, () => { invalidate(); void guarded(refresh) })
onMounted(() => { void guarded(async () => {
  const data = await request('/api/v1/runbooks/services')
  bindings.value = data.items
  selected.value = bindings.value[0] ? bindingKey(bindings.value[0]) : ''
  await refresh()
}) })
onUnmounted(() => controller.abort())
</script>

<template>
  <details class="runbooks">
    <summary>排障手册 · 上传与切分</summary>
    <div class="body">
      <p class="hint">上传 UTF-8 编码的 .md 或 .txt 文件（最大 256 KiB）。免费预览后再确认导入；同一服务范围内同名文件会更新原手册。</p>
      <p v-if="!bindings.length && !busy && !error" class="hint">当前身份下没有已配置 RAG 的服务，请先在服务配置中启用 runbook_rag。</p>
      <form @submit.prevent="previewFile">
        <div class="fields">
          <label>服务范围<select v-model="selected" :disabled="busy || !bindings.length"><option value="" disabled>选择服务</option><option v-for="item in bindings" :key="bindingKey(item)" :value="bindingKey(item)">{{ item.service }} / {{ item.environment }} / {{ item.service_version }}</option></select></label>
          <label>手册分类<select v-model="category" :disabled="busy"><option v-for="(label, value) in categories" :key="value" :value="value">{{ label }}</option></select></label>
          <label>选择文件<input type="file" accept=".md,.txt" :disabled="busy || !binding" @change="selectFile" /></label>
        </div>
        <p v-if="binding" class="hint">Markdown 按 # 标题分节并保留标题上下文；TXT 按段落切分。每片最多 {{ binding.chunk_size }} 字，重叠最多 {{ binding.chunk_overlap }} 字。</p>
        <button type="submit" :disabled="busy || !file || !binding">{{ busy ? '处理中…' : '免费预览切分' }}</button>
      </form>
      <section v-if="preview" class="preview">
        <p><strong>{{ preview.source_name }}</strong> · {{ preview.chunks }} 个片段 · 预计 {{ preview.embedding_requests }} 次向量请求</p>
        <p v-if="preview.replaces_existing" class="replace">导入将更新这篇同名手册，保留其他手册。预览后资料发生变化时需重新预览。</p>
        <details v-for="sample in preview.samples" :key="sample.index"><summary>片段 {{ sample.index }} · {{ sample.characters }} 字</summary><pre>{{ sample.text }}</pre></details>
        <p v-if="preview.samples_truncated" class="hint">仅展示前 5 个片段，导入会保存全部片段。</p>
        <label class="consent"><input v-model="authorized" type="checkbox" :disabled="busy" />我确认调用向量服务导入本篇手册，可能产生费用。</label>
        <button type="button" :disabled="busy || !authorized" @click="upload">{{ busy ? '导入中…' : '确认并导入' }}</button>
      </section>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <p v-if="notice" class="success" role="status">{{ notice }}</p>
      <div class="list-title"><strong>当前范围的手册</strong><button type="button" :disabled="busy || !binding" @click="guarded(refresh)">刷新列表</button></div>
      <p v-if="listingError" class="error" role="alert">{{ listingError }}</p>
      <p v-else-if="!binding" class="hint">选择服务后查看手册。</p>
      <p v-else-if="!documents.length" class="hint">{{ indexReady ? '还没有手册资料。' : '索引尚未建立，首次导入会初始化知识表。' }}</p>
      <div v-else class="table-wrap"><table><thead><tr><th>手册</th><th>分类</th><th>片段数</th><th>更新时间</th></tr></thead><tbody><tr v-for="item in documents" :key="item.document_id"><td>{{ item.source_name || item.document_id }}</td><td>{{ categories[item.category] }}</td><td>{{ item.chunks }}</td><td>{{ new Date(item.updated_at).toLocaleString('zh-CN') }}</td></tr></tbody></table></div>
    </div>
  </details>
</template>

<style scoped>
.runbooks { margin: 24px 28px 0; border: 1px solid #dce3ed; background: white; border-radius: 12px; }
summary { cursor: pointer; padding: 16px; font-size: 14px; font-weight: 650; }
.body { padding: 0 20px 20px; }.hint { color: #66758c; font-size: 13px; line-height: 1.8; }
.fields { display: grid; grid-template-columns: 1.3fr .6fr 1fr; gap: 14px; }
label { display: block; font-size: 13px; line-height: 1.8; }select, input[type=file] { display: block; width: 100%; margin-top: 7px; padding: 9px; border: 1px solid #c9d5e5; border-radius: 6px; background: white; }
button { border: 1px solid #3973e5; background: #3973e5; color: white; border-radius: 6px; padding: 8px 13px; font-size: 13px; }
.preview { margin-top: 18px; padding: 16px; background: #f6f8fc; border: 1px solid #dce3ed; border-radius: 8px; font-size: 13px; }
.preview summary { padding: 10px 0; }.preview pre { margin: 0; padding: 12px; background: white; white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.7; }
.consent { margin: 14px 0; }.replace { color: #8a591c; }.error { color: #b33535; font-size: 13px; line-height: 1.8; }.success { color: #22744c; font-size: 13px; line-height: 1.8; }
.list-title { display: flex; justify-content: space-between; align-items: center; margin-top: 22px; font-size: 14px; }.list-title button { background: white; color: #3973e5; }
.table-wrap { overflow: auto; }table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; text-align: left; }th, td { padding: 10px; border-bottom: 1px solid #e3e8f0; }td:first-child { overflow-wrap: anywhere; }
@media (max-width: 900px) { .runbooks { margin: 18px 18px 0; }.fields { grid-template-columns: 1fr; } }
</style>
