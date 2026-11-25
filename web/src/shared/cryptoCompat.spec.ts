import { expect, it } from 'vitest'
import { digestSha256, randomUUID, sha256Sync } from './cryptoCompat'

const toHex = (bytes: Uint8Array) => Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')

it('sha256Sync matches known SHA-256 vectors', () => {
  expect(toHex(sha256Sync(new TextEncoder().encode('')))).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')
  expect(toHex(sha256Sync(new TextEncoder().encode('abc')))).toBe('ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad')
})

it('sha256Sync matches native digest across padding boundaries', async () => {
  for (const length of [55, 56, 63, 64, 65, 1000, 3000]) {
    const input = 'x'.repeat(length)
    const native = toHex(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(input))))
    expect(toHex(sha256Sync(new TextEncoder().encode(input)))).toBe(native)
  }
})

it('sha256Sync matches native digest for UTF-8 multibyte input', async () => {
  const value = '中文测试数据·𝄞emoji🎉'.repeat(7)
  const native = toHex(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))))
  expect(toHex(sha256Sync(new TextEncoder().encode(value)))).toBe(native)
})

it('digestSha256 returns native-compatible bytes', async () => {
  const input = new TextEncoder().encode('hello 世界')
  expect(toHex(await digestSha256(input))).toBe(toHex(new Uint8Array(await crypto.subtle.digest('SHA-256', input))))
})

it('randomUUID produces valid v4 UUIDs', () => {
  for (let index = 0; index < 20; index++) {
    expect(randomUUID()).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
  }
  expect(new Set(Array.from({ length: 50 }, () => randomUUID())).size).toBe(50)
})
