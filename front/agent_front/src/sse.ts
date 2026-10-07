// Bearer headers are supplied by the caller. A subscription never starts a run.
export async function readEvents<T>(response: Response, receive: (event: T) => void | Promise<void>) {
  if (!response.body) throw new Error('流式响应不可用')
  const reader = response.body.getReader(), decoder = new TextDecoder()
  let buffer = ''
  const deliver = async (part: string) => {
    const data = part.split(/\r?\n/).filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
    if (data) await receive(JSON.parse(data) as T)
  }
  try {
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      const parts = buffer.split(/\r?\n\r?\n/)
      buffer = parts.pop() || ''
      for (const part of parts) await deliver(part)
      if (done) break
    }
    if (buffer.trim()) await deliver(buffer)
  } finally { reader.releaseLock() }
}
