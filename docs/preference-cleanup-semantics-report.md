# 偏好清理语义调研报告（调研 11）

**日期**: 2026-08-29  
**基线**: `upstream/main`  
**调研目标**: 回答 wislap 第 7 节的问题："卸载最后一个候选后是否清理偏好"的语义是否符合预期

---

## 一、当前行为：分情况处理

### 1.1 delete_plugin 的偏好清理逻辑

`lifecycle_service.py:delete_plugin` (:1763)：

```python
restored_meta = await asyncio.to_thread(_get_plugin_meta_sync, plugin_id)
restored_builtin = bool(
    restored_meta
    and restored_meta.get("effective_source") == "builtin"
)

if not restored_builtin:
    await asyncio.to_thread(clear_runtime_override, plugin_id)

if is_running and restored_builtin:
    await self.start_plugin(plugin_id, refresh_registry=False)
```

**判定逻辑**：

| 删除后状态 | 清理偏好？ | 依据 |
|---|---|---|
| `restored_builtin == False` | ✅ **清理** | 调用 `clear_runtime_override(plugin_id)` |
| `restored_builtin == True` | ❌ **保留** | 跳过 `clear_runtime_override`，偏好继续生效 |

---

## 二、什么时候 `restored_builtin == True`？

### 2.1 Builtin 候选恢复机制

`delete_plugin` 删除目录后，调用 `refresh_registry()`，Registry 重新扫描磁盘：

| 删除前 | 删除后磁盘 | refresh_registry 结果 | restored_builtin |
|---|---|---|---|
| User 候选覆盖 Builtin | User 目录被删，Builtin 目录仍在 | `effective_source = "builtin"` | ✅ True |
| 只有 User 候选 | User 目录被删，无其他候选 | `plugin_id` 从 Registry 消失 | ❌ False |
| 只有 Builtin 候选 | 不会走 delete_plugin（路径白名单拒绝） | N/A | N/A |

**结论**：
- **删除最后一个候选**（彻底卸载）→ `restored_builtin = False` → **清理偏好**
- **删除 User 候选但还有 Builtin**（回退到 Builtin）→ `restored_builtin = True` → **保留偏好**

---

## 三、测试覆盖验证

### 3.1 测试 1：彻底删除 → 清理偏好

`test_delete_plugin_clears_runtime_override` (:3145-3210)：

```python
# 设置：只有一个 user 候选
plugin_dir = tmp_path / "demo_plugin"
config_path.write_text("[plugin]\nid='demo_plugin'\n")

# 设置偏好
runtime_overrides_module.set_runtime_override("demo_plugin", False)
assert _isolate_runtime_overrides == {"demo_plugin": False}

# 删除
await service.delete_plugin("demo_plugin")

# 验证：偏好已清理
assert _isolate_runtime_overrides == {}
assert runtime_overrides_module.get_runtime_override("demo_plugin") is None
```

**测试判定**：✅ 彻底删除会清理偏好

---

### 3.2 测试 2：删除 User 候选，Builtin 恢复 → 保留偏好

`test_delete_user_override_preserves_disabled_preference_for_restored_builtin` (:579-647)：

```python
# 设置：User 候选覆盖 Builtin
user_config = exec_root / "study_companion" / "plugin.toml"
builtin_config = tmp_path / "builtin" / "study_companion" / "plugin.toml"

# 设置偏好：禁用 + 禁止自启
runtime_overrides_module.set_runtime_override(
    "study_companion",
    False,
    auto_start=False,
)

# refresh_registry 模拟：删除后恢复 Builtin
async def refresh_registry() -> dict[str, object]:
    with module.state.acquire_plugins_write_lock():
        module.state.plugins["study_companion"] = {
            "config_path": str(builtin_config),
            "effective_source": "builtin",  # ← 关键
            "runtime_enabled": False,
            "runtime_auto_start": False,
        }
    return {"success": True}

# 删除 User 候选
await service.delete_plugin("study_companion")

# 验证：Builtin 恢复，且偏好保留
assert response["restored_builtin"] is True
assert response["restored_builtin_started"] is False  # ← 尊重 disabled 偏好
assert _isolate_runtime_overrides == {
    "study_companion": {"enabled": False, "auto_start": False},
}
```

**测试判定**：✅ Builtin 恢复时保留偏好（包括 `disabled` 状态）

---

### 3.3 测试 3：删除 User 候选，Builtin 恢复且之前在运行 → 尊重偏好不自动启动

`test_delete_user_override_restores_running_builtin_and_preserves_state` (:468-577)：

```python
# 设置：User 候选在运行，覆盖 Builtin
with module.state.acquire_plugin_hosts_write_lock():
    module.state.plugin_hosts["study_companion"] = object()  # ← 标记为运行中

# 删除 User 候选
await service.delete_plugin("study_companion")

# 验证：Builtin 恢复，且尝试重新启动
assert response["restored_builtin"] is True
assert response["restored_builtin_started"] is True  # ← 之前在运行，恢复后重启
```

