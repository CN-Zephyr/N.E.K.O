<template>
  <div class="plugin-detail" data-yui-guide-id="plugin-detail-page">
    <!-- Loading 状态 -->
    <div v-if="loading" class="loading-container">
      <el-icon class="is-loading" :size="32"><Loading /></el-icon>
      <span>{{ $t('common.loading') }}</span>
    </div>

    <el-card v-else-if="plugin" data-yui-guide-id="plugin-detail-card">
      <template #header>
        <div class="card-header" data-yui-guide-id="plugin-detail-header">
          <div class="header-left" data-yui-guide-id="plugin-detail-title">
            <el-button :icon="ArrowLeft" data-yui-guide-id="plugin-detail-back" @click="goBack">{{ $t('common.back') }}</el-button>
            <h2>{{ pluginDisplayText.name }}</h2>
          </div>
          <div data-yui-guide-id="plugin-detail-actions">
            <PluginActions :plugin-id="pluginId" />
          </div>
        </div>
      </template>

      <el-tabs v-model="activeTab" data-yui-guide-id="plugin-detail-tabs">
        <el-tab-pane v-if="displayedPanelSurfaces.length > 0" :label="$t('plugins.ui.panel')" name="panel">
          <div class="surface-section" data-yui-guide-id="plugin-detail-panel">
            <el-alert
              v-if="surfaceWarnings.length > 0"
              class="surface-warning"
              type="warning"
              show-icon
              :closable="false"
            >
              <template #title>{{ $t('plugins.ui.surfaceWarnings') }}</template>
              <ul class="surface-warning__list">
                <li v-for="warning in surfaceWarnings" :key="`${warning.path}:${warning.code}:${warning.message}`">
                  <code>{{ warning.path }}</code>
                  <span>{{ warning.message }}</span>
                </li>
              </ul>
            </el-alert>
            <el-tabs v-if="displayedPanelSurfaces.length > 1" v-model="activePanelSurfaceId" type="border-card">
              <el-tab-pane
                v-for="surface in displayedPanelSurfaces"
                :key="surface.id"
                :label="surface.title || surface.id"
                :name="surface.id"
              >
                <HostedSurfaceFrame
                  :ref="(instance) => setPanelSurfaceFrameRef(surface.id, instance)"
                  :plugin-id="pluginId"
                  :surface="surface"
                  :height="hostedSurfaceFrameHeight"
                  :active="isSurfaceActive(surface)"
                  :activation-revision="activationRevisionFor(surface)"
                  @open-logs="openLogsTab"
                  @message="relayHostedSurfaceMessageToStaticUi"
                />
              </el-tab-pane>
            </el-tabs>
            <HostedSurfaceFrame
              v-else
              :ref="(instance) => setPanelSurfaceFrameRef(displayedPanelSurfaces[0]?.id || '', instance)"
              :plugin-id="pluginId"
              :surface="displayedPanelSurfaces[0]!"
              :height="hostedSurfaceFrameHeight"
              :active="isSurfaceActive(displayedPanelSurfaces[0]!)"
              :activation-revision="activationRevisionFor(displayedPanelSurfaces[0]!)"
              @open-logs="openLogsTab"
              @message="relayHostedSurfaceMessageToStaticUi"
            />
          </div>
        </el-tab-pane>

        <el-tab-pane v-if="guideSurfaces.length > 0" :label="$t('plugins.ui.guide')" name="guide">
          <div class="surface-section" data-yui-guide-id="plugin-detail-guide">
            <el-alert
              v-if="surfaceWarnings.length > 0"
              class="surface-warning"
              type="warning"
              show-icon
              :closable="false"
            >
              <template #title>{{ $t('plugins.ui.surfaceWarnings') }}</template>
              <ul class="surface-warning__list">
                <li v-for="warning in surfaceWarnings" :key="`${warning.path}:${warning.code}:${warning.message}`">
                  <code>{{ warning.path }}</code>
                  <span>{{ warning.message }}</span>
                </li>
              </ul>
            </el-alert>
            <el-tabs v-if="guideSurfaces.length > 1" v-model="activeGuideSurfaceId" type="border-card">
              <el-tab-pane
                v-for="surface in guideSurfaces"
                :key="surface.id"
                :label="surface.title || surface.id"
                :name="surface.id"
              >
                <HostedSurfaceFrame
                  :plugin-id="pluginId"
                  :surface="surface"
                  :height="hostedSurfaceFrameHeight"
                  :active="isSurfaceActive(surface)"
                  :activation-revision="activationRevisionFor(surface)"
                  @open-logs="openLogsTab"
                  @message="relayHostedSurfaceMessageToStaticUi"
                />
              </el-tab-pane>
            </el-tabs>
            <HostedSurfaceFrame
              v-else
              :plugin-id="pluginId"
              :surface="guideSurfaces[0]!"
              :height="hostedSurfaceFrameHeight"
              :active="isSurfaceActive(guideSurfaces[0]!)"
              :activation-revision="activationRevisionFor(guideSurfaces[0]!)"
              @open-logs="openLogsTab"
              @message="relayHostedSurfaceMessageToStaticUi"
            />
          </div>
        </el-tab-pane>

        <el-tab-pane :label="$t('plugins.basicInfo')" name="info">
          <div class="info-section" data-yui-guide-id="plugin-detail-info">
            <el-descriptions :column="2" border>
              <el-descriptions-item :label="$t('plugins.id')">{{ plugin.id }}</el-descriptions-item>
              <el-descriptions-item :label="$t('plugins.version')">{{ plugin.version }}</el-descriptions-item>
              <el-descriptions-item :label="$t('market.filterLabels.author')" :span="2">
                {{ authorDisplay || $t('common.noData') }}
              </el-descriptions-item>
              <el-descriptions-item :label="$t('plugins.description')" :span="2">{{ pluginDisplayText.description || $t('common.noData') }}</el-descriptions-item>
              <el-descriptions-item :label="$t('plugins.pluginType')">
                <el-tag size="small" :type="pluginTypeTagType">
                  {{ $t(pluginTypeText) }}
                </el-tag>
              </el-descriptions-item>
              <el-descriptions-item :label="$t('plugins.sdkVersion')">{{ plugin.sdk_version || $t('common.nA') }}</el-descriptions-item>
              <el-descriptions-item :label="$t('plugins.autoStart')">
                <el-tag size="small" :type="plugin.autoStart ? 'success' : 'warning'">
                  {{ plugin.autoStart ? $t('plugins.autoStart') : $t('plugins.manualStart') }}
                </el-tag>
              </el-descriptions-item>
              <el-descriptions-item :label="$t('plugins.status')">
                <StatusIndicator :status="pluginStatus" />
              </el-descriptions-item>
            </el-descriptions>

            <section class="candidate-section" data-yui-guide-id="plugin-detail-candidates">
              <div class="candidate-section__header">
                <div>
                  <h3>{{ $t('plugins.candidates.title') }}</h3>
                  <p>{{ $t('plugins.candidates.hint') }}</p>
                </div>
                <el-icon v-if="candidateLoading" class="is-loading"><Loading /></el-icon>
              </div>
              <el-alert
                v-if="candidateError"
                type="warning"
                show-icon
                :closable="false"
                :title="candidateError"
              />
              <div v-else-if="candidateState?.candidates.length" class="candidate-list">
                <div
                  v-for="candidate in candidateState.candidates"
                  :key="candidateKey(candidate.key)"
                  class="candidate-row"
                  :data-candidate-key="candidateKey(candidate.key)"
                >
                  <div class="candidate-row__identity">
                    <div class="candidate-row__title">
                      <el-tag size="small" :type="candidate.source === 'builtin' ? 'info' : 'primary'">
                        {{ $t(`plugins.installSource.channel.${candidate.source}`) }}
                      </el-tag>
                      <strong>{{ candidate.version || $t('common.nA') }}</strong>
                    </div>
                    <code>{{ candidate.key.root_id }}/{{ candidate.key.directory_name }}</code>
                    <span v-if="candidate.error" class="candidate-row__error">{{ candidate.error }}</span>
                  </div>
                  <div class="candidate-row__status">
                    <el-tag v-if="candidate.running" size="small" type="success">
                      {{ $t('plugins.candidates.running') }}
                    </el-tag>
                    <el-tag v-if="candidate.effective" size="small">
                      {{ $t('plugins.candidates.current') }}
                    </el-tag>
                    <el-tag v-if="!candidate.valid" size="small" type="danger">
                      {{ $t('plugins.candidates.invalid') }}
                    </el-tag>
                    <el-tag v-if="candidate.requires_shared_state_authorization" size="small" type="warning">
                      {{ $t('plugins.candidates.dataAccessRequired') }}
                    </el-tag>
                    <el-button
                      size="small"
                      type="primary"
                      :data-candidate-action="candidateKey(candidate.key)"
                      :disabled="(candidate.registered && candidate.selected) || !candidate.valid"
                      :loading="switchingCandidateKey === candidateKey(candidate.key)"
                      @click="switchCandidate(candidate)"
                    >
                      {{ candidate.registered && candidate.selected ? $t('plugins.candidates.current') : $t('plugins.candidates.switch') }}
                    </el-button>
                  </div>
                </div>
              </div>
              <el-empty v-else-if="!candidateLoading" :description="$t('plugins.candidates.none')" />
            </section>

          </div>
        </el-tab-pane>

        <el-tab-pane :label="$t('plugins.entries')" name="entries">
          <div data-yui-guide-id="plugin-detail-entries">
            <EntryList :entries="plugin.entries || []" :plugin-id="pluginId" :plugin-status="pluginStatus" />
          </div>
        </el-tab-pane>

        <el-tab-pane :label="$t('plugins.performance')" name="metrics">
          <div data-yui-guide-id="plugin-detail-metrics">
            <MetricsCard :plugin-id="pluginId" />
          </div>
        </el-tab-pane>

        <el-tab-pane :label="$t('plugins.config')" name="config">
          <div data-yui-guide-id="plugin-detail-config">
            <PluginConfigEditor :plugin-id="pluginId" />
          </div>
        </el-tab-pane>

        <el-tab-pane :label="$t('plugins.logs')" name="logs">
          <div data-yui-guide-id="plugin-detail-logs">
            <LogViewer :plugin-id="pluginId" />
          </div>
        </el-tab-pane>

      </el-tabs>
    </el-card>

    <EmptyState v-else-if="!loading" :description="$t('plugins.pluginNotFound')" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, Loading } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { usePluginStore } from '@/stores/plugin'
