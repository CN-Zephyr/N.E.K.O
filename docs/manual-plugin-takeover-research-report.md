# Manual 插件接管现状调研报告（调研 10）

**日期**: 2026-08-29  
**基线**: `upstream/main`  
**调研目标**: 回答 wislap 第 1 节的三个问题：用户手动解压的插件目录能否被 N.E.K.O 接管（删除、升级、切换来源）

---

## 一、Manual 插件的发现与分类

### 1.1 定义

**Manual 插件**指用户手动放入 `user_root/` 的目录，不是通过 CLI 包安装、也不是从 Market 下载的。

**分类规则**（`manager.py:_seed_entry`）：

```python
channel: Channel = "builtin" if d.root_id == "builtin" else "manual"
```

- `root_id == "builtin"` → `channel = "builtin"`
- `root_id == "user"` 且**无** lock entry → `channel = "manual"`

### 1.2 发现机制

启动时 `StartupReconciler` 调用 `manager.reconcile()`，执行三路 diff：

| 情况 | lock entry | 磁盘目录 | 动作 |
|---|---|---|---|
| A1 | 无 | 有 | **Add** — 用 `_seed_entry` 创建 entry，`channel = "manual"` |
| A2 | 有，`removed=True` | 有 | **Resurrect** — 保留原 `channel`（可能是 `"imported"` / `"market"`），清除 `removed` |
| A3 | 有，`removed=False` | 有 | **Stable** — 刷新 `last_seen_at` |
| B | 有，`removed=False` | 无 | **Soft delete** — 设 `removed=True` |

**关键**: A1 分支为新目录**自动**打上 `channel="manual"` 标签（`manager.py` `:1100` 注释明确说明 Req 11.1）。

---

## 二、Manual 插件的删除行为

### 2.1 删除流程

`lifecycle_service.py:delete_plugin` (:1672-1836)：

```python
async def delete_plugin(self, plugin_id: str) -> dict[str, object]:
    # 1. 检查插件存在
    plugin_meta = await asyncio.to_thread(_get_plugin_meta_sync, plugin_id)
    plugin_dir = await asyncio.to_thread(_resolve_plugin_dir_sync, plugin_id, plugin_meta)
    
    # 2. 路径安全检查
    path_allowed = await asyncio.to_thread(_path_within_plugin_roots_sync, plugin_dir)
    if not path_allowed:
        raise PLUGIN_DELETE_FORBIDDEN_PATH
    
    # 3. 停止 → 删除目录 → 清理 profile → 标记移除
    await self.stop_plugin(plugin_id)
    await asyncio.to_thread(_delete_plugin_directory_sync, plugin_dir)
    await asyncio.to_thread(_mark_install_source_removed_sync, plugin_dir)
    ...
```

### 2.2 路径白名单检查

`_path_within_plugin_roots_sync` (:344-372)：

```python
def _path_within_plugin_roots_sync(path: Path) -> bool:
    resolved_exec_root = get_user_plugin_exec_root().resolve(strict=False)
    resolved_builtin_root = BUILTIN_PLUGIN_CONFIG_ROOT.resolve(strict=False)
    resolved_state_root = get_plugin_state_root().resolve(strict=False)
    
    # 明确排除 builtin 和 state root
    allowed_roots: set[Path] = set()
    if resolved_exec_root not in {resolved_builtin_root, resolved_state_root}:
        allowed_roots.add(resolved_exec_root)
    
    for root in PLUGIN_CONFIG_ROOTS:
        resolved_root = root.resolve(strict=False)
        if resolved_root not in {resolved_builtin_root, resolved_state_root}:
            allowed_roots.add(resolved_root)
    
    # 路径必须在白名单内
    for allowed_root in allowed_roots:
        ...
        if p_key.startswith(root_key + sep):
            return True
    return False
```

**结论**: 
- ✅ `user_root/` 下的目录（包括 manual 插件）在白名单内
- ❌ `builtin_root/` 和 `state_root/` 被**显式排除**

### 2.3 实际删除行为

| Channel | 能否删除 | 依据 |
|---|---|---|
| `manual` | ✅ **可以** | `user_root/` 在白名单；`_delete_plugin_directory_sync` 不检查 channel |
| `imported` | ✅ 可以 | 同上 |
| `market` | ✅ 可以 | 同上 |
| `builtin` | ❌ **不可以** | `builtin_root` 被显式排除 |

**关键发现**: `delete_plugin` **不检查 `channel` 字段**。只要路径在 `user_root/` 下，无论是 manual、imported 还是 market，都会被删除。

---

## 三、Manual 插件的升级行为

### 3.1 升级入口

两条路径可以触发升级：

1. **CLI 包安装** — `plugin_cli/service.py:install` → `replace_plugin`
2. **Market 升级** — `market_bridge.py:upgrade_plugin` → `replace_plugin`

### 3.2 升级前置检查

`plugin_cli/service.py:plan_install` (:224-337) 决定 `action`：

