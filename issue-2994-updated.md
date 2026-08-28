# 插件生命周期管理重构（基于全面调研）

**状态**: 调研完成，待社区讨论优先级  
**基线**: `upstream/main` (commit `7bca8614`)  
**调研周期**: 2 轮，共 11 个独立调研  
**结论**: 原 Issue 的 4 个假设全部不成立；真正的缺口收敛为 3 项

---

## 问题背景

当前插件系统的生命周期管理（安装、升级、降级、切换来源、卸载）分散在多个模块中：
- `PluginCliService` — CLI 包安装
- `InstallSourceManager` — 安装来源记账
- `PluginLifecycleService` — 启停 + 卸载
- `source_switch` — builtin→user 切换
- `upgrade_support` — 替换事务
- Market Bridge — Market 升级

尽管核心事务 `replace_plugin` 是共享的（只有 2 个调用方），但存在三个实际问题：

1. **卸载路径未收口** — 7 个包文件函数绕过共享事务
2. **回调装配重复** — 调用方需装配 12 个参数，5 个在两处完全相同
3. **候选模型缺失** — 无 managed/unmanaged 区分，manual 插件可被误删

---

## 调研结论

### 原 Issue 的 4 个假设全部不成立

| Issue 声称 | 调研结果 | 证据 |
|---|---|---|
| 需要 CAS 防并发 | ❌ 不存在多进程并发 | CLI install 已禁用；Server 单进程 + `plugin_operation_lock` 串行化 |
| 3 文件写入不一致 | ❌ 只有 2 文件，已有防护 | 每个文件原子写入（tmp + rename）；生命周期不同，不需要同步 |
| 600ms → 35ms | ❌ 实测 13ms，无法重现 | 22 插件：扫描 3.53ms + 解析 11.44ms；100 插件估算 59ms |
| Registry 应 Git 友好 | ❌ 混淆了概念 | 运行状态（本机路径/偏好）不应 Git 友好；环境复现需独立声明文件 |

详见：
- [多进程并发调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/concurrency-research-report.md)
- [一致性风险调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/consistency-risk-research-report.md)
- [性能 Benchmark 调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/performance-benchmark-report.md)
- [环境复现需求调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/environment-reproduction-research-report.md)

### upstream/main 的守护程度比想象中好

11 条不变量中 **4 条完全守护、5 条部分守护**：

| 不变量 | 判定 | 关键测试/机制 |
|---|---|---|
| 候选切换不丢启停偏好 | ✅ 完全 | 7 条测试（`:579`、`:3271` 等） |
| 单个坏候选不中断 inventory | ✅ 完全 | 5 条测试（`:412` 等） |
| 代码与持久数据分离 | ✅ 完全 | `_validate_replacement_targets` fail-closed |
| 磁盘是候选存在的事实来源 | ✅ 完全 | `reconciler.py` 每次重扫 |
| `plugin_id` 不通过改名解决冲突 | ⚠️ 部分 | 目录改名已封堵；运行时 ID 改名靠 `source_replacement` 护栏 |
| 一个 ID 多候选、只有一个生效 | ⚠️ 部分 | builtin + 1 user 有测试；多 user 不支持 |
| 五类操作走同一文件事务 | ⚠️ 部分 | `replace_plugin` 共享；**卸载走独立路径** |
| 修改统一串行 | ⚠️ 部分 | 已串行化；但调用方仍需装配 12 个回调 |
| 返回阶段/回滚状态 | ⚠️ 部分 | `ReplacePluginResult` 有结构；卸载路径无 |
| 四类来源同一候选模型 | ❌ 缺失 | 无统一 `PluginCandidate`；无 `manual/unmanaged` 概念 |
| 只有 managed 候选可升级/卸载 | ❌ 缺失 | 无 managed/unmanaged 区分 |

详见：
- [模块职责重叠量化调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/module-ownership-research-report.md)
- [不变量守护现状调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/invariant-guard-research-report.md)

---

## 真正的 3 个缺口