import StatusIndicator from '@/components/common/StatusIndicator.vue'
import PluginActions from '@/components/plugin/PluginActions.vue'
import EntryList from '@/components/plugin/EntryList.vue'
import MetricsCard from '@/components/metrics/MetricsCard.vue'
import PluginConfigEditor from '@/components/plugin/PluginConfigEditor.vue'
import LogViewer from '@/components/logs/LogViewer.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import HostedSurfaceFrame from '@/components/plugin/HostedSurfaceFrame.vue'
import { getPluginCandidates, getPluginUiSurfaceInfo, selectPluginCandidate } from '@/api/plugins'
import { resolvePluginDisplayText, type PluginDisplayText } from '@/utils/pluginDisplay'
import { useI18n } from 'vue-i18n'
import type {
  PluginCandidateInventory,
  PluginCandidateItem,
  PluginCandidateKey,
  PluginUiSurface,
  PluginUiWarning,
} from '@/types/api'

const route = useRoute()
const router = useRouter()
const pluginStore = usePluginStore()
const { locale, t } = useI18n()

const pluginId = computed(() => route.params.id as string)
const activeTab = ref('info')
const loading = ref(true)
const surfaces = ref<PluginUiSurface[]>([])
const surfaceWarnings = ref<PluginUiWarning[]>([])
const candidateState = ref<PluginCandidateInventory | null>(null)
const candidateLoading = ref(false)
const candidateError = ref('')
const switchingCandidateKey = ref('')
const activePanelSurfaceId = ref('')
const activeGuideSurfaceId = ref('')
type SurfaceMessageReceiver = {
  sendSurfaceMessage: (data: unknown) => void
  refreshContext: () => Promise<void>
}
const panelSurfaceFrameRefs = new Map<string, SurfaceMessageReceiver>()
const surfaceActivationRevisions = ref<Record<string, number>>({})
const hostedSurfaceFrameHeight = 'clamp(560px, calc(100vh - 220px), 1200px)'
const allowedTabs = new Set(['panel', 'guide', 'ui', 'info', 'entries', 'metrics', 'config', 'logs'])
let currentSurfaceLoadId = 0
let currentCandidateLoadId = 0