**但注意**：测试 2 的场景（`:579`）验证了 `runtime_enabled=False` 时 **不会** 重启——偏好优先于"之前在运行"的状态。

代码逻辑（`:1768`）：

```python
if is_running and restored_builtin:
    await self.start_plugin(plugin_id, refresh_registry=False)
```

**这里的 `start_plugin` 内部会检查 `runtime_enabled`**（`:1215`）：

```python
if not enabled_value:
    raise _to_domain_error(
        code="PLUGIN_DISABLED",
        message=f"Plugin '{current_plugin_id}' is disabled by plugin_runtime.enabled and cannot be started",
    )
```

**结论**：`is_running` 触发重启尝试，但 `start_plugin` 尊重偏好，所以 `disabled` 的插件不会被重启。

---

## 四、语义总结

### 4.1 当前行为矩阵

| 删除场景 | restored_builtin | 偏好清理？ | 重启行为 | 测试覆盖 |
|---|---|---|---|---|
| 删除最后一个候选（彻底卸载） | False | ✅ 清理 | 无候选，无法重启 | `:3145` |
| 删除 User，Builtin 恢复，之前未运行 | True | ❌ 保留 | 不重启 | `:579` |
| 删除 User，Builtin 恢复，之前在运行 | True | ❌ 保留 | **尝试**重启，尊重 `disabled` 偏好 | `:468` |

### 4.2 设计意图

偏好的生命周期与 **plugin_id** 绑定，不与候选绑定：

| 概念 | 生命周期 | 例子 |
|---|---|---|
| **候选**（Candidate） | 磁盘目录 | `user_root/demo/` 被删除 |
| **插件身份**（Plugin ID） | 逻辑概念 | `plugin_id="demo"` 仍存在（Builtin 候选） |
| **用户偏好**（Preference） | 绑定 plugin_id | "我不想要 `demo` 自启" — 无论哪个候选 |

**逻辑一致性**：
- 用户对 `demo` 设置了 `disabled` 偏好 → 无论是 User 候选还是 Builtin 候选，都应该 `disabled`
- 删除 User 候选 → `demo` 回退到 Builtin → **偏好应该继续生效**
- 删除最后一个候选 → `demo` 彻底消失 → **偏好没有绑定对象了，应该清理**

---

## 五、回答 wislap 的问题

### Q: "`:3145 test_delete_plugin_clears_runtime_override` 看起来是清理的，但这是否是你想要的语义？"

**A**: ✅ **是合理的语义**

**理由**：

### 5.1 语义 1：彻底卸载 → 清理偏好

当用户删除最后一个候选时，`plugin_id` 从系统中消失。此时偏好没有绑定对象，应该清理。

**用户场景**：
- 用户安装了 `demo` 插件
- 设置 `disabled` 偏好："我不要这个插件启动"
- 后来决定完全卸载 `demo`
- 几个月后重新安装 `demo`

**如果不清理偏好**：
- 用户重新安装 `demo` 后发现它不启动
- 用户困惑："我都卸载了，为什么还记得我之前的设置？"
- 偏好文件越积越多（历史上安装过的所有插件）

**清理偏好的好处**：
- 卸载 = 完全移除，包括用户数据和偏好
- 重新安装后回到"干净状态"
- 偏好文件保持精简

---

### 5.2 语义 2：候选切换 → 保留偏好

当用户删除 User 候选但 Builtin 候选仍在时，`plugin_id` 仍然存在，偏好应该继续生效。

**用户场景**：
- 系统自带 `study_companion` (Builtin)
- 用户安装了 User 候选覆盖它
- 用户对 `study_companion` 设置 `disabled` 偏好："我不要学习助手打扰我"
- 用户删除 User 候选，回退到 Builtin

**如果清理偏好**：
- Builtin 候选恢复后自动启动
- 用户困惑："我明明禁用了它，为什么又启动了？"

**保留偏好的好处**：
- 用户的意图是"我不要这个插件"，而不是"我不要这个候选"
- 候选切换对用户来说是实现细节，不应影响偏好

**测试 `:579` 专门验证这一点**：`disabled` 偏好在 Builtin 恢复后继续生效，插件不会被重启。

---

## 六、边界情况分析

### 6.1 场景：删除 Market 候选，恢复 Builtin

| 步骤 | 状态 | 偏好 |
|---|---|---|
| 1. 系统自带 Builtin | `effective_source = "builtin"` | 无偏好（使用 manifest 默认） |
| 2. 用户从 Market 安装覆盖 | `effective_source = "market"` | 无偏好 |
| 3. 用户禁用 | `effective_source = "market"` | `{"enabled": False}` |
| 4. 用户删除 Market 候选 | `effective_source = "builtin"` | `{"enabled": False}` ← **保留** |

**结果**：Builtin 恢复但保持 `disabled` 状态。

**是否合理**：✅ 是，用户的禁用意图针对 `plugin_id`，不针对候选来源。

---

### 6.2 场景：删除 Imported 候选，无其他候选

