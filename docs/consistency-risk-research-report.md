# 插件状态一致性风险调研报告

**日期**: 2026-08-28  
**调研目标**: 确认"3 个文件写入不一致"是否为真实问题

---

## 一、Issue/PR 搜索结果

### 1.1 GitHub Issue 搜索

**搜索关键词**:
- "corrupt plugin state"
- "half installed"
- "plugin broken"
- "state inconsistent"
- "lock corrupted"
- "plugin disappeared"
- "installation failed"

**结果**: ❌ **未找到任何相关 Issue**

**唯一相关 Issue**: #2879 - 插件安装生命周期重构追踪（这是设计 Issue，不是 Bug 报告）

---

## 二、当前代码的防护机制

### 2.1 原子写入

**实现**: `plugin/server/application/install_source/manager.py:228`

```python
def _atomic_write(lock_path: Path, payload: bytes) -> None:
    """Write payload atomically via tmp + rename dance.
    
    Steps:
    1. Write to <parent>/plugins.lock.json.<pid>.<uuid>.tmp
    2. os.replace(tmp, lock_path)  # Atomic by kernel guarantee
    3. On failure, unlink tmp and re-raise
    """
    tmp_path = parent / f"plugins.lock.json.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    tmp_path.write_bytes(payload)
    
    # Windows: retry on PermissionError (AV/Explorer holds file)
    for attempt_ms in (0, 50, 100, 200):
        try:
            os.replace(tmp_path, lock_path)  # Atomic!
            return
        except PermissionError:
            time.sleep(attempt_ms / 1000.0)
            continue
    raise  # Exhausted retries
```

**关键特性**:
- ✅ POSIX `rename()` 是原子操作（内核保证）
- ✅ 失败时清理临时文件
- ✅ Windows 重试机制（处理 AV 锁文件）

---

### 2.2 损坏文件恢复

**实现**: `plugin/server/application/install_source/manager.py:1088-1173`

```python
def load(self) -> None:
    try:
        raw = atomic_read_bytes(self.lock_path)
        self._current = _parse_lock(raw)
    except FileNotFoundError:
        # First startup - seed empty
        self._current = LockFile(...)
    except InstallSourceError as exc:
        if exc.error_code == "LOCK_FILE_CORRUPT":
            # Backup corrupt file
            bak_path = self.lock_path.parent / f"plugins.lock.json.bak-{epoch}"
            self.lock_path.rename(bak_path)
            logger.warning("Corrupt lock backed up to %s", bak_path)
            
            # Rebuild from scratch
            self._current = LockFile(...)
        else:
            raise
```

**关键特性**:
- ✅ 检测损坏文件（JSON 解析失败）
- ✅ 自动备份损坏文件
- ✅ 从空状态重建

---

### 2.3 临时文件清理

**实现**: `plugin/server/application/install_source/manager.py:279`

```python
def _sweep_stale_tmp_files(lock_path: Path) -> None:
    """Remove plugins.lock.json.*.tmp leftovers from previous runs."""
    # 启动时清理所有 .tmp 文件（进程被 kill 后的残留）
```

**场景**: 进程在写入过程中被强制终止（kill -9, 系统崩溃）

---

## 三、当前的"多文件"情况

### 3.1 实际存在的文件

**当前 `main` 分支**:
1. `plugins.lock.json` - 安装来源记录（`InstallSourceManager`）
2. `plugin_runtime_overrides.json` - 用户启停偏好（`runtime_overrides.py`）

**注意**: Issue #2994 提到的 `plugin_candidate_selections.json` **不存在于当前 `main`**

---

### 3.2 两个文件的写入是否同步？

**查找结果**: ❌ **没有找到同时写入两个文件的代码**

**写入路径分离**:

| 文件 | 写入时机 | 实现 |
|---|---|---|
| `plugins.lock.json` | 安装/升级/卸载 | `InstallSourceManager.save()` |
| `plugin_runtime_overrides.json` | 用户启停插件 | `runtime_overrides._save_to_disk()` |