const plugin = computed(() => {
  return pluginStore.pluginsWithStatus.find(p => p.id === pluginId.value)
})

const emptyPluginDisplayText: PluginDisplayText = {
  name: '',
  description: '',
  shortDescription: '',
}

const pluginDisplayText = computed(() => {
  return plugin.value ? resolvePluginDisplayText(plugin.value, locale.value) : emptyPluginDisplayText
})

const authorDisplay = computed(() => {
  const author = plugin.value?.author
  if (!author) return ''
  if (author.name && author.email) return `${author.name} <${author.email}>`
  return author.name || author.email || ''
})

const panelSurfaces = computed(() => surfaces.value.filter((surface) => surface.kind === 'panel'))
const guideSurfaces = computed(() => surfaces.value.filter((surface) => surface.kind === 'guide' || surface.kind === 'docs'))
const availablePanelSurfaces = computed(() => panelSurfaces.value.filter((surface) => surface.available !== false))
// `auto` is accepted by the manifest but does not have a renderer yet. Do not
// let its placeholder hide a working legacy static UI.
const renderablePanelSurfaces = computed(() => availablePanelSurfaces.value.filter((surface) => surface.mode !== 'auto'))
const availableDeclaredPanelSurfaces = computed(() => renderablePanelSurfaces.value.filter((surface) => !surface.legacy_static_compat))
// Keep every renderable panel, including the host-generated static `main`
// compatibility surface. The separate legacy "界面" tab is what gets hidden
// when panels exist; filtering main here would make that page unreachable.
const displayedPanelSurfaces = computed(() => renderablePanelSurfaces.value)
// A generated static `main` is inserted before declared panels by the backend.
// Keep it accessible in the list, but let generic `?tab=panel` entry points
// select the first declared hosted panel when one exists.
const defaultPanelSurface = computed(() => {
  return availableDeclaredPanelSurfaces.value.find((surface) => surface.mode === 'hosted-tsx')
    ?? availableDeclaredPanelSurfaces.value[0]
    ?? displayedPanelSurfaces.value[0]
})
const hasDisplayablePanelSurface = computed(() => displayedPanelSurfaces.value.length > 0)

