# 不变量守护现状调研报告（调研 6–9）

**日期**: 2026-08-29
**基线**: `upstream/main`（所有引用来自 `git show upstream/main:<path>`）
**调研目标**: 逐条核对 wislap 提出的 11 条不变量在 upstream/main 的实际守护情况

---

## 一、先更正上一份报告的两处错误标注

上一份《模块职责重叠量化调研报告》把不变量 7 和 9 标为「❓ 待验证」。
实际核对后发现 **upstream/main 已有测试覆盖**，标注错误。

---

## 二、不变量 1：`plugin_id` 不通过改名解决冲突

### 2.1 存在两套独立的改名机制

**机制 A：目录改名**（`demo_1/`）

- 实现：`neko_plugin_cli/core/install.py:214 resolve_unique_dir()`
- 触发条件：`on_conflict == "rename"`（`install.py:210`）

**机制 B：运行时 ID 改名**（内存中 `demo` → `demo_1`）

- 实现：`plugin/core/registry.py:434 new_id = f"{desired}_{counter}"`
- 触发条件：`enable_rename=True` 且 config_path 不同

### 2.2 机制 A（目录改名）已被两个 HTTP 入口彻底封堵

| 入口 | 约束 | 结果 |
|---|---|---|
| `routes/plugin_cli.py:61` | `pattern="^fail$"` | 直接拒绝 `rename` |
| `routes/market_bridge.py:367` | `pattern=r"^(fail\|rename)$"` | 接受但**归一化** |

Market 的归一化逻辑（`market_bridge.py:387-390`）：

```python
@field_validator("on_conflict")
@classmethod
def _normalize_on_conflict(cls, value: str) -> str:
    del cls
    return "fail" if value == "rename" else value
```

配套注释（`market_bridge.py:363-366`）说得很明确：

> Keep Market installs aligned with imported packages: an existing plugin
> directory is a conflict, never a request to create `plugin_1`. Accept the
> legacy value so cached Market clients remain compatible, then normalise it
> to the non-renaming behaviour.

**结论：目录改名在生产路径上不可达。** 接受 `rename` 只为兼容旧 Market 客户端缓存，随即被改写为 `fail`。

### 2.3 机制 B（运行时 ID 改名）仍然启用

`registry.py:353` 默认由环境变量控制，且**默认关闭**：

```python
# settings.py:633
PLUGIN_ENABLE_ID_CONFLICT_CHECK = os.getenv("PLUGIN_ENABLE_ID_CONFLICT_CHECK", "false").lower() in (...)
```

**但 `registry_service.py:688` 硬编码为 `True`**：

```python
runtime_plugin_id = target_plugin_id if source_replacement else _resolve_plugin_id_conflict(
    target_plugin_id,
    ...
    purpose="register",
    enable_rename=True,   # ← 硬编码
)
```

**关键护栏**：`source_replacement` 为真时**跳过**改名。builtin→user 覆盖属于
source replacement，因此同一逻辑插件的两个候选不会互相改名。

**残余风险**：改名只在「两个**不同**插件抢同一个 ID」时发生。这与 wislap 描述的
「同一插件跨目录被改名」不是同一场景。但 `source_replacement` 的判定是否在所有
边界情况下都正确，需要专项测试确认。

### 2.4 判定

**⚠️ 部分守护**：目录改名已封堵；运行时 ID 改名仍在，靠 `source_replacement` 护栏，
护栏正确性未被专项测试锁定。

---

## 三、不变量 7：候选切换不丢用户启停偏好

### 3.1 已有测试覆盖

`upstream/main:plugin/tests/unit/server/test_plugins_lifecycle_service.py`：

| 行号 | 测试 | 覆盖点 |
|---|---|---|
| 468 | `test_delete_user_override_restores_running_builtin_and_preserves_state` | 删 user 覆盖 → builtin 恢复运行 + 状态保留 |
| 579 | `test_delete_user_override_preserves_disabled_preference_for_restored_builtin` | **删 user 覆盖后 disabled 偏好不丢** |
| 825 | `test_persist_user_runtime_intent_migrates_resolved_plugin_preferences` | 偏好随解析后的 plugin id 迁移 |
| 3145 | `test_delete_plugin_clears_runtime_override` | 彻底删除时清理偏好 |
| 3230 | `test_stop_plugin_persist_user_intent_writes_runtime_override` | 用户停止 → 写入偏好 |
| 3271 | `test_stop_plugin_internal_call_does_not_touch_override` | **内部调用不污染用户偏好** |
| 3358 | `test_stop_plugin_returns_partial_success_on_preference_write_failure` | 偏好写失败 → 部分成功语义 |

### 3.2 判定

**✅ 已守护**（上一份报告标注 ❓ 有误）。