### 缺口 1：卸载路径未走共享事务

**现状**：7 个包文件函数住在 `lifecycle_service.py` 里，从不经过 `replace_plugin`：

| 行号 | 函数 | 职责 |
|---|---|---|
| 391 | `_delete_plugin_directory_sync` | 删代码目录 |
| 521 | `_record_deferred_profile_cleanup_sync` | 延迟清理记账 |
| 549 | `_retry_deferred_profile_cleanup_sync` | 启动期重试 |
| 615 | `_stage_orphaned_package_profile_sync` | profile 暂存 |
| 777 | `_restore_staged_package_profile_sync` | profile 恢复 |
| 784 | `_finalize_staged_package_profile_sync` | profile 最终删除 |
| 793 | `_mark_install_source_removed_sync` | 安装来源记账 |

**问题**：卸载的文件操作逻辑与安装/升级分离，无法复用回滚、阶段报告等机制。

---

### 缺口 2：调用方装配 12 个回调

**现状**：`replace_plugin` 要求调用方提供 7 个必需回调 + 5 个可选参数。两处装配对比：

| 参数 | `plugin_cli/service.py:459` | `market_bridge.py:3588` | 判定 |
|---|---|---|---|
| `is_running` | `upgrade_support.plugin_is_running` | `plugin_is_running` | **完全相同** |
| `stop` | `stop_plugin_for_replace` | `stop_plugin_for_upgrade` | **完全相同** |
| `start` | 闭包 → `start_plugin_after_replace(strict=True)` | 闭包 → `start_plugin_after_upgrade(strict=True)` | **逻辑相同** |
| `cleanup_backup` | `remove_directory` | `_async_remove_dir` | **几乎相同** |
| `additional_targets` | `(profile_dir,)` | `(profile_dir,)` | **完全相同** |
| `validate_new` | 闭包：校验 plugin_id + directory_name | 闭包：校验 plugin_id + registry runtime source | **同一检查实现两次** |

**12 个参数里 5 个在两处完全相同，各写一遍。**

Market 侧还多一层类型丢失：

```python
# market_bridge.py:3299
replace_kwargs: dict[str, Any],
# market_bridge.py:3317
return await replace_plugin(**replace_kwargs)
```

**问题**：
- 静态检查失效
- `replace_plugin` 改签名时 Market 路径不报错
- 调用方仍需知道"正确调用顺序"

---

### 缺口 3：候选模型缺 managed/unmanaged

**现状**：`channel` 字段不等于 managed/unmanaged

| Channel | 删除行为 | 应有行为 | 差距 |
|---|---|---|---|
| `builtin` | 路径白名单排除 | ✅ 不可删 | 已守护 |
| `manual` | 可删 | ⚠️ 拒绝或警告 | **缺口 3** |
| `imported` | 可删 | ✅ 可删 | 正确 |
| `market` | 可删 | ✅ 可删 | 正确 |

**问题**：

1. **Manual 插件可被误删**  
   用户手动放置插件目录 → `reconcile()` 自动打 `"manual"` 标签 → 用户在 UI 点删除 → N.E.K.O 直接 `rmtree`，无警告。

2. **无法区分来源**  
   `delete_plugin` 不检查 `channel`，只检查路径白名单。无法保证"只有 N.E.K.O 安装的目录可以被 N.E.K.O 删除"。

详见：
- [Manual 插件接管行为调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/manual-plugin-takeover-research-report.md)

---

## 两个具体场景的结论

### 场景 1：Manual 插件接管（@wislap 第 1 节的三个问题）

**Q1: 用户手动解压一个目录到 `plugins/` 后，能否通过 CLI 或 Market 接管它？**

✅ **可以**。启动时 `reconcile()` 打 `"manual"` 标签 → CLI/Market 安装同 ID 包 → `plan_install` 判定为 `"upgrade"`（不检查 channel）→ `replace_plugin` 替换目录 → `record_import`/`record_market` 覆写 lock entry，`channel` 变为 `"imported"`/`"market"` — **接管完成**。