const isAdapter = computed(() => plugin.value?.type === 'adapter')

// 获取插件类型显示文本
const pluginTypeText = computed(() => {
  if (isAdapter.value) return 'plugins.typeAdapter'
  return 'plugins.pluginTypeNormal'
})

// 获取插件类型标签颜色
const pluginTypeTagType = computed(() => {
  if (isAdapter.value) return 'warning'
  return 'info'
})

// 确保 status 始终是字符串类型
const pluginStatus = computed(() => {
  if (!plugin.value) return 'stopped'
  const status = plugin.value.status
  if (typeof status === 'object' && status !== null) {
    return (status as any).status || 'stopped'
  }
  return typeof status === 'string' ? status : 'stopped'
})

function goBack() {
  router.push('/plugins')
}

function candidateKey(candidate: PluginCandidateKey): string {
  return `${candidate.root_id}:${candidate.directory_name}`
}

async function fetchCandidates(): Promise<void> {
  const loadId = ++currentCandidateLoadId
  const currentPluginId = pluginId.value
  candidateLoading.value = true
  candidateError.value = ''
  try {
    const result = await getPluginCandidates(currentPluginId)
    if (loadId !== currentCandidateLoadId || currentPluginId !== pluginId.value) return
    candidateState.value = result
  } catch (caught: any) {
    if (loadId !== currentCandidateLoadId || currentPluginId !== pluginId.value) return
    candidateState.value = null
    candidateError.value = caught?.response?.data?.detail || caught?.message || t('plugins.candidates.loadFailed')
  } finally {
    if (loadId === currentCandidateLoadId) candidateLoading.value = false
  }
}