```python
action: Literal["install", "blocked", "upgrade", "override_builtin"]
```

**判定逻辑**（`install_plan.py:build_install_plan`）：

| 磁盘状态 | lock entry | action |
|---|---|---|
| 目录不存在 | 无 or `removed=True` | `"install"` |
| 目录存在，同 plugin_id | 任意 | `"upgrade"` |
| 目录存在，不同 plugin_id | — | `"blocked"` |
| builtin 存在 | `channel="builtin"` | `"override_builtin"` |

**关键**: `action="upgrade"` 的判定**不检查 channel**。只要磁盘目录存在且 `plugin_id` 匹配，就进入升级流程。

### 3.3 实际升级行为

| Channel | 能否升级 | 结果 |
|---|---|---|
| `manual` | ✅ **可以** | 用新包内容替换目录；lock entry 的 `channel` 被改写为 `"imported"` 或 `"market"` |
| `imported` | ✅ 可以 | 正常升级流程 |
| `market` | ✅ 可以 | 正常升级流程 |
| `builtin` | ⚠️ 特殊 | 走 `override_builtin` 分支，需要 Market SHA256 验证 |

**关键发现**: Manual 插件可以被升级，且升级后 `channel` **自动变成** `"imported"` 或 `"market"`——这就是**接管（takeover）**的实现。

---

## 四、Manual 插件的来源切换

### 4.1 来源记录机制

升级或重装后，`channel` 和 `source_detail` 会被**覆写**（`manager.py:record_import` / `record_market`）：

```python
# record_import (:890-950)
new_entry = dataclasses.replace(
    prev,
    channel="imported",  # ← 覆写
    source_detail=SourceDetailImported(...),
    plugin_id=plugin_id or prev.plugin_id,
    updated_at=now,
    last_seen_at=now,
    removed=False,  # 取消 soft-delete
    removed_at=None,
)
```

### 4.2 实际行为矩阵

| 初始 channel | 操作 | 新 channel | 说明 |
|---|---|---|---|
| `manual` | CLI 包安装 | `imported` | lock entry 被 `record_import` 覆写 |
| `manual` | Market 安装 | `market` | lock entry 被 `record_market` 覆写 |
| `imported` | Market 安装 | `market` | 允许切换 |
| `market` | CLI 包安装 | `imported` | 允许切换 |

**关键**: 一旦对 manual 插件执行包安装或 Market 安装，它就**永久**变成 managed（`imported` / `market`），并被纳入 N.E.K.O 的包管理体系。

---

## 五、回答 wislap 的三个问题

### Q1: 用户手动解压一个目录到 `plugins/` 后，能否通过 CLI 或 Market 接管它？

**A**: ✅ **可以**

**实现路径**：
1. 启动时 `reconcile()` 为目录打上 `channel="manual"` 标签
2. 用户执行 CLI 包安装或 Market 安装
3. `plan_install` 判定为 `"upgrade"`（不检查 channel）
4. `replace_plugin` 替换目录内容
5. `record_import` / `record_market` 覆写 lock entry，`channel` 变为 `"imported"` / `"market"`

**接管完成** — 该目录从此被 N.E.K.O 管理。

---

### Q2: N.E.K.O 怎么知道这个目录"可以安全删除"？

**A**: ❌ **不知道**

**当前逻辑**：
- `delete_plugin` 只检查路径是否在 `user_root/` 下（白名单）
- **不检查** `channel` 字段
- 不区分"用户手动放的"和"N.E.K.O 安装的"

**隐藏风险**：
- 用户手动放了一个插件目录
- `reconcile()` 自动打上 `channel="manual"`
- 用户在 UI 点"删除"
- N.E.K.O 直接 `shutil.rmtree(plugin_dir)`

**用户可能期待的行为**：
- Manual 插件应该"只读"——可以禁用、不能删除
- 或者删除前有明确警告："此插件不是通过 N.E.K.O 安装的，删除后无法恢复"

---

### Q3: 如果不能接管，应该如何拒绝？

**A**: **当前可以接管，但应该有选择性拒绝机制**

**建议拒绝策略**（对应缺口 3 的 managed/unmanaged 区分）：

| 操作 | Manual (unmanaged) | Imported/Market (managed) |
|---|---|---|
| **删除** | ⚠️ 拒绝或警告 | ✅ 允许 |
| **升级** | ✅ 允许（接管） | ✅ 允许 |
| **降级** | ❌ 拒绝 | ✅ 允许 |
| **切换来源** | ✅ 允许（接管） | ✅ 允许 |

**拒绝方式**（参考 builtin 的 `BUILTIN_CHANNEL_LOCKED` 错误）：

```python
if entry.channel == "manual":
    if operation == "delete":
        raise InstallSourceError(
            code="MANUAL_PLUGIN_DELETE_FORBIDDEN",
            message=f"Plugin '{plugin_id}' was manually installed and cannot be deleted by N.E.K.O",
            details={"plugin_id": plugin_id, "directory": str(plugin_dir)},
        )
```