**Q2: N.E.K.O 怎么知道这个目录"可以安全删除"？**

❌ **不知道**。`delete_plugin` 只检查路径白名单，不检查 `channel`。

**Q3: 如果不能接管，应该如何拒绝？**

⚠️ **应该但没有**。建议引入 `managed` 字段，在删除入口守护：
```python
if not entry.managed:
    raise UNMANAGED_PLUGIN_DELETE_FORBIDDEN
```

---

### 场景 2：偏好清理语义（@wislap 第 7 节）

**Q: "`:3145 test_delete_plugin_clears_runtime_override` 清理偏好是否合理？"**

✅ **合理**。当前行为：

```python
if not restored_builtin:
    clear_runtime_override(plugin_id)
```

| 删除场景 | 偏好清理？ | 理由 |
|---|---|---|
| 删除最后一个候选（彻底卸载） | ✅ 清理 | `plugin_id` 消失，偏好无绑定对象 |
| 删除 User/Market，Builtin 恢复 | ❌ 保留 | `plugin_id` 仍存在，偏好应继续生效 |

**设计一致性**：
- 偏好绑定 `plugin_id`（逻辑概念），不绑定候选（物理目录）
- 用户说"我禁用了 `demo`"，是指"不要这个插件"，而不是"不要这个候选"
- 候选切换时偏好应继续生效；彻底卸载时偏好应清理

详见：[偏好清理语义调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/preference-cleanup-semantics-report.md)

---

## 重要发现：不需要改存储格式

**这 3 个缺口都是代码组织和接口设计问题，不是数据模型问题。**

不需要：
- 单文件 Registry
- CAS 并发控制
- JSONL 审计日志
- 格式迁移

---

## 实施优先级建议

### 建议顺序：缺口 1 → 缺口 2 → 缺口 3

**理由**：

**缺口 1（卸载收口）优先**：
- 边界清晰：7 个函数已列出，迁移目标明确
- 立即减少 `lifecycle_service.py` 体积（1861 行）
- 为其他缺口扫清障碍（统一事务入口后更容易改接口）

**缺口 2（回调装配）其次**：
- 依赖缺口 1 完成（卸载收口后才有"完整的事务入口"）
- 可以增量改进：先把 5 个恒定回调收进模块内部，再用类型化 request 替代 `dict[str, Any]`

**缺口 3（候选模型）最后**：
- 需要定义领域模型（`PluginCandidate` with `managed` field）
- 影响面最广（scanner、reconciler、registry、lifecycle 都要改）
- 可以复用探索性实现的 `models.py` 作为起点

---

## 完整调研报告索引

### 第一轮（4 个假设验证）
1. [多进程并发调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/concurrency-research-report.md)
2. [一致性风险调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/consistency-risk-research-report.md)
3. [性能 Benchmark 调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/performance-benchmark-report.md)
4. [环境复现需求调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/environment-reproduction-research-report.md)

### 第二轮（真实缺口定位）
5. [模块职责重叠量化调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/module-ownership-research-report.md)
6. [不变量守护现状调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/invariant-guard-research-report.md)
7. [Manual 插件接管行为调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/manual-plugin-takeover-research-report.md)
8. [偏好清理语义调研](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/docs/preference-cleanup-semantics-report.md)

### 总结
9. [研究总结报告](https://github.com/CN-Zephyr/N.E.K.O/blob/refactor/plugin-identity-selection/研究总结报告.md)

---

## 下一步

1. **社区讨论优先级** — 征求对 3 个缺口优先级的意见
2. **确认 managed 字段设计** — bool vs 枚举
3. **确认 manual 插件策略** — 删除时拒绝 vs 警告

欢迎在 Discussion 区讨论：[待创建]

---

**调研耗时**: 约 3 个工作日  
**调研方法**: 静态代码分析 + 测试覆盖验证 + GitHub Issue 搜索  
**基线**: `upstream/main` (commit `7bca8614`)