async function switchCandidate(candidate: PluginCandidateItem): Promise<void> {
  const key = candidateKey(candidate.key)
  if (candidate.requires_shared_state_authorization) {
    try {
      await ElMessageBox.confirm(
        t('plugins.candidates.dataAccessPrompt', {
          source: t(`plugins.installSource.channel.${candidate.source}`),
        }),
        t('plugins.candidates.dataAccessTitle'),
        {
          confirmButtonText: t('plugins.candidates.dataAccessConfirm'),
          cancelButtonText: t('common.cancel'),
          type: 'warning',
        },
      )
    } catch {
      return
    }
  }
  switchingCandidateKey.value = key
  try {
    await selectPluginCandidate(
      pluginId.value,
      candidate.key,
      candidate.requires_shared_state_authorization,
    )
    await Promise.all([
      pluginStore.fetchPlugins(),
      pluginStore.fetchPluginStatus(pluginId.value),
      fetchCandidates(),
    ])
    ElMessage.success(t('plugins.candidates.switchSuccess'))
  } catch {
    ElMessage.error(t('plugins.candidates.switchFailed'))
  } finally {
    switchingCandidateKey.value = ''
  }
}

function resolveActiveTab(value: unknown): string {
  return typeof value === 'string' && allowedTabs.has(value) ? value : 'info'
}

function resolveDefaultTab(value: unknown): string {
  const requested = resolveActiveTab(value)
  if (requested === 'panel' && !hasDisplayablePanelSurface.value) return 'info'
  if (requested === 'guide' && guideSurfaces.value.length === 0) return 'info'
  if (requested === 'ui' && hasDisplayablePanelSurface.value) return 'panel'
  if (requested === 'ui') return 'info'
  return requested
}

function syncActiveTab(requestedTab: unknown) {
  const nextTab = resolveDefaultTab(requestedTab)
  activeTab.value = nextTab
  if (requestedTab === 'ui' && nextTab !== 'ui') {
    void router.replace({
      query: {
        ...route.query,
        tab: nextTab,
      },
    })
  }
}

function syncSurfaceTabs() {
  const requestedSurfaceId = typeof route.query.surface === 'string' ? route.query.surface : ''
  const requestedTab = resolveActiveTab(route.query.tab)
  if (requestedSurfaceId) {
    const panel = requestedTab !== 'guide'
      ? displayedPanelSurfaces.value.find((surface) => surface.id === requestedSurfaceId)
      : undefined
    if (panel) {
      activePanelSurfaceId.value = panel.id
    }
    const guide = requestedTab !== 'panel'
      ? guideSurfaces.value.find((surface) => surface.id === requestedSurfaceId)
      : undefined
    if (guide) {
      activeGuideSurfaceId.value = guide.id
    }
  }
  if (!activePanelSurfaceId.value && defaultPanelSurface.value) {
    activePanelSurfaceId.value = defaultPanelSurface.value.id
  }
  if (!activeGuideSurfaceId.value && guideSurfaces.value[0]) {
    activeGuideSurfaceId.value = guideSurfaces.value[0].id
  }
}

function openLogsTab() {
  activeTab.value = 'logs'
  router.replace({
    query: {
      ...route.query,
      tab: 'logs',
    },
  })
}

function surfaceActivationKey(surface: Pick<PluginUiSurface, 'kind' | 'id'>): string {
  return `${pluginId.value}:${surface.kind}:${surface.id}`
}

