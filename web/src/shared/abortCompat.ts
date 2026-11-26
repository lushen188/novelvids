/**
 * AbortSignal 兼容层。
 * AbortSignal.any 仅在 Chrome/Edge 116+、Firefox 124+、Safari 17.4+ 可用，
 * 旧内核浏览器下为 undefined，会导致 "AbortSignal.any is not a function"。
 * 此模块在原生 API 不可用时降级为手动聚合实现，与 cryptoCompat 思路一致。
 */

type AbortSignalStaticLike = { any?: (signals: AbortSignal[]) => AbortSignal }

/** 多信号聚合：原生 AbortSignal.any 可用时走原生，否则用 AbortController 手动聚合。 */
export function anySignal(signals: AbortSignal[]): AbortSignal {
  const native = (AbortSignal as AbortSignalStaticLike).any
  if (typeof native === 'function') return native.call(AbortSignal, signals)
  const controller = new AbortController()
  const cleanup: (() => void)[] = []
  const done = () => cleanup.forEach(fn => fn())
  for (const signal of signals) {
    if (signal.aborted) {
      controller.abort(signal.reason)
      break
    }
    const onAbort = () => controller.abort(signal.reason)
    signal.addEventListener('abort', onAbort, { once: true })
    cleanup.push(() => signal.removeEventListener('abort', onAbort))
  }
  if (controller.signal.aborted) done()
  else controller.signal.addEventListener('abort', done, { once: true })
  return controller.signal
}
