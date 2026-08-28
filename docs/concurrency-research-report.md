# 插件系统并发模型调研报告

**日期**: 2026-08-28  
**调研目标**: 验证"多进程并发写入"是否为真实需求

---

## 一、关键发现

### 1.1 当前并发控制机制

**已存在统一的锁机制**: `plugin_operation_lock`

**位置**: `plugin/server/application/plugins/operation_lock.py`

**实现方式**:
```python
class _HeldPluginOperationLock:
    # 1. 进程内锁 (_PROCESS_LOCK: asyncio.Lock)
    # 2. 跨进程文件锁 (_acquire_file_lock_cancellation_safe)
    
    async def __aenter__(self):
        await _PROCESS_LOCK.acquire()  # asyncio.Lock
        self._file_lock_handle = await _acquire_file_lock_cancellation_safe()  # 文件锁
```

**关键特性**:
- **进程内**: asyncio.Lock（同一进程的多个异步任务串行化）
- **跨进程**: 文件锁（不同进程互斥）
- **可重入**: 支持同一 task 嵌套调用

---

### 1.2 谁在使用这个锁？

**使用 `@serialized_plugin_operation` 装饰器的函数**:

| 模块 | 函数 | 作用 |
|---|---|---|
| `lifecycle_service.py:797` | `start_plugin()` | 启动插件 |
| `lifecycle_service.py:1825` | `safe_replace_with_restart()` | 升级插件 |
| `plugin_cli/service.py:249` | `safe_upgrade()` | CLI 升级 |
| `plugin_cli/service.py:472` | `safe_replace()` | CLI 替换 |
| `plugin_cli/service.py:477` | `safe_uninstall()` | CLI 卸载 |
| `market_replacement.py:45` | `safe_replace_builtin()` | Market 覆盖内置 |
| `profile_removal_runtime.py:182` | `remove_profile_directory()` | 删除 profile |

**观察**:
- 所有插件安装/升级/卸载操作都已经通过 `plugin_operation_lock` 串行化
- 这个锁**同时保护进程内并发和跨进程并发**

---

### 1.3 CLI 是否独立进程？

**发现**: CLI **不直接写状态文件**

**证据**: `plugin/neko_plugin_cli/commands/install_cmd.py:12-16`
```python
_RUNTIME_INSTALL_DISABLED_MESSAGE = (
    "neko-plugin install does not write plugin runtime directories. "
    "Import the .neko-plugin or .neko-bundle file from the N.E.K.O Plugin Center "
    "so installation, confirmation, rollback, and source tracking use one safe workflow."
)

def handle(args: argparse.Namespace) -> int:
    print(f"[DISABLED] {_RUNTIME_INSTALL_DISABLED_MESSAGE}", file=sys.stderr)
    return 2
```

**解读**:
- CLI 的 `install` 命令已被**禁用**
- 用户必须通过 **Plugin Center (Server)** 导入包
- CLI **不独立写状态文件**

---

### 1.4 InstallSourceManager 的写入路径

**所有写入都在 Server 进程内**:

| 调用点 | 位置 | 是否有锁 |
|---|---|---|
| `record_import()` | `manager.py:1307` | ✅ (通过上层装饰器) |
| `record_market()` | `manager.py:1498` | ✅ |
| `record_market_install()` | `manager.py:1669` | ✅ |
| `record_market_upgrade()` | `manager.py:1951` | ✅ |
| `mark_profile_removed()` | `manager.py:2129` | ✅ |

**观察**:
- `InstallSourceManager` 没有自己的锁
- 但所有调用方都在 `@serialized_plugin_operation` 内

---

## 二、结论

### 2.1 多进程并发的真实性

**结论**: **不存在真正的多进程并发写入**

**理由**:

1. **CLI 不写状态**
   - CLI 的安装命令已禁用
   - 用户必须通过 Server 的 Plugin Center 操作

2. **Server 是单进程**
   - 没有找到 multiprocessing 或 fork 的启动代码
   - 插件运行在独立进程，但状态写入在主进程

3. **已有统一锁机制**
   - `plugin_operation_lock` 同时保护：
     - 进程内异步并发（asyncio.Lock）
     - 跨进程并发（文件锁）
   - 所有写入操作都在这个锁内

---

### 2.2 wislap 的质疑是对的

**她问的**:
> CLI、Server、Market Bridge 是否真的**同时**写，还是只是"可能收到多个请求"（可以串行化）？

**答案**: 
- **不是真正的多进程并发**
- **只是"可能收到多个请求"**
- **已经通过 `plugin_operation_lock` 串行化**

---

### 2.3 CAS 是否必要？

**当前机制**:
```python
# operation_lock.py
async with plugin_operation_lock.hold():
    # 持有文件锁期间，其他进程无法写入
    manager.save()  # 写入 plugins.lock.json
```