**UI 提示**：
- 删除按钮灰显 + tooltip："此插件由您手动添加，请直接删除目录"
- 或者弹窗确认："此插件不是通过 N.E.K.O 安装的，确定删除吗？删除后无法恢复。"

---

## 六、与缺口 3（managed/unmanaged）的关系

### 6.1 当前 `channel` 字段不等于 managed/unmanaged

| Channel | 是否 managed？ | 删除行为 |
|---|---|---|
| `builtin` | ❌ Unmanaged（系统自带） | 拒绝（路径白名单排除） |
| `manual` | ❓ **语义不清** | 允许删除（风险） |
| `imported` | ✅ Managed（CLI 安装） | 允许删除 |
| `market` | ✅ Managed（Market 安装） | 允许删除 |

**问题**: `manual` 的删除行为与 `imported`/`market` 相同，但用户心理预期不同。

### 6.2 缺口 3 的解决方向

引入显式的 **`managed: bool`** 字段（或等价的枚举）：

```python
@dataclass
class LockEntry:
    ...
    channel: Channel
    managed: bool  # ← 新增
```

**判定规则**：
- `channel == "builtin"` → `managed = False`（系统自带，不可删）
- `channel == "manual"` → `managed = False`（用户手动，删除需警告）
- `channel in ["imported", "market"]` → `managed = True`（N.E.K.O 安装，可删）

**删除守护**：
```python
if not entry.managed and operation == "delete":
    raise UNMANAGED_PLUGIN_DELETE_FORBIDDEN
```

---

## 七、实测验证（建议补充）

上述结论来自代码静态分析。建议补充**实际操作验证**：

### 7.1 手动插件发现测试

1. 手动解压一个插件到 `user_root/demo_manual/`
2. 重启 N.E.K.O Server
3. 检查 `plugins.lock.json` 是否出现 `"channel": "manual"` entry

### 7.2 手动插件删除测试

1. 在 UI 或 CLI 执行删除
2. 观察是否成功删除
3. 检查是否有警告或确认提示

### 7.3 手动插件升级测试

1. 手动放置 `demo` v1.0.0
2. 用 CLI 安装 `demo` v1.1.0 的包
3. 检查：
   - 目录内容是否被替换
   - lock entry 的 `channel` 是否变为 `"imported"`
   - 再次删除是否仍然成功

---

## 八、结论

### 8.1 回答 wislap 的三个问题

| 问题 | 答案 | 现状 |
|---|---|---|
| Q1: 能否接管 manual 插件？ | ✅ 可以 | 升级时自动改写 `channel` |
| Q2: 如何判断可安全删除？ | ❌ 不判断 | `delete_plugin` 不检查 `channel` |
| Q3: 如何拒绝？ | ⚠️ 应该但没有 | 建议引入 `managed` 字段 |

### 8.2 隐藏风险

**Manual 插件可被误删**：
- 用户手动放置插件
- `reconcile()` 自动打 `"manual"` 标签
- 用户在 UI 点删除
- N.E.K.O 直接 `rmtree`，无警告、无确认

**这与用户心理预期不符**：
- 用户认为手动放的东西"只读"，不会被 N.E.K.O 删除
- 实际上删除操作**不区分**来源

### 8.3 缺口 3 的具体形态

缺口 3（managed/unmanaged 区分）包含两个子问题：

1. **模型缺失** — `LockEntry` 没有 `managed` 字段
2. **守护缺失** — `delete_plugin` 不检查来源就删除

**解决方案**：
- 添加 `managed: bool` 到 `LockEntry`
- 在 `delete_plugin` 开头增加守护：
  ```python
  if not entry.managed:
      raise UNMANAGED_PLUGIN_DELETE_FORBIDDEN
  ```
- UI 显示差异：manual 插件的删除按钮灰显或有警告

### 8.4 与其他缺口的关系

| 缺口 | 与 manual 插件的关系 |
|---|---|
| 缺口 1（卸载收口） | Manual 插件走同一个 `delete_plugin`，若卸载收口，守护点统一 |
| 缺口 2（回调装配） | 与 manual 无关（装配重复是调用方问题） |
| 缺口 3（managed 区分） | **直接相关** — manual 是 unmanaged 的典型代表 |

---

## 九、补充建议

### 9.1 测试覆盖

在 `test_plugins_lifecycle_service.py` 补充测试：

1. `test_delete_manual_plugin_requires_confirmation` — manual 插件删除应提示
2. `test_upgrade_manual_plugin_changes_channel` — 升级后 channel 变为 imported
3. `test_reconcile_assigns_manual_channel` — 手动目录自动打 manual 标签

### 9.2 文档改进

在 API 文档明确：
- `DELETE /plugins/{id}` 只能删除 managed 插件（`imported` / `market`）
- Manual 和 builtin 插件应该通过文件系统直接操作
- 或者提供 `force=true` 参数明确用户意图

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
10. ✅ Manual 插件接管行为（本报告）
