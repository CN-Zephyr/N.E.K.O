# 模块职责重叠量化调研报告

**日期**: 2026-08-29
**调研目标**: 量化 wislap 指出的"职责混乱"问题
**基线**: `upstream/main`（所有引用均来自 `git show upstream/main:<path>`）

---

## 零、先纠正前四份报告的方法学缺陷

### 0.1 问题

前四份调研报告（并发/一致性/性能/环境复现）是在工作分支
`refactor/plugin-identity-selection` 上读取代码的，**该分支包含未合并的 2w 行重构**。

核对四个关键文件的 blob hash：

| 文件 | 工作分支 vs upstream/main |
|---|---|
| `neko_plugin_cli/commands/install_cmd.py` | **SAME** |
| `plugins/operation_lock.py` | **DIFFER** |
| `install_source/manager.py` | **DIFFER** |
| `infrastructure/runtime_overrides.py` | **DIFFER** |

三个文件不同 —— 意味着我可能是在描述重构后的代码，而不是 upstream/main。

### 0.2 逐条复核结果

对 upstream/main 重新核对每一条结论：

| 结论 | upstream/main 证据 | 是否成立 |
|---|---|---|
| CLI install 已禁用 | `install_cmd.py` 与工作分支逐字节相同 | ✅ 成立 |
| 已有跨进程文件锁 | `operation_lock.py:119 _PROCESS_LOCK = _CrossLoopLock()`；`:199 _acquire_file_lock_sync`；`:161 os.register_at_fork` | ✅ 成立 |
| 已有原子写入 | `manager.py:327 def _atomic_write`；`:359 os.replace(tmp_path, lock_path)` | ✅ 成立 |
| 只有 2 个状态文件 | `manager.py:106` → `plugins.lock.json`；`runtime_overrides.py:28` → `plugin_runtime_overrides.json`；`infrastructure/` 下**无** `plugin_selections.py` | ✅ 成立 |

**结论：四条结论在 upstream/main 上全部成立，机制本身在重构前就存在。**
方法学有瑕疵，结论无需修改。后续报告全部改为直接从 `upstream/main` 读取。

---

## 一、共享事务的调用方分布

### 1.1 `replace_plugin` —— 唯一的文件替换事务

**定义**: `upstream/main:plugin/server/application/plugins/upgrade_support.py:307`

**调用方**: 仅 2 处

| # | 位置 | 场景 |
|---|---|---|
| 1 | `plugin_cli/service.py:459` | 本地包安装/升级/降级/重装 |
| 2 | `market_bridge.py:3317`（经 `_replace_market_plugin_transaction` 包装） | Market 升级/重装 |

**观察**: 事务实现**确实是共享的**。这一点比 Issue 原描述的"3 套编排逻辑"要好。

### 1.2 `switch_builtin_source` —— builtin→user 来源切换事务

**定义**: `upstream/main:plugin/server/application/plugins/source_switch.py:250`

**调用方**: 仅 1 处 —— `plugin_cli/service.py:651`

---

## 二、真正的问题：回调装配重复

`replace_plugin` 的签名要求调用方提供 **7 个必需回调 + 5 个可选参数**：

```python
async def replace_plugin(
    *,
    layout: PluginLayout,
    install_new: Callable[[], Awaitable[dict[str, object]]],
    validate_new: Callable[[], Awaitable[None]],
    is_running: Callable[[str], Awaitable[bool]],
    stop: Callable[[str], Awaitable[None]],
    start: Callable[[str], Awaitable[None]],
    cleanup_backup: Callable[[Path], Awaitable[None]],
    additional_targets: tuple[Path, ...] = (),
    preserve_targets: tuple[Path, ...] = (),
    initialize_runtime_config: bool = True,
    validate_backup: Callable[[Path], Awaitable[None]] | None = None,
    on_rollback_start: Callable[[], None] | None = None,
) -> ReplacePluginResult:
```

### 2.1 两处装配逐参数对比