function activationRevisionFor(surface: Pick<PluginUiSurface, 'kind' | 'id'>): number {
  return surfaceActivationRevisions.value[surfaceActivationKey(surface)] ?? 0
}

function isSurfaceActive(surface: Pick<PluginUiSurface, 'kind' | 'id'>): boolean {
  if (surface.kind === 'panel') {
    return activeTab.value === 'panel' && activePanelSurfaceId.value === surface.id
  }
  return activeTab.value === 'guide' && activeGuideSurfaceId.value === surface.id
}

function isActivationRevision(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

function openHostedSurfaceFromStaticUi(payload: { pluginId?: string; surfaceId: string; kind?: string; activationRevision?: unknown }) {
  if (payload.pluginId && payload.pluginId !== pluginId.value) return
  let activeSurface: PluginUiSurface | undefined
  let activeSurfaceId = ''
  const preferPanel = payload.kind === 'panel'
  const preferGuide = payload.kind === 'guide' || payload.kind === 'docs'
  const panel = (preferPanel || !preferGuide)
    ? displayedPanelSurfaces.value.find((surface) => surface.id === payload.surfaceId)
    : undefined
  if (panel) {
    activePanelSurfaceId.value = panel.id
    activeSurface = panel
    activeSurfaceId = panel.id
    activeTab.value = 'panel'
  } else {
    const guide = (preferGuide || !preferPanel)
      ? guideSurfaces.value.find((surface) => surface.id === payload.surfaceId)
      : undefined
    if (!guide) return
    activeGuideSurfaceId.value = guide.id
    activeSurface = guide
    activeSurfaceId = guide.id
    activeTab.value = 'guide'
  }
  if (isActivationRevision(payload.activationRevision)) {
    surfaceActivationRevisions.value[surfaceActivationKey(activeSurface)] = payload.activationRevision
  }
  router.replace({
    query: {
      ...route.query,
      tab: activeTab.value,
      surface: activeSurfaceId,
    },
  })
}

function isLegacyOpenSurfaceMessage(data: unknown): data is {
  type: 'neko-study-open-surface'
  payload: { pluginId?: string; surfaceId: string; kind?: string; activationRevision?: unknown }
} {
  if (!data || typeof data !== 'object') return false
  const message = data as { type?: unknown; payload?: unknown }
  if (message.type !== 'neko-study-open-surface' || !message.payload || typeof message.payload !== 'object') return false
  const payload = message.payload as { pluginId?: unknown; surfaceId?: unknown; kind?: unknown; activationRevision?: unknown }
  return typeof payload.surfaceId === 'string'
    && (!payload.pluginId || typeof payload.pluginId === 'string')
    && (!payload.kind || typeof payload.kind === 'string')
}

function setPanelSurfaceFrameRef(surfaceId: string, instance: unknown) {
  if (!surfaceId) return
  const receiver = instance as SurfaceMessageReceiver | null
  if (receiver && typeof receiver.sendSurfaceMessage === 'function') {
    panelSurfaceFrameRefs.set(surfaceId, receiver)
  } else {
    panelSurfaceFrameRefs.delete(surfaceId)
  }
}

function relayHostedSurfaceMessageToStaticUi(data: unknown) {
  if (isLegacyOpenSurfaceMessage(data)) {
    openHostedSurfaceFromStaticUi(data.payload)
    return
  }
  if (data && typeof data === 'object' && (data as { type?: unknown }).type === 'neko-plugin-context-invalidated') {
    void Promise.allSettled(Array.from(panelSurfaceFrameRefs.values(), (frame) => frame.refreshContext()))
    return
  }
  // Hosted surface messages have already been source/origin checked by the
  // frame. Keep every mounted static panel current, including a `main` tab
  // that is temporarily off-screen while a hosted surface is active. Static
  // panels are the only legacy-UI iframe owners; do not mount a duplicate
  // hidden relay for the same /ui/ document.
  for (const surface of displayedPanelSurfaces.value) {
    if (surface.mode === 'static') {
      panelSurfaceFrameRefs.get(surface.id)?.sendSurfaceMessage(data)
    }
  }
}

async function fetchSurfaces(): Promise<boolean> {
  const loadId = ++currentSurfaceLoadId
  const currentPluginId = pluginId.value
  try {
    const info = await getPluginUiSurfaceInfo(currentPluginId, locale.value)
    if (loadId !== currentSurfaceLoadId || currentPluginId !== pluginId.value) return false
    surfaces.value = info.surfaces
    surfaceWarnings.value = info.warnings
  } catch (caught: any) {
    if (loadId !== currentSurfaceLoadId || currentPluginId !== pluginId.value) return false
    surfaces.value = []
    surfaceWarnings.value = [{
      path: 'plugin.ui',
      code: 'surface_query_failed',
      message: caught?.response?.data?.detail || caught?.message || String(caught),
    }]
  }
  activePanelSurfaceId.value = ''
  activeGuideSurfaceId.value = ''
  syncSurfaceTabs()
  return true
}

async function refreshPluginUi(): Promise<boolean> {
  return fetchSurfaces()
}

onMounted(async () => {
  try {
    await pluginStore.fetchPlugins()
    await pluginStore.fetchPluginStatus(pluginId.value)
    await fetchCandidates()
    if (await refreshPluginUi()) syncActiveTab(route.query.tab)
    pluginStore.setSelectedPlugin(pluginId.value)
  } finally {
    loading.value = false
  }
})

watch(
  () => [route.query.tab, route.query.surface],
  ([tab]) => {
    syncSurfaceTabs()
    syncActiveTab(tab)
  },
)

watch(pluginId, async () => {
  loading.value = true
  try {
    await pluginStore.fetchPluginStatus(pluginId.value)
    await fetchCandidates()
    if (await refreshPluginUi()) syncActiveTab(route.query.tab)
    pluginStore.setSelectedPlugin(pluginId.value)
  } finally {
    loading.value = false
  }
})

watch(locale, () => {
  if (!plugin.value) return
  void refreshPluginUi().then((refreshed) => {
    if (refreshed) syncActiveTab(route.query.tab)
  })
})
</script>

<style scoped>
.plugin-detail {
  padding: 0;
}

.loading-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 200px;
  gap: 12px;
  color: var(--el-text-color-secondary);
}