| 步骤 | 状态 | 偏好 |
|---|---|---|
| 1. 用户通过 CLI 安装 | `effective_source = "imported"` | 无偏好 |
| 2. 用户禁用 | `effective_source = "imported"` | `{"enabled": False}` |
| 3. 用户删除 | `plugin_id` 从 Registry 消失 | 偏好被 `clear_runtime_override` 清理 |
| 4. 几个月后重新安装 | `effective_source = "imported"` | 无偏好（回到 manifest 默认） |

**结果**：重新安装后插件按 manifest 默认启用（如果 manifest 是 `enabled=true`）。

**是否合理**：✅ 是，卸载 = 完全移除，重装 = 全新状态。

---

### 6.3 潜在的用户心理模型冲突

**用户可能期待的语义 A**（当前实现）：
- 偏好绑定 `plugin_id`（逻辑概念）
- 只要 ID 存在，偏好就生效（无论哪个候选）
- ID 彻底消失时才清理偏好

**用户可能期待的语义 B**（备选方案）：
- 偏好绑定候选（物理目录）
- 删除 User 候选 → 清理偏好 → Builtin 恢复时回到 manifest 默认
- 每个候选独立追踪偏好

**当前选择语义 A 的理由**：
- 简化用户心理模型："我禁用了插件 X"，而不是"我禁用了 User 候选的插件 X"
- 避免偏好丢失：用户卸载重装后不会奇怪"为什么我的设置没了"（在同一 ID 持续存在的情况下）
- 与 Registry 的设计一致：`plugin_id` 是主键，候选是实现细节

---

## 七、与缺口 3（managed/unmanaged）的关系

引入 managed/unmanaged 区分后，偏好清理逻辑可能需要调整：

### 7.1 Manual 插件的偏好语义

| 场景 | 当前行为 | 引入 managed 后的建议 |
|---|---|---|
| 用户手动放置 `demo/` | 可以禁用/启用，偏好生效 | ✅ 保持 |
| 用户删除 Manual 插件 | 如果没有其他候选，清理偏好 | ⚠️ **应该拒绝删除**（调研 10 的结论） |
| 用户用包接管 Manual 插件 | `channel` 变为 `imported`，偏好保留 | ✅ 保持（接管不改变 ID） |

**无需调整**：偏好清理逻辑只看 `restored_builtin`，不检查 `channel`。引入 managed 后只需在删除入口处守护，不影响偏好逻辑。

---

## 八、建议的改进

### 8.1 文档化语义

在 API 文档和用户手册明确：

**DELETE /plugins/{id} 的偏好处理**：
- 如果删除后 `{id}` 仍存在（Builtin 恢复），保留用户偏好
- 如果删除后 `{id}` 彻底消失，清理用户偏好
- 理由：偏好绑定插件 ID（逻辑概念），不绑定候选（物理目录）

### 8.2 补充测试

当前测试覆盖了 Builtin 恢复的场景（`:579`、`:468`），建议补充：

1. `test_delete_market_override_preserves_preference_for_restored_builtin` — Market 候选删除，Builtin 恢复，偏好保留
2. `test_delete_imported_without_builtin_clears_preference` — Imported 候选删除，无 Builtin，偏好清理（`:3145` 已覆盖 User 候选，补充 Imported 对称性）

### 8.3 UI 提示

在删除确认对话框提示用户：

**如果有 Builtin 候选**：
> 删除此插件后将恢复系统自带版本，您的启停偏好将保留。

**如果无其他候选**：
> 删除此插件后将彻底卸载，您的启停偏好也会被清除。

---

## 九、结论

### 9.1 回答 wislap 的问题

**Q**: "`:3145 test_delete_plugin_clears_runtime_override` 看起来是清理的，但这是否是你想要的语义？"

**A**: ✅ **是合理的语义**

**当前实现**：
- 删除最后一个候选（彻底卸载）→ 清理偏好
- 删除候选但有其他候选恢复（Builtin 回退）→ 保留偏好

**测试覆盖**：
- `:3145` — 彻底删除清理偏好
- `:579` — Builtin 恢复保留偏好（包括 `disabled`）
- `:468` — Builtin 恢复尝试重启但尊重 `disabled` 偏好

**设计一致性**：
- 偏好绑定 `plugin_id`（逻辑概念），不绑定候选（物理目录）
- ID 存在 → 偏好生效；ID 消失 → 偏好清理
- 符合用户直觉："我禁用了插件 X"，而不是"我禁用了某个候选"

### 9.2 无需为缺口 3 调整偏好逻辑

引入 managed/unmanaged 后：
- 偏好清理只看 `restored_builtin`，不检查 `channel`
- Manual 插件的删除守护在删除入口（调研 10 的建议），不影响偏好逻辑
- 当前设计已经足够健壮

### 9.3 建议

1. **文档化**当前语义（API 文档 + 用户手册）
2. **补充测试**覆盖 Market 候选删除 + Builtin 恢复的场景
3. **UI 提示**在删除确认对话框说明偏好处理方式

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
10. ✅ Manual 插件接管行为
11. ✅ 偏好清理语义（本报告）