**CAS 的价值**:
- ❌ **不需要防并发**（已有锁）
- ❌ **不需要防冲突**（串行化保证只有一个写者）
- ⚠️ **可能有价值的场景**: 
  - 如果未来 CLI 重新启用独立写入
  - 如果未来支持多个 Server 实例（分布式）
  - 如果锁失效（bug 或配置错误）

**建议**:
- CAS 可以作为**防御性编程**（double-check）
- 但**不应该作为主要并发控制机制**
- 优先修复锁机制（如果有问题），而不是依赖 CAS

---

## 三、对 Phase 1 代码的影响

### 3.1 PluginRegistryStore 的定位

**如果采用单文件 Registry**:
```python
# 当前设计
class PluginRegistryStore:
    def update(self, expected_revision, mutate):
        with registry_file_lock(...):  # 文件锁
            current = self.load()
            if current.revision != expected_revision:  # CAS 检查
                raise RegistryRevisionConflict
```

**可以简化为**:
```python
class PluginRegistryStore:
    def update(self, mutate):
        # CAS 检查移除，只依赖文件锁
        with registry_file_lock(...):
            current = self.load()
            new = mutate(current)
            new.revision = current.revision + 1  # 仍然增加 revision（版本追踪）
            self._write(new)
```

**调用方不需要传 `expected_revision`**:
```python
# 之前
registry = store.load()
store.update(expected_revision=registry.revision, mutate=...)

# 简化后
store.update(mutate=...)  # 内部自动处理
```

---

### 3.2 是否仍需要 revision 字段？

**保留 revision 的理由**:
- ✅ 版本追踪（调试、审计）
- ✅ 检测文件是否被外部修改
- ✅ 未来扩展（如果需要分布式）

**但不作为并发控制机制**:
- ❌ 不暴露 `expected_revision` 给调用方
- ❌ 不依赖 CAS 防并发
- ✅ 只作为版本号自增

---

## 四、推荐方案

### 4.1 如果最终采用单文件 Registry

**架构**:
```
PluginManagement (领域层)
    ↓ 调用
InstallationCoordinator (事务协调)
    ↓ 持有
plugin_operation_lock (全局锁)
    ↓ 保护
PluginRegistryStore (存储层)
```

**关键点**:
1. `plugin_operation_lock` 继续作为主要并发控制
2. `PluginRegistryStore` 在锁内调用（不暴露 CAS）
3. `revision` 仅作为版本追踪

---

### 4.2 如果保留多文件

**架构**:
```
PluginManagement (领域层)
    ↓ 调用
InstallationCoordinator (事务协调)
    ↓ 持有
plugin_operation_lock (全局锁)
    ↓ 保护
InstallSourceManager.save() (写 3 个文件)
```

**关键点**:
1. `plugin_operation_lock` 继续作为主要并发控制
2. 3 个文件在同一个锁内原子写入
3. 不需要 CAS

---

## 五、回答 wislap 的问题

### Q: 哪些进程可以读/写插件状态？

**A**: 只有 **Server 主进程**

- CLI: 已禁用独立写入
- Plugin 子进程: 不写状态（只运行插件代码）
- Market Bridge: 在 Server 进程内

---

### Q: 是否存在无法收口到统一模块的写入路径？

**A**: **不存在**

- 所有写入都通过 `InstallSourceManager`
- 所有调用方都在 `@serialized_plugin_operation` 内
- 已经串行化

---

### Q: 当前代码是否已有锁机制？

**A**: **有，且很完善**

- `plugin_operation_lock` 同时保护进程内和跨进程
- 文件锁 + asyncio.Lock + 可重入
- 覆盖所有写入操作

---

## 六、建议的回复内容

给 wislap 的回复：

> **调研结果**: 你的质疑完全正确。
> 
> 当前代码**不存在真正的多进程并发写入**：
> 1. CLI 的 `install` 命令已禁用，用户必须通过 Server 的 Plugin Center 操作
> 2. Server 是单进程，所有写入都在 `@serialized_plugin_operation` 装饰器保护下
> 3. 已有 `plugin_operation_lock`（文件锁 + asyncio.Lock）串行化所有操作
> 
> **CAS 不是必要的并发控制机制**。当前的锁机制已经足够。
> 
> **调整方案**：
> - 如果采用单文件 Registry，`revision` 只作为版本追踪，不暴露给调用方
> - 并发控制继续依赖 `plugin_operation_lock`
> - `PluginRegistryStore` 在锁内调用，调用方不感知 revision

---

**报告结束**

需要补充的调研：
1. ✅ 多进程并发场景（已完成）
2. ⏳ 一致性风险（是否有真实案例）
3. ⏳ 性能 Benchmark
4. ⏳ 环境复现需求