| 参数 | `plugin_cli/service.py:459` | `market_bridge.py:3588` | 判定 |
|---|---|---|---|
| `layout` | `resolve_plugin_layout(plan.plugin_id, target_dir)` | `resolve_plugin_layout(installed_plugin_id, plugin_dir)` | 同构 |
| `install_new` | 闭包 → `_install_sync` | 闭包 → `_cli_service.upload_and_install` | 不同（合理） |
| `validate_new` | 闭包：校验 plugin_id 与 directory_name | 闭包：校验 plugin_id **+** registry runtime source | **重复实现同一身份检查** |
| `is_running` | `upgrade_support.plugin_is_running` | `plugin_is_running` | **完全相同** |
| `stop` | `upgrade_support.stop_plugin_for_replace` | `stop_plugin_for_upgrade`（同函数的历史别名） | **完全相同** |
| `start` | 闭包 → `start_plugin_after_replace(strict=True)` | 闭包 → `start_plugin_after_upgrade(strict=True)` | **逻辑相同，两份闭包** |
| `cleanup_backup` | `upgrade_support.remove_directory` | `_async_remove_dir`（加了一层日志） | **几乎相同** |
| `additional_targets` | `(profile_dir,)` | `(profile_dir,)` | **完全相同** |
| `preserve_targets` | 条件式（manifestless 分支） | `(profile_dir,)` | 不同（合理） |
| `initialize_runtime_config` | `not plan.manifestless_state` | 默认 `True` | 不同（合理） |
| `validate_backup` | 条件式（manifestless 分支） | 未传 | 不同（合理） |
| `on_rollback_start` | 未传 | `mark_rollback_running` | 不同（合理） |

### 2.2 量化结论

- **12 个参数中，5 个在两处完全相同或几乎相同**
  （`is_running` / `stop` / `start` / `cleanup_backup` / `additional_targets`）
- 这 5 个参数每处都要重新写一遍
- `validate_new` 里的插件身份检查在两处**各实现了一次**

**这就是 wislap 所说"调用方仍需知道正确调用顺序"的具体证据。**

### 2.3 Market 侧额外的一层类型丢失

`market_bridge.py:3588` 不是直接调用，而是把 12 个参数塞进
`dict[str, Any]`，经 `_replace_market_plugin_transaction(replace_kwargs=...)`
中转，最后 `:3317` 用 `**replace_kwargs` 展开：

```python
# market_bridge.py:3299
replace_kwargs: dict[str, Any],
...
# market_bridge.py:3317
return await replace_plugin(**replace_kwargs)
```

**后果**:
- 类型检查完全失效（`dict[str, Any]` + `**` 展开）
- 参数拼写错误只能在运行时暴露
- 新增/重命名 `replace_plugin` 参数时，这条路径不会被静态检查发现

---

## 三、Lifecycle 持有的包文件职责

`upstream/main:plugin/server/application/plugins/lifecycle_service.py`（1861 行）
内含 **7 个纯文件/包管理函数**：

| 行号 | 函数 | 职责归属 |
|---|---|---|
| 391 | `_delete_plugin_directory_sync` | 包管理（删代码目录） |
| 521 | `_record_deferred_profile_cleanup_sync` | 包管理（延迟清理记账） |
| 549 | `_retry_deferred_profile_cleanup_sync` | 包管理（启动期重试） |
| 615 | `_stage_orphaned_package_profile_sync` | 包管理（profile 暂存） |
| 777 | `_restore_staged_package_profile_sync` | 包管理（profile 恢复） |
| 784 | `_finalize_staged_package_profile_sync` | 包管理（profile 最终删除） |
| 793 | `_mark_install_source_removed_sync` | 安装来源记账 |

**问题**: 这 7 个函数与"启停插件进程"没有关系，却住在 lifecycle 模块里。

**wislap 的对应要求**（她的第 5 条）：
> 把 `lifecycle_service` 中的文件卸载和 profile 清理迁出。

**证实**: 有具体的 7 个函数需要迁出，不是笼统的"职责不清"。

---

## 四、wislap 的 11 条不变量 —— 当前守护情况

对照 upstream/main 逐条核对：