`:579` 与 `:3271` 两条恰好覆盖了 wislap 最关心的两个方向：
切换候选后保留偏好，以及系统内部动作不能被误当成用户意图。

---

## 四、不变量 9：单个坏候选不中断整个 inventory

### 4.1 已有测试覆盖

`upstream/main:plugin/tests/unit/server/test_plugin_registry_service.py`：

| 行号 | 测试 | 覆盖点 |
|---|---|---|
| 241 | `test_refresh_registry_uses_manifest_defaults_when_overrides_are_invalid` | 坏 override → 回落 manifest 默认 |
| 364 | `test_refresh_registry_keeps_existing_metadata_when_config_parse_fails` | 解析失败 → 保留旧元数据 |
| 412 | `test_refresh_registry_marks_syntax_error_plugin_failed_without_aborting` | **语法错误插件标记失败，不中断 refresh** |
| 453 | `test_refresh_registry_marks_entry_directory_mismatch_failed` | 目录/ID 不一致 → 标记失败 |
| 594 | `test_refresh_plugin_marks_missing_simple_plugin_dependency_failed` | 依赖缺失 → 标记失败 |

### 4.2 判定

**✅ 已守护**（上一份报告标注 ❓ 有误）。

`:412` 的测试名直接就是 wislap 的要求：`marks_..._failed_without_aborting`。

---

## 五、不变量 2：一个 ID 多候选、只有一个生效

### 5.1 已有测试覆盖

同文件 `test_plugin_registry_service.py`：

| 行号 | 测试 | 覆盖点 |
|---|---|---|
| 819 | `test_noncanonical_user_conflict_keeps_builtin_declared_id_and_runtime_context` | 非规范 user 目录不夺取 builtin 的 ID |
| 864 | `test_noncanonical_user_conflict_cannot_replace_canonical_user_override` | 非规范目录不能替换规范 user 覆盖 |
| 914 | `test_canonical_user_override_precedes_earlier_legacy_conflict` | 规范 user 覆盖优先于历史冲突项 |

### 5.2 判定

**⚠️ 部分守护**：builtin + 1 个规范 user 候选的组合有测试锁定；
**多个 user 候选共存不支持**，这是新领域能力，不是现有缺陷。

---

## 六、不变量 5 + 降级承诺边界（调研 9）

### 6.1 替换事务禁止触碰持久数据

`upstream/main:plugin/server/application/plugins/upgrade_support.py:260 _validate_replacement_targets()`：

```python
state_roots = {get_plugin_state_root().resolve(strict=False)}
if state_root is not None:
    state_roots.add(state_root.resolve(strict=False))
forbidden = [
    target for target in targets
    if any(_path_is_within(target, root) or _path_is_within(root, target)
           for root in state_roots)
]
if forbidden:
    raise ValueError("plugin persistent state paths cannot be replacement targets: ...")
```

同一函数还禁止 builtin root 作为替换目标，并禁止目标之间重叠。

### 6.2 「rollback completed」的准确含义

`replace_plugin` 的回滚（`upgrade_support.py` 的 `_rollback_targets`）恢复的是：

| 内容 | 是否回滚 |
|---|---|
| 插件代码目录 | ✅ 从 backup 目录 rename 回来 |
| package profile（安装器拥有） | ✅ 经 `preserve_targets` merge 回来 |
| manifest 旁的 legacy profile | ✅ `_restore_manifest_adjacent_profiles` |
| **持久数据**（state root 下的 `config`/`data`/`cache`） | ❌ **从不参与替换，因此也不回滚** |

**wislap 的第 6 条完全正确**：

> `rollback completed` 很可能只代表代码目录恢复，而不是插件真正恢复可用。

**证实**：`rollback_status == "completed"` 的准确含义是
**「代码目录与安装器拥有的 profile 已恢复」**，不包含插件业务数据。
若新版本启动后已改写 `data/` 下的数据库或数据格式，回滚代码不保证旧代码还能读。

### 6.3 判定

- 不变量 5（代码与持久数据分离）：**✅ 已守护**，且有显式 fail-closed 校验
- 降级数据承诺：**❌ 文档缺口**，代码行为正确但 `rollback_status` 的语义未向用户说明

---

## 七、11 条不变量守护现状（修订版）

