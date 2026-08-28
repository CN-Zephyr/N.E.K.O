// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { del } from './index'
import { clearPluginLogs } from './logs'

vi.mock('./index', () => ({
  del: vi.fn(),
  get: vi.fn(),
}))

beforeEach(() => {
  vi.clearAllMocks()
})

describe('logs API', () => {
  it('clears only the requested plugin logs and owns the error message', async () => {
    vi.mocked(del).mockResolvedValue({ plugin_id: 'demo', cleared_files: 2, cleared_bytes: 42 })

    await clearPluginLogs('demo/plugin')

    expect(del).toHaveBeenCalledWith('/plugin/demo%2Fplugin/logs', {
      suppressErrorMessage: true,
    })
  })
})
