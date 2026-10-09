import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { getAllMetrics } from '@/api/metrics'
import { useMetricsStore } from './metrics'

vi.mock('@/api/metrics', () => ({
  getAllMetrics: vi.fn(),
  getPluginMetrics: vi.fn(),
  getPluginMetricsHistory: vi.fn(),
}))

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}

const metric = (plugin_id: string) => ({ plugin_id, timestamp: '2026-10-09T00:00:00Z' }) as any

describe('metrics store fetchAllMetrics', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    vi.mocked(getAllMetrics).mockReset()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('drops a response that arrives after a newer request took over', async () => {
    const stale = deferred<any>()
    const fresh = deferred<any>()
    vi.mocked(getAllMetrics).mockReturnValueOnce(stale.promise).mockReturnValueOnce(fresh.promise)
    const store = useMetricsStore()

    const first = store.fetchAllMetrics()
    // The 15s guard releases the slot while the first request is still in flight.
    vi.advanceTimersByTime(15000)
    const second = store.fetchAllMetrics()

    fresh.resolve({ metrics: [metric('alive')], global: { total: 1 } })
    await expect(second).resolves.toMatchObject({ global: { total: 1 } })
    expect(Object.keys(store.currentMetrics)).toEqual(['alive'])

    stale.resolve({ metrics: [metric('alive'), metric('stopped')], global: { total: 2 } })
    await expect(first).resolves.toBeUndefined()
    expect(Object.keys(store.currentMetrics)).toEqual(['alive'])
    expect(store.allMetrics.map((m) => m.plugin_id)).toEqual(['alive'])
  })

  it('does not clear the pending slot of a newer request when an old one settles', async () => {
    const stale = deferred<any>()
    const fresh = deferred<any>()
    vi.mocked(getAllMetrics).mockReturnValueOnce(stale.promise).mockReturnValueOnce(fresh.promise)
    const store = useMetricsStore()

    store.fetchAllMetrics()
    vi.advanceTimersByTime(15000)
    const second = store.fetchAllMetrics()

    stale.resolve({ metrics: [] })
    await Promise.resolve()
    await Promise.resolve()
    // The newer request is still pending, so a third call joins it instead of starting another.
    const third = store.fetchAllMetrics()
    expect(store.loading).toBe(true)
    expect(getAllMetrics).toHaveBeenCalledTimes(2)

    fresh.resolve({ metrics: [], global: { total: 0 } })
    await expect(third).resolves.toMatchObject({ global: { total: 0 } })
    await second
    expect(store.loading).toBe(false)
  })
})
