// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createApp, defineComponent, h, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import PluginAutoStartSwitch from './PluginAutoStartSwitch.vue'
import { usePluginStore } from '@/stores/plugin'

const apiMocks = vi.hoisted(() => ({
  getPlugins: vi.fn(),
  getPlugin: vi.fn(),
  getPluginSummaries: vi.fn(),
  getPluginStatus: vi.fn(),
  startPlugin: vi.fn(),
  stopPlugin: vi.fn(),
  reloadPlugin: vi.fn(),
  refreshPluginsRegistry: vi.fn(),
  setPluginAutoStart: vi.fn(),
}))

vi.mock('@/api/plugins', () => apiMocks)
vi.mock('@/i18n', () => ({ getLocale: () => 'en-US' }))
vi.mock('vue-i18n', () => ({
  useI18n: () => ({ t: (key: string) => key }),
}))

const elementPlusMocks = vi.hoisted(() => ({
  ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}))
vi.mock('element-plus', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...elementPlusMocks,
}))

function stubSwitch(app: ReturnType<typeof createApp>) {
  app.component('el-switch', defineComponent({
    props: { modelValue: Boolean, loading: Boolean, disabled: Boolean },
    emits: ['change'],
    setup(props, { emit }) {
      return () => h('button', {
        'data-checked': String(props.modelValue),
        'data-loading': String(props.loading),
        disabled: props.disabled,
        onClick: () => emit('change', !props.modelValue),
      })
    },
  }))
}

async function flushPromises() {
  for (let i = 0; i < 8; i += 1) await nextTick()
}

function mount(autoStart: boolean) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePluginStore()
  store.pluginSummaries = [{
    id: 'demo',
    name: 'Demo',
    description: 'Demo',
    version: '1.0.0',
    status: 'running',
    runtime_auto_start: autoStart,
  } as never]
  const root = document.createElement('div')
  const app = createApp(PluginAutoStartSwitch, { pluginId: 'demo' })
  app.use(pinia)
  stubSwitch(app)
  app.mount(root)
  const button = () => root.querySelector<HTMLButtonElement>('[data-testid="plugin-auto-start-switch"]')!
  return { app, button }
}

describe('PluginAutoStartSwitch', () => {
  beforeEach(() => {
    Object.values(apiMocks).forEach(mock => mock.mockReset())
    Object.values(elementPlusMocks.ElMessage).forEach(mock => mock.mockReset())
  })

  it('writes the auto-start preference without starting or stopping the plugin', async () => {
    let resolveRequest: (value: unknown) => void = () => {}
    apiMocks.setPluginAutoStart.mockImplementation(() => new Promise((resolve) => { resolveRequest = resolve }))
    const { app, button } = mount(true)
    apiMocks.getPluginSummaries.mockResolvedValue({ plugins: [{ id: 'demo', name: 'Demo', status: 'running', runtime_auto_start: false }] })

    expect(button().dataset.checked).toBe('true')
    button().click()
    await flushPromises()

    expect(apiMocks.setPluginAutoStart).toHaveBeenCalledWith('demo', false)
    expect(button().dataset.loading).toBe('true')
    expect(button().disabled).toBe(true)

    resolveRequest({ success: true, plugin_id: 'demo', auto_start: false })
    await vi.waitFor(() => expect(button().dataset.loading).toBe('false'))

    expect(button().dataset.checked).toBe('false')
    expect(elementPlusMocks.ElMessage.success).toHaveBeenCalledWith('messages.autoStartDisabled')
    expect(apiMocks.getPluginSummaries).toHaveBeenCalled()
    expect(apiMocks.startPlugin).not.toHaveBeenCalled()
    expect(apiMocks.stopPlugin).not.toHaveBeenCalled()
    app.unmount()
  })

  it('reports errors the interceptor does not surface and clears loading', async () => {
    apiMocks.setPluginAutoStart.mockRejectedValue({ response: { status: 404, data: {} } })
    const { app, button } = mount(false)

    button().click()
    await flushPromises()

    expect(apiMocks.setPluginAutoStart).toHaveBeenCalledWith('demo', true)
    expect(elementPlusMocks.ElMessage.error).toHaveBeenCalledTimes(1)
    expect(elementPlusMocks.ElMessage.success).not.toHaveBeenCalled()
    expect(button().dataset.loading).toBe('false')
    expect(button().dataset.checked).toBe('false')
    app.unmount()
  })
})
