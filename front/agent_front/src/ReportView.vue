<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ text: string }>()
// Vue text interpolation escapes source text. Reports never enter v-html.
const blocks = computed(() => {
  const result: { tag: string; text: string; list: boolean }[] = []
  let inCode = false, code = ''
  for (const line of props.text.split('\n')) {
    if (line.trim().startsWith('```')) {
      if (inCode) { result.push({ tag: 'pre', text: code, list: false }); code = '' }
      inCode = !inCode; continue
    }
    if (inCode) { code += line + '\n'; continue }
    if (!line.trim()) continue
    const heading = line.match(/^(#{1,3})\s+(.+)$/)
    result.push({ tag: heading ? `h${heading[1]!.length + 1}` : 'p',
      text: heading ? heading[2]! : line.replace(/^\s*[-*]\s+/, ''), list: /^\s*[-*]\s+/.test(line) })
  }
  if (inCode) result.push({ tag: 'pre', text: code, list: false })
  return result
})
const fragments = (text: string) => text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map(value => ({
  tag: value.startsWith('**') ? 'strong' : value.startsWith('`') ? 'code' : 'span',
  text: value.startsWith('**') ? value.slice(2, -2) : value.startsWith('`') ? value.slice(1, -1) : value,
}))
</script>

<template>
  <article class="diagnosis-report" aria-label="诊断报告">
    <component v-for="(block, index) in blocks" :key="index" :is="block.tag" :class="{ 'list-line': block.list }">
      <template v-if="block.tag === 'pre'">{{ block.text }}</template>
      <template v-else><component v-for="(fragment, i) in fragments(block.text)" :key="i" :is="fragment.tag">{{ fragment.text }}</component></template>
    </component>
  </article>
</template>

<style scoped>
.diagnosis-report { line-height: 1.85; padding: 20px; border: 1px solid #dce5ef; border-radius: 9px; background: #fbfcfe; overflow-wrap: anywhere; font-size: 14px; }
h2 { font-size: 20px; margin: 0 0 15px; } h3, h4 { font-size: 16px; margin: 18px 0 8px; } p { margin: 8px 0; white-space: pre-wrap; }
.list-line { padding-left: 16px; position: relative; }.list-line::before { content: '•'; position: absolute; left: 0; color: #71819a; }
code { background: #eaf0fa; border-radius: 4px; padding: 2px 5px; } pre { white-space: pre-wrap; background: #eef2f8; padding: 12px; }
</style>
