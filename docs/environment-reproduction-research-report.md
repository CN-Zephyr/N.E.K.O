# 插件环境复现需求调研报告

**日期**: 2026-08-28  
**调研目标**: 确认是否需要"复现插件环境"功能（类似 requirements.txt）

---

## 一、GitHub Issue/Discussion 搜索结果

### 1.1 搜索关键词

- "reproduce environment"
- "requirements.txt"
- "lock file"
- "plugin lock"
- "dependency lock"
- "reproducible"
- "environment setup"

### 1.2 搜索结果

**结果**: ❌ **未找到任何相关需求**

**唯一相关**: Issue #2994 (本 Issue) 提到"Git 友好"，但这已被 wislap 质疑为混淆了运行状态和声明文件。

---

## 二、现有代码中的相关功能

### 2.1 插件依赖系统

**存在**: ✅ 插件可以声明对其他插件的依赖

**位置**: `plugin/core/dependency.py`

**示例**:
```toml
# plugin.toml
[plugin]
dependencies = ["shared_plugin", "utils_plugin"]
```

**功能**:
- 插件间依赖解析
- 版本约束检查
- 依赖冲突检测

**但不是环境复现**:
- 这是**运行时依赖**（插件 A 依赖插件 B）
- 不是**环境声明**（记录当前安装了哪些插件）

---

### 2.2 Python 依赖

**存在**: ✅ 插件可以声明 Python 包依赖

**位置**: `plugin/core/python_dependencies.py`

**示例**:
```toml
# pyproject.toml
[project]
dependencies = ["httpx>=0.27", "pydantic>=2.0"]
```

**功能**:
- 收集插件的 Python 依赖
- 过滤掉插件间依赖（如 "N.E.K.O>=0.1"）
- 用于安装 Python 包

**但不是环境复现**:
- 这是**单个插件的依赖**
- 不是**全局插件列表**

---

### 2.3 插件导出/冻结功能

**存在**: ❌ **未找到**

**搜索结果**:
- `export` 只用于打包插件（`export_package`）
- `freeze` 只用于插件生命周期（`freeze`/`unfreeze` 事件）
- 没有 `neko-plugin freeze` 或 `neko-plugin list --export` 命令

---

## 三、类似项目的实践

### 3.1 Python (pip)

**环境复现**:
```bash
# 导出
pip freeze > requirements.txt

# 复现
pip install -r requirements.txt
```

**格式**:
```
httpx==0.27.0
pydantic==2.5.0
```

---

### 3.2 Node.js (npm)

**环境复现**:
```json
// package-lock.json
{
  "dependencies": {
    "express": {
      "version": "4.18.2",
      "resolved": "https://...",
      "integrity": "sha512-..."
    }
  }
}
```

**特点**:
- 锁定精确版本
- 记录下载源
- 校验哈希

---

### 3.3 VS Code (Extensions)

**环境复现**:
```bash
# 导出
code --list-extensions > extensions.txt

# 复现
cat extensions.txt | xargs -L 1 code --install-extension
```

**格式**:
```
ms-python.python
dbaeumer.vscode-eslint
```

---

## 四、N.E.K.O 插件环境复现的需求场景

### 4.1 可能的场景

**场景 1: 开发者协作**
- 开发者 A 在本地配置了插件环境
- 开发者 B 想复现同样的插件集合
- 需要导出插件列表供 B 安装

**场景 2: 问题复现**
- 用户报告 Bug
- 开发者想复现用户的插件环境
- 需要导出插件列表供开发者调试

**场景 3: 部署自动化**
- 在多台机器部署 N.E.K.O
- 需要相同的插件配置
- 需要声明文件供自动化脚本安装

**场景 4: 版本回滚**
- 用户想回到之前的插件配置
- 需要历史快照

---

### 4.2 当前是否有这些场景？

**调研结果**: ❌ **未找到证据**

**原因**:
1. GitHub 搜索无相关 Issue
2. 代码没有 freeze/export 命令
3. 用户文档没有提到环境复现

**可能原因**:
- N.E.K.O 是桌面应用（不是服务端，部署需求少）
- 插件安装主要通过 Plugin Center（有 UI，手动操作）
- 社区规模较小（协作场景少）

---

## 五、"Git 友好"的真实含义

### 5.1 Issue 的说法

> Registry 应该 Git 友好，方便开发者提交到版本控制。

### 5.2 wislap 的质疑

> 用户数据目录（`~/.neko/`）的运行状态不应提交到 Git。启停偏好、安装时间、本机路径都不适合 Git。

### 5.3 验证结果

**wislap 完全正确**。

