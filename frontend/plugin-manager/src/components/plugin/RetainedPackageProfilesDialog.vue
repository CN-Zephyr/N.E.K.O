<template>
  <el-dialog
    :model-value="visible"
    width="min(680px, calc(100vw - 32px))"
    align-center
    append-to-body
    class="retained-profile-dialog"
    :close-on-click-modal="!deleting"
    :close-on-press-escape="!deleting"
    @close="close"
  >
    <template #header>
      <div class="retained-profile-dialog__header">
        <div>
          <h3>{{ t('plugins.retainedData.title') }}</h3>
          <p>{{ t('plugins.retainedData.description') }}</p>
        </div>
        <el-button :loading="loading" circle plain :aria-label="t('common.refresh')" @click="load">
          <el-icon><Refresh /></el-icon>
        </el-button>
      </div>
    </template>

    <div v-loading="loading" class="retained-profile-dialog__body">
      <el-alert
        :title="t('plugins.retainedData.scopeNotice')"
        type="info"
        :closable="false"
        show-icon
      />

      <el-empty
        v-if="!loading && profiles.length === 0"
        :description="t('plugins.retainedData.empty')"
      />

      <div v-else class="retained-profile-list">
        <article
          v-for="profile in profiles"
          :key="profileKey(profile)"
          class="retained-profile-item"
        >
          <div class="retained-profile-item__copy">
            <div class="retained-profile-item__title-row">
              <strong>{{ profile.plugin_id }}</strong>
              <el-tag size="small" effect="plain">
                {{ t(`plugins.installSource.channel.${profile.source}`) }}
              </el-tag>
            </div>
            <dl>
              <div>
                <dt>{{ t('plugins.retainedData.packageId') }}</dt>
                <dd>{{ profile.package_id }}</dd>
              </div>
              <div>
                <dt>{{ t('plugins.retainedData.candidate') }}</dt>
                <dd>{{ profile.candidate.directory_name }}</dd>
              </div>
            </dl>
            <p v-if="!profile.deletable" class="retained-profile-item__blocked">
              {{ t('plugins.retainedData.codePresent') }}
            </p>
          </div>

          <el-button
            type="danger"
            plain
            :disabled="!profile.deletable || deleting"
            @click="requestDelete(profile)"
          >
            <el-icon><Delete /></el-icon>
            {{ t('plugins.retainedData.deleteAction') }}
          </el-button>
        </article>
      </div>
    </div>

    <template #footer>
      <el-button :disabled="deleting" @click="close">
        {{ t('common.close') }}
      </el-button>
    </template>
  </el-dialog>

  <PluginDangerConfirmDialog
    :visible="confirmVisible"
    :loading="deleting"
    :title="t('plugins.retainedData.confirmTitle')"
    :message="t('plugins.retainedData.confirmMessage', { pluginId: pendingProfile?.plugin_id || '' })"
    :hint="t('plugins.retainedData.confirmHint')"
    :action-label="t('plugins.retainedData.deleteAction')"
    :warning-title="t('plugins.retainedData.warningTitle')"
    :cancel-label="t('common.cancel')"
    :loading-label="t('plugins.retainedData.deleting')"
    :hold-idle-label="t('plugins.retainedData.holdIdle')"
    :hold-active-label="t('plugins.retainedData.holdActive')"
    @close="closeConfirmation"
    @confirm="confirmDelete"
  />
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { Delete, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import {
  deleteRetainedPackageProfile,
  getRetainedPackageProfiles,
  type RetainedPackageProfile,
} from '@/api/plugins'
import { formatHttpError } from '@/utils/request'
import PluginDangerConfirmDialog from './PluginDangerConfirmDialog.vue'

const props = defineProps<{
  visible: boolean
}>()

const emit = defineEmits<{
  close: []
}>()

const { t } = useI18n()
const profiles = ref<RetainedPackageProfile[]>([])
const loading = ref(false)
const deleting = ref(false)
const confirmVisible = ref(false)
const pendingProfile = ref<RetainedPackageProfile | null>(null)

function profileKey(profile: RetainedPackageProfile): string {
  return `${profile.plugin_id}:${profile.candidate.root_id}:${profile.candidate.directory_name}`
}

async function load() {
  profiles.value = []
  loading.value = true
  try {
    const result = await getRetainedPackageProfiles()
    profiles.value = result.profiles
  } catch (error) {
    const detail = formatHttpError(error)
    ElMessage.error(detail || t('plugins.retainedData.loadFailed'))
  } finally {
    loading.value = false
  }
}

function requestDelete(profile: RetainedPackageProfile) {
  pendingProfile.value = profile
  confirmVisible.value = true
}

function closeConfirmation() {
  if (deleting.value) return
  confirmVisible.value = false
  pendingProfile.value = null
}

async function confirmDelete() {
  const profile = pendingProfile.value
  if (!profile || deleting.value) return

  deleting.value = true
  try {
    await deleteRetainedPackageProfile(profile)
    confirmVisible.value = false
    pendingProfile.value = null
    ElMessage.success(t('plugins.retainedData.deleteSuccess'))
    await load()
  } catch (error) {
    const detail = formatHttpError(error)
    ElMessage.error(detail || t('plugins.retainedData.deleteFailed'))
  } finally {
    deleting.value = false
  }
}

function close() {
  if (deleting.value) return
  confirmVisible.value = false
  pendingProfile.value = null
  emit('close')
}

watch(
  () => props.visible,
  (visible) => {
    if (visible) {
      void load()
    }
  },
)
</script>

<style scoped>
.retained-profile-dialog__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding-right: 28px;
}

.retained-profile-dialog__header h3 {
  margin: 0;
  color: var(--el-text-color-primary);
  font-size: 18px;
}

.retained-profile-dialog__header p {
  margin: 6px 0 0;
  color: var(--el-text-color-secondary);
  line-height: 1.55;
}

.retained-profile-dialog__body {
  min-height: 180px;
}

.retained-profile-list {
  display: grid;
  gap: 12px;
  margin-top: 16px;
}

.retained-profile-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 14px;
  background: var(--el-fill-color-extra-light);
}

.retained-profile-item__copy {
  min-width: 0;
}

.retained-profile-item__title-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.retained-profile-item__title-row strong {
  overflow-wrap: anywhere;
  color: var(--el-text-color-primary);
}

.retained-profile-item dl {
  display: grid;
  gap: 5px;
  margin: 10px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.retained-profile-item dl div {
  display: flex;
  gap: 8px;
}

.retained-profile-item dt {
  flex: 0 0 auto;
  font-weight: 600;
}

.retained-profile-item dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
}

.retained-profile-item__blocked {
  margin: 9px 0 0;
  color: var(--el-color-warning);
  font-size: 12px;
}

@media (max-width: 560px) {
  .retained-profile-item {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