| # | 不变量 | 当前是否守护 | 守护者 / 缺口 |
|---|---|---|---|
| 1 | `plugin_id` 不通过改名解决冲突 | ⚠️ 部分 | `install_plan.py` 会阻断同 ID 跨目录，但历史上有 `_1` 后缀逻辑残留需确认 |
| 2 | 一个 ID 多候选、只有一个生效 | ⚠️ 部分 | 仅支持 builtin + 1 个规范 user 候选；多 user 候选不支持 |
| 3 | builtin/Market/本地包/manual 同一候选模型 | ❌ 缺 | 无统一 `PluginCandidate` 抽象，`manual/unmanaged` 概念不存在 |
| 4 | 只有 managed 候选可升级/卸载 | ❌ 缺 | 无 managed/unmanaged 区分，用户手动解压的目录与包安装无法区分 |
| 5 | 代码与持久数据分离 | ✅ 有 | #2943 已实现（exec root vs state root） |
| 6 | 安装/升级/降级/切换/卸载走同一文件事务 | ⚠️ 部分 | `replace_plugin` 共享，但卸载走 lifecycle 自己的路径（见第三节） |
| 7 | 候选切换不丢用户启停偏好 | ❓ 未验证 | 需要专项测试 |
| 8 | 磁盘是候选存在的事实来源 | ✅ 有 | `reconciler.py` + `scanner.py` 每次重新扫描 |
| 9 | 单个坏候选不中断整个 inventory | ❓ 未验证 | 需要专项测试 |
| 10 | 文件修改统一串行，调用方不接触 revision/锁 | ⚠️ 部分 | `plugin_operation_lock` 已串行化，但调用方仍需自己装配 12 个回调 |
| 11 | 每次操作返回阶段/回滚状态/最终候选 | ⚠️ 部分 | `ReplacePluginResult` 有 `stage`/`rollback_status`，但卸载路径无对应结构 |

**统计**: ✅ 完全守护 2 条 / ⚠️ 部分 5 条 / ❌ 缺失 2 条 / ❓ 待验证 2 条

---

## 五、结论

### 5.1 Issue 原描述错在哪

Issue 说"CLI/Server/Market 各有一套升级编排逻辑"——**这句话不准确**。
事务实现（`replace_plugin`）**是共享的**，只有 2 个调用方。

### 5.2 真正的问题是什么

**不是"多套事务"，而是"事务参数装配重复 + 类型安全丢失 + 卸载路径未收口"**：

1. **回调装配重复**：12 个参数里 5 个在两处完全相同，各写一遍
2. **身份检查重复**：`validate_new` 的 plugin_id 校验两处各实现一次
3. **类型安全丢失**：Market 路径用 `dict[str, Any]` + `**` 展开
4. **卸载未收口**：7 个包文件函数住在 lifecycle_service 里，不走 `replace_plugin`
5. **候选模型缺失**：无 managed/unmanaged 区分，无统一 `PluginCandidate`

### 5.3 对目标架构的含义

wislap 建议的 `PluginManagement` 统一入口是对的，但**收口的重点不是"合并 3 套事务"**
（本来只有 1 套），而是：

- 把那 5 个恒定回调收进模块内部（调用方不再传 `is_running`/`stop`/`start`/`cleanup_backup`）
- 用类型化的 request 对象替代 `dict[str, Any]`
- 把卸载路径接入同一事务
- 补上 managed/unmanaged 候选模型

---

## 六、待补调研

| # | 主题 | 状态 |
|---|---|---|
| 6 | 不变量 7（候选切换保留偏好）专项验证 | ⏳ |
| 7 | 不变量 9（坏候选隔离）专项验证 | ⏳ |
| 8 | `_1` 后缀改名逻辑是否仍存在 | ⏳ |
| 9 | 降级的数据承诺边界（回滚到底恢复什么） | ⏳ |

---

**报告结束**

已完成调研：
1. ✅ 多进程并发（结论已对 upstream/main 复核）
2. ✅ 一致性风险（结论已对 upstream/main 复核）
3. ✅ 性能 Benchmark
4. ✅ 环境复现需求
5. ✅ 模块职责重叠量化（本报告）