| # | 不变量 | 判定 | 依据 |
|---|---|---|---|
| 1 | `plugin_id` 不通过改名解决冲突 | ⚠️ 部分 | 目录改名已封堵（两个入口强制 `fail`）；运行时 ID 改名靠 `source_replacement` 护栏，未被专项测试锁定 |
| 2 | 一个 ID 多候选、只有一个生效 | ⚠️ 部分 | builtin + 1 规范 user 有测试（`:819/:864/:914`）；多 user 候选不支持 |
| 3 | 四类来源同一候选模型 | ❌ 缺 | 无统一 `PluginCandidate`；无 `manual/unmanaged` 概念 |
| 4 | 只有 managed 候选可升级/卸载 | ❌ 缺 | 无 managed/unmanaged 区分 |
| 5 | 代码与持久数据分离 | ✅ 有 | `_validate_replacement_targets` fail-closed |
| 6 | 五类操作走同一文件事务 | ⚠️ 部分 | `replace_plugin` 共享；**卸载走 lifecycle 独立路径**（7 个函数） |
| 7 | 候选切换不丢启停偏好 | ✅ 有 | 7 条测试，含 `:579`/`:3271` |
| 8 | 磁盘是候选存在的事实来源 | ✅ 有 | `reconciler.py` + `scanner.py` 每次重扫 |
| 9 | 单个坏候选不中断 inventory | ✅ 有 | 5 条测试，含 `:412` |
| 10 | 修改统一串行，调用方不接触 revision/锁 | ⚠️ 部分 | 已串行化（`plugin_operation_lock`）；但调用方仍需装配 12 个回调 |
| 11 | 返回阶段/回滚状态/最终候选 | ⚠️ 部分 | `ReplacePluginResult` 有 `stage`/`rollback_status`；卸载路径无对应结构；`rollback_status` 语义未文档化 |

**统计（修订后）**：✅ 4 条 / ⚠️ 5 条 / ❌ 2 条

对比上一份报告（✅2 / ⚠️5 / ❌2 / ❓2）——**upstream/main 的实际守护程度比我上一轮评估的更好**。

---

## 八、真正的缺口收敛为 3 项

排除已守护和"新能力"后，真正需要重构解决的是：

### 缺口 1：卸载路径未走共享事务（不变量 6）

7 个包文件函数住在 `lifecycle_service.py`（1861 行）里：

| 行号 | 函数 |
|---|---|
| 391 | `_delete_plugin_directory_sync` |
| 521 | `_record_deferred_profile_cleanup_sync` |
| 549 | `_retry_deferred_profile_cleanup_sync` |
| 615 | `_stage_orphaned_package_profile_sync` |
| 777 | `_restore_staged_package_profile_sync` |
| 784 | `_finalize_staged_package_profile_sync` |
| 793 | `_mark_install_source_removed_sync` |

### 缺口 2：调用方仍需装配 12 个回调（不变量 10）

见上一份报告第二节：12 个参数中 5 个在两处完全相同，各写一遍；
Market 路径还多一层 `dict[str, Any]` + `**` 展开，丢失类型检查。

### 缺口 3：候选模型缺 managed/unmanaged（不变量 3、4）

无法区分「用户手动解压的目录」与「包安装的目录」，
因此无法保证「只有 managed 候选可以被 N.E.K.O 升级或删除」。

---

## 九、结论

### 9.1 upstream/main 的状态比 Issue 描述的好得多

Issue #2994 描述的 4 个问题（并发、一致性、性能、Git 友好）全部不成立（调研 1–4）。
wislap 提出的 11 条不变量中，**4 条已完全守护、5 条部分守护**，
且关键的失败隔离（#9）和偏好保留（#7）都有针对性测试。

### 9.2 真正值得重构的只有 3 项

1. **卸载路径收口** —— 把 7 个包文件函数从 lifecycle 迁出，接入同一事务
2. **回调装配收口** —— 恒定回调收进模块内部，用类型化 request 替代 `dict[str, Any]`
3. **候选模型补全** —— 引入 managed/unmanaged 区分

**这 3 项都不需要改存储格式。** 不需要单文件 Registry、不需要 CAS、不需要 JSONL。

### 9.3 对 Phase 1 代码的最终判断

Phase 1 实现的 `PluginRegistryStore`（CAS + revision + portalocker）
**解决的是不存在的问题**（调研 1 已证明并发已被 `plugin_operation_lock` 串行化）。

建议：
- `PluginRegistryStore` / `audit_log.py` / `migration.py` —— **不进入生产**
- `models.py` 的 `PluginCandidate` 抽象 —— **可作为缺口 3 的起点**
- `candidates.py` 的 `resolve_effective_candidate` —— **可作为统一候选选择的起点**
- `benchmark_plugin_startup.py` —— **已独立提交，作为性能基线工具保留**

---

**报告结束**

已完成调研：
1. ✅ 多进程并发
2. ✅ 一致性风险
3. ✅ 性能 Benchmark
4. ✅ 环境复现需求
5. ✅ 模块职责重叠量化
6. ✅ 不变量 7（偏好保留）
7. ✅ 不变量 9（坏候选隔离）
8. ✅ `_1` 改名逻辑现状
9. ✅ 降级数据承诺边界