**不应该提交的内容**:
```json
// ~/.neko/plugin_registry.json (运行状态)
{
  "plugins": {
    "demo": {
      "installed_at": "2026-08-20T10:00:00Z",  // 本机时间
      "user_candidate": {
        "root_id": "user",
        "directory_name": "demo",
        "config_path": "/Users/alice/.neko/plugins/demo"  // 本机路径
      },
      "runtime_enabled": false  // 用户偏好
    }
  }
}
```

**如果需要 Git 提交，应该是独立的声明文件**:
```json
// neko-plugins.lock (可复现声明)
{
  "plugins": [
    {
      "id": "demo",
      "version": "1.2.3",
      "source": "market",
      "package_sha256": "abc123..."
    },
    {
      "id": "utils",
      "version": "2.0.0",
      "source": "market"
    }
  ]
}
```

**区别**:
- 运行状态: 本机路径、偏好、时间戳 → **不 Git 友好**
- 声明文件: 插件 ID、版本、来源 → **Git 友好**

---

## 六、结论

### 6.1 当前不存在"环境复现"需求

**证据**:

1. **GitHub 搜索无结果**
   - 没有用户提出相关需求
   - 没有 Issue/Discussion 讨论环境复现

2. **代码没有相关功能**
   - 没有 `freeze` / `export` 命令
   - 没有导出插件列表的 API

3. **应用场景不强**
   - N.E.K.O 是桌面应用（不是服务端）
   - 插件通过 UI 安装（不是命令行脚本）
   - 社区规模较小（协作需求少）

---

### 6.2 "Git 友好"是伪需求

**wislap 说得对**:

> 用户数据目录的运行状态不应提交到 Git。

**Issue 的说法是错误的**:
- Registry 记录的是**运行状态**（本机路径、偏好、时间）
- 这些内容**不应该** Git 友好
- 类比: `.git/config` 不应该提交，`package-lock.json` 才应该

**如果未来需要环境复现**:
- 设计**独立的声明文件**（如 `neko-plugins.lock`）
- 只记录 `plugin_id + version + source`
- 不包含本机路径、偏好、时间戳

---

### 6.3 建议

**当前**: ❌ **不需要实现环境复现功能**

**理由**:
1. 没有真实需求
2. 应用场景不强
3. "Git 友好"混淆了概念

**如果未来有需求**:
1. 先收集用户场景（开发者协作？问题复现？部署自动化？）
2. 设计独立的声明文件格式
3. 实现 `neko-plugin freeze` / `neko-plugin install --from-lock` 命令

---

## 七、回答 wislap 的问题

### Q: 是否有"复现环境"的真实需求？

**A**: ❌ **未找到**

- GitHub 搜索无相关 Issue
- 代码没有相关功能
- N.E.K.O 的应用场景（桌面应用）不强

---

### Q: "Git 友好"是指什么？

**A**: **Issue 混淆了概念**

- 运行状态（Registry）**不应该** Git 友好
- 如需环境复现，应该设计**独立的声明文件**
- wislap 的质疑完全正确

---

## 八、对比其他 3 个调研

| 调研 | Issue 说法 | 调研结果 | wislap 正确性 |
|---|---|---|---|
| 1. 多进程并发 | 需要 CAS 防并发 | 不存在，已有锁 | ✅ 正确 |
| 2. 一致性风险 | 3 文件写入不一致 | 不是真实问题 | ✅ 正确 |
| 3. 性能 | 600ms → 35ms | 实测 13ms | ✅ 正确 |
| 4. Git 友好 | Registry 应 Git 友好 | 混淆了概念 | ✅ 正确 |

**总结**: wislap 的 **4 个质疑全部正确**。

---

## 九、建议的回复内容

给 wislap 的回复：

> **调研结果**: 你的第 4 个质疑也是对的。
> 
> 我搜索了所有相关 Issue 和代码，**未找到"环境复现"的真实需求**：
> 1. GitHub 无相关 Issue
> 2. 代码没有 freeze/export 功能
> 3. N.E.K.O 是桌面应用，部署/协作场景不强
> 
> **你说的完全正确**：
> - 运行状态（Registry）记录本机路径、偏好、时间，**不应该** Git 友好
> - 如果真需要环境复现，应该设计**独立的声明文件**（类似 `package-lock.json`）
> - "Git 友好"混淆了两个不同的概念
> 
> **4 个调研全部完成，你的质疑全部被证实**：
> 1. ✅ 多进程并发 - 不存在
> 2. ✅ 一致性风险 - 不是真实问题
> 3. ✅ 性能 600ms - 无法重现（实测 13ms）
> 4. ✅ Git 友好 - 混淆了概念

---

**报告结束**

所有 4 个调研已完成：
1. ✅ 多进程并发场景
2. ✅ 一致性风险
3. ✅ 性能 Benchmark
4. ✅ 环境复现需求

**下一步**: 汇总所有调研结果，发到 Issue #2994
