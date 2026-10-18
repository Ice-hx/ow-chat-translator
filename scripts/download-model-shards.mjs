import { createWriteStream, existsSync, statSync, mkdirSync } from 'node:fs'
import { Readable } from 'node:stream'
import { pipeline } from 'node:stream/promises'
import { dirname } from 'node:path'

const TARGETS = [
  {
    name: '7B shard 1/2',
    file: 'qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf',
    approx: 3995896881,
  },
  {
    name: '7B shard 2/2',
    file: 'qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf',
    approx: 686000000,
  },
].map((t) => ({
  ...t,
  url: 'https://modelscope.cn/api/v1/models/Qwen/Qwen2.5-7B-Instruct-GGUF/repo?Revision=master&FilePath=' + t.file,
  out: 'D:/DSH-Lab/ow-translator/models/' + t.file,
}))

const fmt = (n) => (n / 1024 / 1024).toFixed(1) + ' MB'

async function one(t, attempt) {
  mkdirSync(dirname(t.out), { recursive: true })
  if (existsSync(t.out) && statSync(t.out).size > t.approx * 0.95) {
    console.log(`[skip] ${t.name}`)
    return
  }
  console.log(`[start] ${t.name} (尝试 ${attempt})`)
  const res = await fetch(t.url, { redirect: 'follow' })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const total = Number(res.headers.get('content-length') || 0)
  let got = 0
  let last = 0
  const body = Readable.fromWeb(res.body)
  body.on('data', (c) => {
    got += c.length
    const now = Date.now()
    if (now - last > 8000) {
      last = now
      console.log(`[progress] ${t.name} ${total ? ((got / total) * 100).toFixed(1) + '%' : fmt(got)} (${fmt(got)})`)
    }
  })
  await pipeline(body, createWriteStream(t.out))
  console.log(`[done] ${t.name} ${fmt(statSync(t.out).size)}`)
}

for (const t of TARGETS) {
  for (let i = 1; i <= 3; i++) {
    try {
      await one(t, i)
      break
    } catch (e) {
      console.log(`[error] ${t.name}: ${e.message}`)
      if (i === 3) process.exitCode = 1
      await new Promise((r) => setTimeout(r, 3000))
    }
  }
}
console.log('[all done]')
