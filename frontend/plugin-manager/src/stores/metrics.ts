/**
 * 性能指标状态管理
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getAllMetrics, getPluginMetrics, getPluginMetricsHistory } from '@/api/metrics'
import type { PluginMetrics } from '@/types/api'

export const useMetricsStore = defineStore('metrics', () => {
  // 状态
  const allMetrics = ref<PluginMetrics[]>([])
  const currentMetrics = ref<Record<string, PluginMetrics>>({})
  const metricsHistory = ref<Record<string, PluginMetrics[]>>({})
  const loading = ref(false)
  const error = ref<string | null>(null)
  
  // 防止请求堆积：正在进行的请求
  let pendingFetchAll: Promise<any> | null = null
  // 每次发起全量请求加一。超时后旧请求可能还没结束，只有最新的请求能写指标、
  // 清理 pendingFetchAll，晚到的旧响应直接丢掉。
  let fetchAllGeneration = 0
  // 全量请求和单插件请求共用的发起序号。单插件请求在全量请求之后发起，
  // 它看到的服务端状态更新，全量结果整体替换时要保留它写入（或删掉）的那一项。
  let requestSeq = 0
  const pluginResultSeq: Record<string, number> = {}
  // 最近一次写入的全量结果的序号。比它早发起的单插件请求看到的状态更旧，不能再覆盖。
  let fullAppliedSeq = 0

  // 单插件结果只有比该插件已写入的结果（单插件或全量）都新时才能写入或删除。
  function acceptPluginResult(pluginId: string, seq: number): boolean {
    if (seq <= fullAppliedSeq || seq <= (pluginResultSeq[pluginId] ?? 0)) {
      return false
    }
    pluginResultSeq[pluginId] = seq
    return true
  }
  // 请求超时自动清理（防止请求堆积）
  const REQUEST_TIMEOUT = 15000 // 15秒

  function isGoodbyeResourceSuspendingOrSuspended() {
    if (typeof window === 'undefined') return false
    try {
      const helper = (window as any).isNekoGoodbyeResourceSuspendingOrSuspended
      if (typeof helper === 'function' && helper()) return true
      if ((window as any).goodbyeResourceSuspended === true) return true
      if ((window as any).__nekoGoodbyeResourceSuspendPending === true) return true
      return window.localStorage.getItem('neko-goodbye-resource-suspended') === 'true'
    } catch {
      return false
    }
  }

  // 操作
  async function fetchAllMetrics() {
    if (isGoodbyeResourceSuspendingOrSuspended()) {
      return { metrics: allMetrics.value }
    }
    // 如果已有请求正在进行，直接返回该请求的结果（防止请求堆积）
    if (pendingFetchAll) {
      return pendingFetchAll
    }
    
    loading.value = true
    error.value = null
    const generation = ++fetchAllGeneration
    const fullSeq = ++requestSeq
    
    // 设置超时自动清理，防止请求堆积
    const timeoutId = setTimeout(() => {
      if (pendingFetchAll && generation === fetchAllGeneration) {
        console.warn('[Metrics Store] fetchAllMetrics timeout, clearing pending request')
        pendingFetchAll = null
        loading.value = false
      }
    }, REQUEST_TIMEOUT)
    
    // 创建请求并保存引用（防止请求堆积）
    pendingFetchAll = (async () => {
      try {
        const response = await getAllMetrics()
        if (generation !== fetchAllGeneration) {
          return undefined
        }
        const metricsList: PluginMetrics[] = Array.isArray((response as any)?.metrics)
          ? ((response as any).metrics as PluginMetrics[])
          : []
        allMetrics.value = metricsList

        // 用这一次的结果替换当前指标。只增不删的话，服务端已经剔除的插件
        // 会一直留着上一次的数字。
        const next: Record<string, PluginMetrics> = {}
        metricsList.forEach((metric: PluginMetrics) => {
          next[metric.plugin_id] = metric
        })
        for (const [id, seq] of Object.entries(pluginResultSeq)) {
          if (seq <= fullSeq) continue
          const local = currentMetrics.value[id]
          if (local) {
            next[id] = local
          } else {
            delete next[id]
          }
        }
        currentMetrics.value = next
        fullAppliedSeq = fullSeq
        
        // 返回响应以便提取全局指标
        return response
      } catch (err: any) {
        if (generation !== fetchAllGeneration) {
          return undefined
        }
        error.value = err?.message || 'FETCH_METRICS_FAILED'
        console.error('Failed to fetch metrics:', err)
        throw err
      } finally {
        clearTimeout(timeoutId)
        if (generation === fetchAllGeneration) {
          loading.value = false
          pendingFetchAll = null  // 请求完成后清除引用
        }
      }
    })()
    
    return pendingFetchAll
  }

  async function fetchPluginMetrics(pluginId: string) {
    if (!pluginId) {
      console.warn('[Metrics] fetchPluginMetrics called with empty pluginId')
      return
    }
    if (isGoodbyeResourceSuspendingOrSuspended()) {
      return
    }
    
    console.log(`[Metrics] Fetching metrics for plugin: ${pluginId}`)
    const seq = ++requestSeq
    
    try {
      const response = await getPluginMetrics(pluginId)
      console.log(`[Metrics] Received response for ${pluginId}:`, response)
      
      // 检查响应格式
      if (!response || typeof response !== 'object') {
        console.warn(`[Metrics] Invalid response format for ${pluginId}:`, response)
        return
      }
      
      if (response.metrics && typeof response.metrics === 'object') {
        // 确保 metrics 包含必需的字段
        if (response.metrics.plugin_id && response.metrics.timestamp) {
          if (!acceptPluginResult(pluginId, seq)) return
          currentMetrics.value[pluginId] = response.metrics
          console.log(`[Metrics] Successfully stored metrics for ${pluginId}`)
        } else {
          console.warn(`[Metrics] Incomplete metrics data for ${pluginId}:`, response.metrics)
        }
      } else {
        // 插件正在运行但没有指标数据（可能正在收集）
        // 清除之前的指标数据，让组件显示"暂无数据"
        if (!acceptPluginResult(pluginId, seq)) return
        if (currentMetrics.value[pluginId]) {
          delete currentMetrics.value[pluginId]
        }
        // 记录消息（如果有）
        if (response.message) {
          console.log(`[Metrics] ${pluginId}: ${response.message}`)
        } else {
          console.log(`[Metrics] ${pluginId}: No metrics available (metrics is null)`)
        }
      }
    } catch (err: any) {
      // 404 表示插件不存在，这是正常的
      if (err.response?.status === 404) {
        console.log(`[Metrics] Plugin ${pluginId} not found (404)`)
        // 清除该插件的指标数据（如果存在）
        if (!acceptPluginResult(pluginId, seq)) return
        if (currentMetrics.value[pluginId]) {
          delete currentMetrics.value[pluginId]
        }
        return
      }
      // 其他错误才记录
      console.error(`[Metrics] Failed to fetch metrics for plugin ${pluginId}:`, err)
      // 即使失败也不抛出异常，让组件显示"暂无数据"
    }
  }

  async function fetchMetricsHistory(
    pluginId: string,
    params?: { limit?: number; start_time?: string; end_time?: string }
  ) {
    if (isGoodbyeResourceSuspendingOrSuspended()) {
      return
    }
    try {
      const response = await getPluginMetricsHistory(pluginId, params)
      metricsHistory.value[pluginId] = response.history || []
    } catch (err: any) {
      console.error(`Failed to fetch metrics history for plugin ${pluginId}:`, err)
    }
  }

  function getCurrentMetrics(pluginId: string): PluginMetrics | null {
    return currentMetrics.value[pluginId] || null
  }

  function getHistory(pluginId: string): PluginMetrics[] {
    return metricsHistory.value[pluginId] || []
  }

  return {
    // 状态
    allMetrics,
    currentMetrics,
    metricsHistory,
    loading,
    error,
    // 操作
    fetchAllMetrics,
    fetchPluginMetrics,
    fetchMetricsHistory,
    getCurrentMetrics,
    getHistory
  }
})