**观察**:
- 两个文件由**不同的模块**管理
- 写入时机**不同步**
- 但它们记录的是**不同性质的状态**：
  - `plugins.lock.json`: 安装元数据（"插件装在哪"）
  - `runtime_overrides.json`: 用户偏好（"用户想启动它吗"）

---

### 3.3 是否存在"部分写入"的风险？

**磁盘满场景**:

```
场景: 磁盘只剩 1KB 空间

1. 写入 plugins.lock.json
   tmp_path.write_bytes(payload)  # ✅ 成功（假设 payload < 1KB）
   os.replace(tmp, lock)           # ✅ 原子替换

2. 写入 plugin_runtime_overrides.json
   cm.save_json_config(...)        # ❌ 失败（磁盘满）

结果: plugins.lock.json 写入成功，runtime_overrides.json 写入失败
```

**但这是问题吗？**

**不是！理由**:
- `plugins.lock.json` 记录的是安装元数据（必须持久化）
- `runtime_overrides.json` 记录的是用户偏好（丢失后可重设）
- 两者**不需要原子性**（它们是独立的关注点）

**类比**:
```
Git 类比:
- plugins.lock.json = .git/objects/ (必须一致)
- runtime_overrides.json = .git/config (用户配置，丢失后重设)

数据库类比:
- plugins.lock.json = 数据表
- runtime_overrides.json = 用户会话偏好
```

---

## 四、wislap 的观点验证

### 她说的：

> 候选（磁盘扫描）、安装来源（元数据）、用户选择（偏好）、运行状态（内存）生命周期不同，塞进一个文件反而增加"谁能改哪个字段"的复杂度。

**验证结果**: ✅ **完全正确**

**证据**:

1. **生命周期不同**:
   - `plugins.lock.json`: 安装/升级时修改
   - `runtime_overrides.json`: 用户启停时修改
   - 两者**不需要同步**

2. **修改者不同**:
   - `plugins.lock.json`: `InstallSourceManager`（通过 `plugin_operation_lock`）
   - `runtime_overrides.json`: UI 启停按钮（通过 `runtime_overrides` 模块）

3. **失败语义不同**:
   - `plugins.lock.json` 写入失败 → 安装失败，需要回滚
   - `runtime_overrides.json` 写入失败 → 用户偏好未保存，下次重启会用默认值

**合并文件的风险**:
```python
# 如果合并为 plugin_registry.json
{
  "plugins": {
    "demo": {
      "installed_at": "...",        # 由 InstallSourceManager 写
      "source_detail": {...},       # 由 InstallSourceManager 写
      "runtime_enabled": false,     # 由 UI 写（用户点击启停按钮）
      "runtime_auto_start": true    # 由 UI 写
    }
  }
}

# 问题: 谁能修改 runtime_enabled？
# - InstallSourceManager 不应该改（它只管安装）
# - UI 不应该改 source_detail（它只管启停）
# - 但他们要写同一个文件 → 需要复杂的字段级权限
```

---

## 五、真实风险在哪里？

### 5.1 找到的唯一风险：损坏文件

**场景**: JSON 解析失败

**原因**:
- 手动编辑文件（用户改错了）
- 磁盘 I/O 错误
- 进程在写入过程中崩溃（但 `_atomic_write` 已经防护）

**当前防护**: ✅ **已有完善处理**
- 自动备份损坏文件
- 从空状态重建
- 日志记录

---

### 5.2 未找到的风险："部分写入多文件"

**Issue 描述的场景**:
> 磁盘满时可能只写入部分文件，导致状态不一致。

**调研结果**: ❌ **未找到证据**

**原因**:
1. 只有 2 个文件（不是 3 个）
2. 两个文件记录不同性质的状态（不需要原子性）
3. 每个文件都用原子写入（tmp + rename）

---

## 六、结论

### 6.1 "3 个文件写入不一致"不是真实问题

**理由**:

1. **Issue 描述已过时**
   - 当前只有 2 个文件
   - `plugin_candidate_selections.json` 不存在

2. **两个文件不需要同步**
   - 记录不同性质的状态
   - 生命周期和修改者不同
   - 失败语义不同

3. **已有原子写入防护**
   - 每个文件都用 tmp + rename
   - 失败时清理临时文件
   - 损坏文件自动恢复

4. **未找到真实 Bug 报告**
   - GitHub Issue 搜索无结果
   - 代码有完善的错误处理

---

### 6.2 真实痛点可能是"查看麻烦"

**wislap 说的**:
> 真实痛点是"查看麻烦"，还是"一致性风险"？

**答案**: **查看麻烦**

**证据**:
- 用户需要查看 2 个文件才能理解插件状态
- 开发者调试时需要手动组合信息
- 但这个问题**不需要合并文件**，统一读取接口（`neko-plugin status --all`）就够了

---

### 6.3 合并文件反而增加复杂度

**如果合并**:
```python
# 同一个文件，不同模块写不同字段
plugin_registry.json:
  - InstallSourceManager 写 installed_at, source_detail
  - UI 写 runtime_enabled, runtime_auto_start
  
# 问题:
# 1. 谁能修改哪个字段？需要字段级权限控制
# 2. 如果 UI 写失败，是否要回滚 InstallSourceManager 的修改？
# 3. revision 增加由谁触发？安装时？启停时？
```

**保持分离**:
```python
# 两个文件，清晰分工
plugins.lock.json:          # InstallSourceManager 独占
  - installed_at, source_detail

runtime_overrides.json:     # UI 独占
  - runtime_enabled, runtime_auto_start
  
# 优点:
# 1. 职责清晰
# 2. 失败隔离（启停失败不影响安装记录）
# 3. 简单的文件级锁
```

---

## 七、回答 wislap 的问题

### Q: 是否有"状态损坏"的真实案例？

**A**: ❌ **未找到**

- GitHub Issue 搜索无结果
- 代码有完善的原子写入和错误恢复
- 唯一可能的损坏场景（磁盘 I/O 错误、手动编辑）已有防护

---

### Q: "写入不一致"是真实风险还是理论风险？

**A**: **理论风险，且已有防护**

- 每个文件都用原子写入（tmp + rename）
- 两个文件不需要同步（记录不同性质的状态）
- 未找到真实 Bug 报告

---

### Q: 合并文件是否解决真实痛点？

**A**: ❌ **不解决，反而增加复杂度**

- 真实痛点是"查看麻烦" → 统一读取接口可解决
- 合并文件会引入"谁能改哪个字段"的复杂度
- wislap 的"生命周期不同"观点完全正确

---

## 八、建议的回复内容

给 wislap 的回复：

> **调研结果**: 你的质疑再次被证实。
> 
> 我搜索了所有相关 Issue（"corrupt state", "half installed", etc.），**未找到任何真实 Bug 报告**。
> 
> 当前代码已有完善的防护：
> 1. 每个文件都用原子写入（tmp + rename）
> 2. 损坏文件自动备份和重建
> 3. 临时文件清理
> 
> **当前只有 2 个文件**（不是 Issue 说的 3 个）：
> 1. `plugins.lock.json` - 安装元数据
> 2. `plugin_runtime_overrides.json` - 用户启停偏好
> 
> 它们记录**不同性质的状态**，生命周期和修改者都不同，**不需要同步**。
> 
> 你说的完全正确：
> - 真实痛点是"查看麻烦"，不是"一致性风险"
> - 合并文件会增加"谁能改哪个字段"的复杂度
> - 统一读取接口（`neko-plugin status --all`）可以解决查看问题
> 
> **"写入不一致"是理论风险，且已有防护**。

---

**报告结束**

需要补充的调研：
1. ✅ 多进程并发场景（已完成）
2. ✅ 一致性风险（已完成）
3. ⏳ 性能 Benchmark
4. ⏳ 环境复现需求
