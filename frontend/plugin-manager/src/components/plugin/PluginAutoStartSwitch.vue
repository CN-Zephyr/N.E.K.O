<template>
  <div class="plugin-auto-start-switch">
    <el-switch
      :model-value="autoStart"
      :loading="loading"
      :disabled="loading"
      :aria-label="t('plugins.autoStart')"
      data-testid="plugin-auto-start-switch"
      @change="handleChange"
    />
    <span class="plugin-auto-start-switch__hint">{{ t('plugins.autoStartHint') }}</span>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { usePluginStore } from '@/stores/plugin'
import { formatHttpError } from '@/utils/request'

interface Props {
  pluginId: string
}

const props = defineProps<Props>()
const pluginStore = usePluginStore()
const { t } = useI18n()

const loading = ref(false)

const autoStart = computed(() => {
  const plugin = pluginStore.getPluginById(props.pluginId)
  return plugin?.autoStart ?? false
})

// Same reporting rule as PluginActions: the request interceptor already
// surfaces network failures and most HTTP errors, so only fill the gaps.
interface ActionError {
  request?: unknown
  response?: { status?: number }
}

function showActionError(error: unknown, fallbackMessage: string) {
  const actionError = error as ActionError | undefined
  const status = actionError?.response?.status
  if (actionError?.request && !actionError?.response) {
    return
  }
  if (typeof status === 'number' && ![401, 403, 404].includes(status)) {
    return
  }
  ElMessage.error(formatHttpError(error) || fallbackMessage)
}

async function handleChange(value: string | number | boolean) {
  const next = Boolean(value)
  try {
    loading.value = true
    await pluginStore.setAutoStart(props.pluginId, next)
    ElMessage.success(next ? t('messages.autoStartEnabled') : t('messages.autoStartDisabled'))
  } catch (error: unknown) {
    showActionError(error, t('messages.autoStartUpdateFailed'))
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.plugin-auto-start-switch {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.plugin-auto-start-switch__hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