.loading-container .el-icon {
  color: var(--el-color-primary);
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.is-disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.header-left h2 {
  margin: 0;
  font-size: 20px;
}

.info-section {
  padding: 20px 0;
}

.candidate-section {
  margin-top: 20px;
  padding: 18px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 8px;
}

.candidate-section__header,
.candidate-row,
.candidate-row__title,
.candidate-row__status {
  display: flex;
  align-items: center;
}

.candidate-section__header,
.candidate-row {
  justify-content: space-between;
  gap: 16px;
}

.candidate-section__header h3 {
  margin: 0;
}

.candidate-section__header p {
  margin: 6px 0 0;
  color: var(--el-text-color-secondary);
}

.candidate-list {
  margin-top: 14px;
}

.candidate-row {
  padding: 14px 0;
  border-top: 1px solid var(--el-border-color-lighter);
}

.candidate-row__identity {
  min-width: 0;
}

.candidate-row__title,
.candidate-row__status {
  gap: 8px;
}

.candidate-row code {
  display: block;
  margin-top: 6px;
  color: var(--el-text-color-secondary);
  overflow-wrap: anywhere;
}

.candidate-row__error {
  display: block;
  margin-top: 6px;
  color: var(--el-color-danger);
}

@media (max-width: 720px) {
  .candidate-row {
    align-items: flex-start;
    flex-direction: column;
  }
}

.surface-section {
  padding: 16px 0;
}

.surface-warning {
  margin-bottom: 14px;
}

.surface-warning__list {
  margin: 6px 0 0;
  padding-left: 18px;
}

.surface-warning__list li {
  line-height: 1.7;
}

.surface-warning__list code {
  margin-right: 8px;
  color: var(--el-color-warning);
}

</style>
