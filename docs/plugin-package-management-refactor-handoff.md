# Plugin 包管理重构交接文档

> 状态：架构已冻结并完成完整 diff 验收（实现已提交；未执行真实用户配置 cutover）
>
> 更新时间：2026-08-28
> 适用工作树：`D:\Users\zheng\Desktop\code\neko-core\N.E.K.O-plugin-lifecycle-rework`

本文记录当前代码现场、已经验证的行为、已废弃的试验和下一阶段硬约束。
总体范围与阶段合同参见
`docs/plugin-package-management-refactor-working-plan.md`。

## 0. 接手者先看这里

当前不是“重构已经上线”，而是三个可验证切片集中在同一工作树：

1. Phase 2 Coordinator 已完成自动化退出门：接管 Local import、Market fresh install
   和 Market upgrade/reinstall replacement，concrete replacement/Lifecycle 适配已移出
   Route；
2. Registry schema/codec/纯迁移/CAS Store、定点读取快路径、shadow comparison、只读
   preflight、损坏留证、可恢复初始化事务、生产路径工厂和 startup authority switch 已完成；
   Selection/StateOwner/runtime intent 兼容 API 与 install-source facade 在 cutover 后只写
   Registry；
3. Plugin log clear 已完成后端、前端和 8 locale 收敛。

最重要的现场边界：生产 startup 接线已经存在，但本任务没有启动真实服务、没有对真实用户
配置执行迁移，也没有修改真实用户插件状态。候选切换、selected-running 删除 fallback、
启动失败 rollback、跨实例 Registry commit 可见性和 CAS lost-update 已有临时目录自动化合同；
真实插件进程与真实用户配置仍未获授权验证。

Phase 4 的代码边界和自动化组合门已经完成：候选代码暂存/Registry retire/最终清理已经进入
可恢复事务，profile ownership/sharing/staging/恢复/deferred cleanup 策略也已移入独立
`package_management/profile_cleanup.py`，code/profile retirement 也已由
`CandidateRemovalCoordinator` 组合成一个注入式事务。Lifecycle 的旧私有 profile helper 已
删除，profile 安全测试已直接归位到 package-management 边界，concrete Package/Profile service
及 deferred cleanup record 路径也已由 infrastructure composition root 组装；Lifecycle 不再
导入 package filesystem 实现。默认删除已经只退休候选代码并保留 profile/用户数据，8 locale
确认文案也已同步。显式数据删除事务所需的精确候选持久化提交
`mark_profile_removed()` 已在 legacy manager 与 Registry facade 中对称实现；其文件事务现已由
独立 `PackageProfileRemovalCoordinator` 组合完成：只接受已退休、
代码原路径已消失、明确由包创建的 profile，复用 sharing/symlink/path 防护，并执行 staging、
Registry commit、失败恢复、最终或延迟清理。admin-only 精确候选 HTTP 入口与错误映射也已
接入既有跨进程 operation lock，要求 `confirm_delete=true` 且不返回本地路径。插件管理页顶部现有
始终可发现的“保留数据”入口：独立 admin-only 列表只投影 Registry 中已退休且仍有显式 package
profile ownership 的候选，不暴露本机路径；逐条删除要求长按确认，普通卸载仍只删代码。
last-candidate authority 高层合同也已完成：唯一候选删除后保留 retired tombstone、清空
Selection/runtime intent、保留 StateOwner；同路径同 identity 字节重新出现也不会在新
refresh/restart projection 中复活。完整 dirty diff 验收未发现绕过代码/数据分离合同的生产入口；
隔离前端 UI 与 CLI 短进程验收也已完成，下一步只剩完整 Electron 分发和真实插件进程场景。
不应继续扩张安装 Coordinator、重写安全 installer，或恢复已废弃的
Phase 2 草稿。

## 1. Git 与工作树基线

- 唯一允许写入的 worktree：
  `D:\Users\zheng\Desktop\code\neko-core\N.E.K.O-plugin-lifecycle-rework`
- 分支：`refactor/plugin-identity-selection`
- upstream：`origin/refactor/plugin-identity-selection`
- 本轮实现提交：`36733387 refactor(plugin): complete registry and package management boundaries`
- 本文档使用独立后续提交保存；远端同步状态以交付时的 `git status` / upstream 核验为准
- origin：`https://github.com/CN-Zephyr/N.E.K.O.git`
- upstream remote：`https://github.com/Project-N-E-K-O/N.E.K.O.git`
- 本轮实现提交包含 Phase 2 的 Local import、Market fresh install 与
  Market upgrade/reinstall package deployment
  Coordinator 切片（兼容适配接线和对应单元测试）、对 Registry
  原型的数据合同修正、原子 CAS Store、候选定点读取与只读 shadow gate，以及用户随后
  要求收敛进同一插件中心工作树的 plugin-log-clear 后端、前端、8 locale 与测试；两份
  临时执行/交接文档由后续独立提交保存。准确文件清单以对应提交为准。

`git status` 中以下删除是本轮有意重命名，不是内容丢失：

```text
inventory_codec.py                    -> registry_codec.py
migration.py                          -> registry_migration.py
test_install_source_inventory_v3.py   -> test_plugin_registry_codec.py
```

未经用户明确要求，不 commit、push、创建 PR、改写历史或执行 GitHub 写入。

## 2. 当前有效提交

### `08523040`：候选删除 P1

删除当前 selected candidate 时：

1. Registry 纯计算删除后的候选视图与 fallback；
2. 没有安全 fallback 时在 stop/文件修改前拒绝；
3. 有 fallback 时复用 `switch_plugin_candidate()`；
4. fallback 成功后才删除旧代码；
5. StateOwner 不因删除候选代码而被直接清空；
6. 前端候选操作只保留一个错误提示所有者。

已记录验证：后端聚焦集 `208 passed, 4 skipped`，前端 `14 passed`。
仍需真实进程与 UI 人工场景验证。

### `e41e5b69`：Package Management 边界

当前有效目录：

```text
plugin/server/application/package_management/
  __init__.py
  artifacts.py
  filesystem.py
  install_plan.py
  package_service.py
  replacement.py
```

它抽取了 artifact IO、package inspect/install、文件备份恢复和 replacement
事务，同时保留旧 Route/Service 兼容入口。PackageService 不选择候选、不写
Selection、不启停插件、不拥有 Market transport。

### `c502c2ac` 与 `36733387`：Registry 数据层

`c502c2ac` 提交了尚未接入运行时的数据层原型：

- grouped candidate/selection/state-owner 数据模型；
- 确定性 JSON codec；
- v2 lock + selection/state-owner 的纯迁移；
- 迁移损失报告；
- 22 个测试，包含 Hypothesis round-trip。

`InstallSourceManager` 仍只读写 v2，运行时行为没有切换。

`36733387` 已经补齐原型中可独立验证的数据与写入合同：

- durable model 改名为 `PluginRegistrySnapshot`，避免与 domain scan
  `PluginInventory` 同名；独立 `plugin_registry.json` schema 从 v1 开始；
- `enabled` / `auto_start` 使用 `bool | None` 保存稀疏覆盖，纯迁移接受已经解析的
  `plugin_runtime_overrides.json` 内容，并保留 legacy bool 形式；
- future/legacy schema、坏 revision、坏候选字段、重复候选主键、坏 runtime intent、
  失效 Selection 和未知 StateOwner 均整份失败关闭，不会容错丢字段后自动重选或降写；
- 新增 `JsonPluginRegistry`：复用已有 `portalocker`，每次写入短持有专用文件锁，
  在锁内重读、校验 expected revision、revision +1、tmp + flush/fsync + atomic replace，
  成功后发布 frozen snapshot；共享 writer 已复用 #2958 的 0/50/100/200ms Windows
  `PermissionError` 重试，且临时文件清理失败不会遮蔽原始替换异常；
- 两个独立 Store 的 barrier 并发测试证明 stale writer 冲突并可重读重试，不会丢失
  不同 PluginId 的更新；原子替换失败保留旧文件，future schema 读取不改原字节；
- Registry/Store 聚焦集为 `42 passed`，无真实网络和真实用户状态依赖；包含本切片
  相关 CLI、Coordinator、日志、install-source 与 Market E2E 的扩大回归为
  `297 passed`。

`36733387` 还建立了 Registry authority 读取接线，但没有执行真实用户 cutover：

- `PluginRegistryService` 接受可选 frozen snapshot provider；未注入时保持旧运行时行为，
  但一旦显式配置为 authority，provider 尚未初始化会结构化失败关闭，不再退回旧扫描；
- provider 可用时，全量 refresh、单插件 refresh、候选列表/校验/删除预案均通过 Registry
  的 PluginId 映射只定位声明候选，只读取这些候选的 `plugin.toml`，不再 glob/parse
  其他插件目录；
- Registry selection/state-owner 被投影为现有 resolver 输入，返回 payload 与旧路径逐字段
  等价；候选目录缺失、越界、根映射不唯一或 manifest PluginId 漂移时返回
  `PLUGIN_REGISTRY_STALE` 503，不得从目录扫描复活候选；
- 单元测试强制禁用全量 scanner，覆盖全量/单插件 refresh、候选查询、validate、删除预案、
  provider 未初始化和 stale snapshot fail-closed。Registry service 聚焦测试为 `21 passed`；
- 本机合成 102 个候选、目标 PluginId 有 2 个候选、各执行 100 次的扫描层基准中，旧全量
  扫描 mean/p50/p95 为 `39.858/38.562/48.611 ms`，定点读取为
  `1.379/1.355/1.494 ms`，mean 降低 `96.5%`（约 `28.9x`）。这是开发机临时目录
  微基准，不是端到端 API SLA，也不代表默认生产实例已经获得该收益。
- 新增纯函数 `registry_shadow` cutover gate：比较 legacy 内存迁移结果与候选 Registry 的
  candidates、selection、runtime intent、state owner，只返回缺失/多余插件数与各类别
  mismatch 数。诊断对象不包含 PluginId、目录、package URL、市场身份或授权 receipt；
  Registry codec/store/shadow 聚焦回归为 `45 passed`；
- 新增只读 `registry_preflight`：只接受已 reconcile 的 v2 `LockFile` 快照，读取显式路径的
  selection/state-owner 与 runtime override sidecar，不使用全局缓存或真实配置路径；既存但
  无效的 sidecar、非 v2 lock、任何迁移损失或 shadow mismatch 均整份阻断；
- preflight 在内存中对候选 Registry 执行确定性 serialize + 严格 parse，错误只暴露 authority、
  mismatch 类别和计数；迁移纯函数不再把 PluginId 或候选目录写入日志；全部文件测试只使用
  `tmp_path`，并断言不会生成 `plugin_registry.json`；
- 新增 `registry_cutover` 初始化事务：调用方必须提供现有跨进程 operation lock，
  并在锁内调用 snapshot provider；磁盘 `plugins.lock.json` 必须与该 snapshot 的 canonical v2
  序列化逐字节一致，否则按 stale snapshot 阻断；两个 sidecar 只捕获一次，迁移和备份使用
  同一份字节；
- 路径对象强制四个 authority 使用准确文件名但允许 legacy fallback 与 runtime config 位于
  不同目录；三份旧 authority 原字节复制到固定 backup directory，manifest 只记录 present、
  SHA-256 和备份文件名。既存备份必须逐字节一致，部分备份失败可重试且不会创建 Registry；
- Registry 使用 fsync + atomic replace 初始化；重复启动、两个并发 initializer、备份完成后
  崩溃、Registry 创建后立即崩溃均可幂等恢复。已存在 Registry 必须严格读取并通过 shadow
  gate；损坏或 future Registry 进入结构化 read-only degrade，不回退旧 authority、不改原字节；
- 初始化成功后原子写入 commit marker，绑定 backup manifest 与初始 Registry 快照。marker
  存在时，后续启动验证备份链、初始血缘和当前 Registry 后直接恢复，不再读取已退役 legacy；
  因此 Registry 合法推进 revision 后不会被旧快照误判为 mismatch；
- Registry codec/store/shadow/preflight/cutover 聚焦回归为 `75 passed`。

`36733387` 还完成了生产路径与兼容发布，但仍没有执行真实用户 cutover：

- 新增 `PluginRegistryRuntime` 工厂，只通过既有 `InstallSourceManager.lock_path` 和
  ConfigManager 的路径 API 组装四个显式 authority；两个 legacy sidecar 使用
  `get_config_path()` 保留现有 project fallback 读取语义，新 `plugin_registry.json` 只使用
  `get_runtime_config_path()`，工厂本身不读取文件内容；
- runtime 初始化只有在 backup、shadow gate、Registry 原子创建和 commit marker 全部成功后
  才能提供 snapshot；初始化失败不发布半成品；全部路径测试使用 `tmp_path` 和 fake config；
- 新增进程内 authority 发布桥。模块导入期已经创建的 Route、Lifecycle 与 Registry service
  可在未来 startup 发布后动态看到同一 frozen snapshot；未发布时继续保持现有 legacy 行为，
  旧 shutdown 也不能误清后来发布的新 authority；
- `plugin_selections` 和 `runtime_overrides` 的既有读取/写入 API 在 authority 发布后转发
  `JsonPluginRegistry`，不再写两个 legacy sidecar。Selection 清除保留 StateOwner；runtime
  intent 迁移只清旧 PluginId 的 intent，不删除其候选来源；CAS 冲突会重读最新 revision 后
  重试，no-op 不增加 revision；
- 新增 `RegistryInstallSourceFacade`，覆盖现有生产调用实际使用的全部 manager 表面：roots、
  degraded 状态、snapshot/list/API projection、directory/profile ownership、active Market
  lookup、import/Market install/upgrade、rollback restore 和 soft remove。所有写入只更新
  Registry candidate/provenance；legacy lock 字节保持不变；
- facade 的 soft remove 在同一次 CAS 写入中把 matching Selection 清空、保留 StateOwner，
  因而不会生成“已删除候选仍被选中”的非法 Registry；仅剩 retired candidate 或仅有 runtime
  intent 的 PluginEntry 被视为空 inventory，而不是 stale Registry；
- facade 发现候选目录被另一个 PluginId 占用时 fail-closed，不会把 provenance/state receipt
  跨 logical identity 搬运；Registry 后续损坏时 facade 进入 `registry_read_failed`，不得回读
  legacy lock；
- 路径工厂/发布桥聚焦测试 `6 passed`；Selection、runtime intent、Registry Store 与
  Registry query 兼容组合 `56 passed`；facade 契约当前 `5 passed`，并补有 barrier/CAS 自动
  重读合并测试。这些测试没有访问真实用户配置。
- legacy sidecar 无效 JSON/内容会在 operation lock 内把同一捕获点的三份原字节复制到固定
  failure-backup，并写入只含 authority/reason/hash 的 manifest；源文件不改、Registry 不创建，
  重复失败幂等，修复后可继续 cutover，变化后的坏内容不能覆盖首次留证；
- `initialize_plugin_registry_startup` 现在明确返回 `registry` / `legacy` / `blocked`：首次 cutover
  前 reconcile/preflight 失败可保留 legacy；Registry 或 commit marker 一旦存在，任何恢复失败
  都发布 blocked provider、清空 install-source manager，禁止所有旧写 API 回退；
- `http_app` 已把顺序改为 `reconcile → cutover/recovery → authority+facade publish → runtime
  discovery/autostart`，并在正常退出或 lifecycle startup 失败时撤销进程内 authority；重启测试
  证明 Registry revision 已推进后不再咨询或重新激活 legacy；
- 已发布 provider 不再返回进程内缓存，而是每次重读共享 Registry 文件，因此另一个进程推进
  revision 后当前服务立即可见；读取损坏返回 `PLUGIN_REGISTRY_UNAVAILABLE` 503，禁止 fallback；
- persistence startup 在 authority 选择本身发生未预期异常时也发布 blocked 状态，而不是清空
  authority 后恢复 scanner；
- authority 高层集成测试覆盖 selected-running Market 删除：生命周期事务先 stop Market、应用并
  start builtin、提交 Selection/StateOwner，再删除/retire Market；旧 lock 字节和 runtime intent
  保持不变；
- authority 高层集成测试覆盖目标启动失败：恢复旧 metadata、重启旧 builtin，Registry revision、
  Selection、StateOwner、runtime intent 均不变，旧 selection sidecar 保持空；
- 本轮加入 startup/facade/损坏、lifecycle authority、candidate removal 事务和 profile cleanup
  边界抽取后的完整
  `plugin/tests/unit/server` 为 `645 passed, 6 skipped`；install-source + Market integration
  为 `107 passed`。

不得宣称真实 cutover 已验证。初始化事务已经把 operation lock、stale snapshot、备份、
原子创建、shadow gate、legacy 损坏留证、Registry 损坏降级、生产路径工厂、兼容转发、
install-source facade、启动顺序和关键 lifecycle authority 合同串成生产代码路径。当前最重要
的候选代码退役风险已由独立事务收口：原目录先原子移动到根内隐藏 `.delete-backups`，Registry
retire 失败则恢复；成功后才清理隐藏副本。fallback 已启动但退役失败时，Lifecycle 会切回旧
候选并恢复删除前的 Selection/StateOwner receipt。清理遇到占用时保留 commit marker，启动只
删除 marker 字节完全匹配的已提交副本；无 marker 的中断暂存绝不自动删除。剩余结构风险是
profile 与 code retirement 已由一个注入式事务统一收口，Lifecycle 不再直接执行两者的文件
暂存/恢复/清理；旧私有 wrappers 已删除，concrete service 构造也已移到 infrastructure。
默认 `delete_plugin` 已不再触碰 package profile，并返回 `user_data_preserved=true`；剩余产品/
架构负担是尚未形成 package profile 的前端用户确认入口。Registry
`profile_installed` 状态提交基础已经完成：按精确候选清空 `profile_dir`、设置 false，保留
package/source/audit/Selection/StateOwner，legacy 写失败恢复内存 snapshot，Registry no-op 不推进
revision。文件协调事务也已完成但没有路由：它要求 Registry row 已退休、候选原代码目录不存在、
profile ownership 明确为 true，无法证明 ownership、共享 profile、symlink/unsafe path 都
fail-closed；commit 前失败恢复原 profile，commit 后清理失败记录延迟清理。因此当前生产路径仍
不会自行删除任何用户文件；只有管理员显式调用新端点、给出精确候选并确认才会进入事务。另一个风险
是 Registry commit 成功到 marker 落盘
之间发生进程崩溃时会安全保留一个无法自动判定的隐藏副本（空间泄漏，不会复活或丢代码）。
真实运行时人工验证未获授权且本轮没有执行。

### `36733387`：Phase 2 Coordinator（自动化退出门已完成）

已用当前真实 API 重建最小 `InstallationCoordinator`：

- 接管 `PluginCliService.upload_and_install()` 的本地 content/package-path 分支；
- 接管已完成下载和外部校验后的 Market fresh install，保持 transport 的 OAuth、
  下载、外部 SHA、进度、取消和上报职责不变；
- 接管 Market upgrade/reinstall 在 replacement transaction 内部执行的包保存、
  重算 SHA、staging deploy、身份硬校验、来源更新和失败清理；
- 保持既有公开 DTO、错误文本、staging 安装和来源登记行为；
- 将保存、重算 SHA、安装、身份核验、来源登记、候选激活和失败清理的顺序集中到
  注入窄 Port 的事务；
- 接管外层 Market replacement 的 operation-lock 范围、stale snapshot 复核、
  stop/backup/deploy/validate/restart 调用和失败后的旧来源恢复顺序；
- concrete replacement adapter 已移动到
  `plugin/server/infrastructure/package_management/market_replacement.py`，Route 不再直接
  导入 package filesystem/replacement、operation lock 或 runtime stop/start；
- recorded profile 路径解析、symlink ancestor 拒绝和 package-id 末级目录校验也已移入
  replacement adapter，并在 Coordinator 持有 operation lock、重新验证 stale snapshot 后执行；
- Market bridge 只保留下载、外部校验、默认 profile root 输入、任务进度/rollback 观察与稳定错误码
  翻译，并直接向 Service 发送 typed fresh/replacement request，不再使用
  `install_source_override` 跨层字典；Service 中该参数仅作旧调用方兼容入口；
- 现有 fake-port 测试覆盖本地、fresh Market、Market replacement deploy
  与外层事务的成功、stale snapshot、来源降级/恢复、候选待选、身份硬失败、
  不安全 recorded profile、登记/激活失败和 rollback，并通过既有 Route、safe-upgrade
  与完整 Market E2E。
- Coordinator 已直接拥有 installer result 的规范化：支持当前 `unpacked_plugins` 与 legacy
  `installed_plugins`，统一收集新建插件/profile 目录，并执行 Market 单插件约束；这些纯规则
  不再出现在 Port。`PluginCliService` 的同名私有 helper 仅保留兼容转发。
- concrete `PluginCliInstallationAdapter` 已迁到
  `application/package_management/plugin_cli_adapter.py`；安装失败时的候选目录、刚创建的
  package profile 和受管 archive 清理也由该 adapter 执行。Service 仍提供来源 receipt
  rollback 和 Lifecycle 激活窄回调，因此没有把 Registry 或运行时职责塞进文件清理模块。
- 已确认的通用本地 upgrade/downgrade/reinstall replacement 现由 Coordinator 组合 deploy、
  manifestless backup 验证和安装后身份验证；adapter 继续调用未改写的 #2958
  `replace_plugin()`，其中 stop/backup/preserve/restart/rollback 顺序保持不变。Service 不再
  直接导入 package replacement/filesystem 或 `upgrade_support`。
- 最终调用图审计已移除 `plugins/upgrade_support.py` 的 package filesystem/replacement
  compatibility exports；该模块只保留 runtime probe/stop/start。旧测试已改为直接导入
  `package_management.filesystem`/`replacement`，并新增“不再反向导出包写操作”的边界合同。
- 审计后，Market Route 剩余磁盘写入只处理 transport 下载、OAuth/任务状态；
  `routes/plugin_install.py` 仍是插件内部资源安装，明确不纳入包管理。Plugin CLI 剩余
  `PluginPackageService`/`PackageArtifactStore` 调用属于其公开 facade，不与 runtime 写入混合。

Phase 2 自动化退出门已完成并进入 `36733387`。

### `36733387`：Plugin log clear 收敛

用户明确要求排除最早的 `refactor/plugin-installation-lifecycle` 大重构，并把其余
插件中心相关工作收敛到本 worktree。因此 `feature/plugin-log-clear` 的未提交实现已
按目标分支真实上下文移植进来，源 worktree 保持只读：

- 只允许清空具体插件的日志，服务器总日志拒绝该操作；
- 使用精确文件名匹配隔离带下划线的相似 plugin id；
- Windows 下截断并 fsync 活动日志文件，不删除仍被 logger 持有的路径；
- 重置已有 tail watcher 的 offset；
- 前端拥有确认框与唯一错误提示，服务器日志页面不显示清空按钮；
- 8 个 locale 同步新增 5 个 key。

移植时修正了原测试对 Windows `write_text()` 换行转换的错误假设。该功能同样尚未
commit 或 push。

## 3. 已废弃的 Claude Phase 2 试验

以下旧的未跟踪草稿实现已从工作树移除，不得从 Claude 对话记录恢复后继续补丁式
开发：

```text
package_management/adapters.py
package_management/coordinator.py  # 指 Claude 的旧文件内容
package_management/coordinator_models.py
package_management/ports.py
```

原因：它们没有 Route/Service 入口和测试，只实现了半个 `install()`，rollback
固定为 `not_attempted`，并引用多个从未存在的 concrete API。错误接口不是被历史
重构删除的，而是草稿把理想 Port 名称误当成现有实现。

同一路径的 `package_management/coordinator.py` 后来已依据当前 concrete API 从零重建；
它是上一节所述、带测试并已接入 Local import、Market fresh install 与 replacement
package deployment 的新实现，
不属于应删除的旧草稿。

同时移除了五份重复或互相矛盾的临时说明：Phase 2 完成度误报、Schema V4
长期双写方案和 `plugins.lock.json` 原地升级方案均不再作为事实来源。

## 4. 更新后的架构决定

### 4.1 Coordinator 先于持久化切换

先用当前真实 API 建立薄 Coordinator，让 Local import 与 Market 的文件事务、
来源登记、候选激活和 rollback 顺序收敛到一个入口。旧持久化可通过临时适配器
工作，但不得把临时适配器描述成最终 Registry。

### 4.2 独立 canonical Registry

最终使用独立 `plugin_registry.json`，自身 schema 从 1 开始。迁移输入至少包括：

1. `plugins.lock.json` v1/v2：候选与 provenance；
2. `plugin_candidate_selections.json` v1/v2/v3：Selection 与 StateOwner；
3. `plugin_runtime_overrides.json`：稀疏 `enabled`/`auto_start` intent。

不得把 `plugins.lock.json` 原地升级：当前旧 reader 会 best-effort 读取 future
schema，而旧 writer 永远写回 v2，存在降写丢字段风险。

不得长期双写：可以在测试或 cutover 前做内存 shadow comparison，但切换成功后
只有 `plugin_registry.json` 是写入真相。旧文件保留只读备份；旧公开 API 改为转发
新 Store，而不是继续写旧 JSON。

### 4.3 写入合同

每次 Registry mutation 必须：

1. 持有短跨进程 Registry 文件锁；
2. 从磁盘重新读取并严格验证 snapshot；
3. 校验 expected revision；
4. 应用确定性 mutation；
5. tmp + flush/fsync + atomic replace；
6. 发布新的 frozen in-process snapshot；
7. 释放文件锁。

Registry 文件锁不得包住下载、解压、插件 import、start/stop。长事务由现有插件
operation lock/未来按 PluginId 收窄的事务锁保护。

## 5. 不可突破的范围

- 保留 PR #2958 的归档限制、staging、跨进程操作锁、profile 防护、替换事务、
  rollback 和结构化错误合同。
- 保留 identity-selection 的逻辑 PluginId、精确 CandidateKey、StateOwner、
  shared-state 授权和 Market release-chain 规则。
- Registry/Resolver 只做 durable desired state 与纯计算；运行实例只由
  RuntimeLifecycle/Coordinator 改变。
- 删除候选代码与删除用户数据仍是两个独立操作。
- Market transport 继续拥有 OAuth、下载、外部 SHA、进度、取消和上报。
- 不引入新生产依赖、数据库、长期服务或后台轮询。
- 不修改包格式、开发者 CLI、插件业务代码或真实用户运行数据。
- 不做不可变 digest store、AuthorityId 或 state 物理布局迁移。

## 6. Phase 2 Coordinator 执行状态

按可独立验证的垂直切片推进：

1. 已定义与当前 concrete API 对齐的窄 Port 和 typed result；
2. 已迁移 Local fresh import，保持 API DTO 和错误码不变；
3. 已迁移 Market fresh install，下载阶段仍在 mutation lock 之外；
4. 已迁移 upgrade/reinstall 的 package deployment，并复用现有 replacement
   transaction；
5. 已把 bridge 中外层 replacement 编排收进 Coordinator，并保持下载在锁外、
   snapshot 复核和 replace 在同一 operation lock 内；
6. 已让 Market bridge 直接使用 typed request，兼容 facade 不再是 Market 运行时依赖；
7. 已把 concrete package replacement、profile 保护、operation lock 和 runtime
   stop/start 适配移到 infrastructure，Route 不再同时拼装 package filesystem 与 Lifecycle；
8. 后续在 Registry cutover 具备安全条件后收口 candidate switch/remove；
9. 每迁移一个入口就删除对应的重复事务拼装，不保留长期双路径。

Coordinator 必须拥有的顺序：

```text
inspect/verify
  -> plan
  -> stage
  -> revalidate desired-state snapshot
  -> stop when required
  -> promote/replace
  -> validate exact candidate
  -> commit provenance/selection
  -> start according to prior runtime intent
  -> return result / rollback on failure
```

Phase 2 已完成 Local import、Market fresh install 与 Market replacement 的安装/替换
主路径及 Route 硬边界收口，自动化退出门已通过；
Registry 数据模型、并发写入、三文件 preflight、备份/原子初始化与恢复合同已经在未接线
切片中完成；生产路径工厂、三个 legacy API 面的单写门面、损坏留证和 startup authority
switch 也已完成。仍不得把临时目录中的自动化测试描述成真实用户配置迁移或真实插件进程
验证完成。

## 7. 最后验证现场

Phase 2 收口后在唯一指定 worktree 重新执行的验证：

- Coordinator + Market safe-upgrade 聚焦集：`38 passed`；
- Phase 2 + PR #2958 安全合同：`168 passed, 6 skipped`；
- 完整 `plugin/tests/unit/server`：`593 passed, 6 skipped`；
- `test_install_source_end_to_end.py` + `test_market_bridge_e2e.py`：`107 passed`；
- 所有 Phase 2 Python 文件 Ruff：通过；
- `frontend/plugin-manager` type-check：通过；
- i18n：8 locale、702 keys、无 placeholder mismatch；
- `git diff --check`：通过。

Phase 3 当前前置切片验证：

- atomic writer + cutover initialization：`28 passed`；
- Registry/codec/store/shadow/preflight/cutover/Lifecycle 组合：`205 passed`；
- 完整 `plugin/tests/unit/server` 首次运行遇到一项既有 staging rename 的 Windows
  `WinError 5`；原样单测重跑通过，随后完整重跑为 `597 passed, 6 skipped`；
- 本轮生产路径工厂、authority 发布桥、legacy API forwarding 与 Registry query 聚焦集：
  `71 passed`；
- 加入上述新测试后的完整 `plugin/tests/unit/server`：`606 passed, 6 skipped`；
- Phase 3 相关 Ruff：通过。

最新 authority 高层切片验证（2026-08-28）：

- Lifecycle + facade + authority + RegistryService + startup 聚焦集：`121 passed`；
- 完整 `plugin/tests/unit/server`：`633 passed, 6 skipped`；
- `test_install_source_end_to_end.py` + `test_market_bridge_e2e.py`：`107 passed`；
- 新增/修改 Python 文件 Ruff：通过；
- warning 仍为既存 deprecation/未注册 mark，不是测试失败。

Candidate removal 事务切片验证（2026-08-28）：

- removal coordinator + Lifecycle + Registry authority + server startup 聚焦集：`89 passed`；
- 完整 `plugin/tests/unit/server`：`639 passed, 6 skipped`；
- `test_install_source_end_to_end.py` + `test_market_bridge_e2e.py`：`107 passed`；
- 相关 Python Ruff：通过。

Package profile cleanup 边界切片验证（2026-08-28）：

- profile/deferred cleanup 聚焦集：`19 passed`；
- removal coordinator + Lifecycle + Registry authority + server startup 聚焦集：`89 passed`；
- 完整 `plugin/tests/unit/server`：`639 passed, 6 skipped`；
- `test_install_source_end_to_end.py` + `test_market_bridge_e2e.py`：`107 passed`；
- 相关 Python Ruff 与 Ruff format check：通过。

Candidate + profile 组合删除事务切片验证（2026-08-28）：

- `CandidateRemovalCoordinator` 成功/双资源恢复/incomplete/deferred 合同：`11 passed`；
- removal coordinator + Lifecycle + Registry authority + server startup 聚焦集：`95 passed`；
- 完整 `plugin/tests/unit/server`：`645 passed, 6 skipped`；
- `test_install_source_end_to_end.py` + `test_market_bridge_e2e.py`：`107 passed`；
- 相关 Python Ruff 与 Ruff format check：通过。

Lifecycle compatibility removal 切片验证（2026-08-28）：

- profile cleanup 边界 + removal composition + Lifecycle/authority 聚焦集：`95 passed`；
- 完整 `plugin/tests/unit/server`：`645 passed, 6 skipped`；
- `test_install_source_end_to_end.py` + `test_market_bridge_e2e.py`：`107 passed`；
- 相关 Python Ruff 与 Ruff format check：通过。

Candidate-code-only deletion 切片验证（2026-08-28）：

- Lifecycle + authority + removal coordinator 聚焦集：`73 passed`；
- 完整 `plugin/tests/unit/server`：`645 passed, 6 skipped`；
- `test_install_source_end_to_end.py` + `test_market_bridge_e2e.py`：`107 passed`；
- frontend type-check：通过；
- i18n：8 locale、702 keys、无 placeholder mismatch；
- 相关 Python Ruff：通过。

Profile-removal Registry commit 基础切片验证（2026-08-28）：

- Registry facade + legacy install-source focused：`21 passed`；
- 完整 `plugin/tests/unit/server`：`646 passed, 6 skipped`；
- install-source property：`20 passed`；
- `test_install_source_end_to_end.py` + `test_market_bridge_e2e.py`：`109 passed`；
- 覆盖已软删除候选、审计保留、Registry revision/no-op、legacy 原文件隔离、legacy 写失败
  snapshot 恢复；
- 相关 Python Ruff 与 `git diff --check`：通过；新 Registry facade/单测文件 Ruff format
  check 通过，两个已有 tracked 文件保持原排版，未为本切片格式化旧改动。

Explicit package-profile removal transaction 切片验证（2026-08-28）：

- profile policy + explicit removal + candidate removal + Registry facade focused：`49 passed`；
- 完整 `plugin/tests/unit/server`：`658 passed, 6 skipped`；
- install-source property：`20 passed`；
- `test_install_source_end_to_end.py` + `test_market_bridge_e2e.py`：`109 passed`；
- 覆盖 retired/code-absent/explicit-owner 门禁、active/manual/unknown/shared 拒绝、成功提交、
  Registry commit 失败完整/不完整恢复、commit 后 deferred cleanup，以及真实 Registry facade
  revision/legacy 原字节隔离；
- 新事务与测试 Ruff format check、相关 Ruff、`git diff --check`：通过。

Package-profile removal API/operation-lock 切片验证（2026-08-28）：

- coordinator + runtime adapter + Registry facade + route focused：`34 passed`；
- 完整 `plugin/tests/unit/server`：`672 passed, 6 skipped`；
- install-source property：`20 passed`；
- `test_install_source_end_to_end.py` + `test_market_bridge_e2e.py`：`109 passed`；
- 覆盖显式确认、user-only exact candidate、PluginId mismatch、代码仍存在、持久化不可用、
  404/409/500/503 稳定映射、rollback details、跨进程 operation lock 持有与响应路径脱敏；
- 新 adapter/测试 Ruff format check、相关 Ruff 与 `git diff --check`：通过。

Last-candidate Registry authority 切片验证（2026-08-28）：

- `test_registry_lifecycle_authority_integration.py`：`3 passed`；
- 完整 `plugin/tests/unit/server`：`673 passed, 6 skipped`；
- install-source property：`20 passed`；
- `test_install_source_end_to_end.py` + `test_market_bridge_e2e.py`：`109 passed`；
- 覆盖唯一候选 Lifecycle 删除、retired tombstone、Selection/runtime intent 清空、StateOwner
  保留、legacy lock 原字节、同路径字节重新出现、fresh Registry projection 零候选、禁止 full
  scanner/metadata resurrection；
- 相关 Ruff 与 `git diff --check`：通过；已有未跟踪测试文件保持原排版，未为本切片格式化旧内容。

Retained package-profile frontend entry 切片验证（2026-08-28）：

- runtime projection + route focused：`18 passed`；
- frontend `plugins.ts` API contract：`8 passed`；
- frontend type-check：通过；
- i18n：8 locale、721 keys、无 placeholder mismatch；
- 相关 Python Ruff 与 `git diff --check`：通过；
- 新增 Vue 组件单独 ESLint：通过。既有 `plugins.ts`/`PluginList.vue` 的全文件 ESLint 仍报告
  15 个任务前已有的 `no-explicit-any`，新增行没有这类错误，未扩大范围处理旧 lint 债务。

该入口接入后的最终组合回归（2026-08-28）：

- 完整 `plugin/tests/unit/server`：`676 passed, 6 skipped`；
- install-source property + install-source/Market integration：`118 passed`；
- frontend production build：通过；
- frontend unit（排除 Playwright e2e）：除既有 `src/utils/i18nLabel.test.ts` 的 1 条 locale
  fallback 断言外为 `284 passed`；进一步排除该未修改基线文件后为 `281 passed`；
- 第一次并行启动两个 pytest 进程时，两者争用项目固定的 `../.pytest-run`，分别出现目录创建冲突
  与包文件被另一进程清理；使用 worktree 内独立 basetemp 串行重跑后上述两组全部通过。这是验证
  命令隔离问题，不是生产失败；后续不要并发运行共享同一 pytest basetemp 的套件。

Market recorded-profile 边界收口后的增量验证（2026-08-28）：

- Coordinator + Market safe-upgrade 聚焦集：`39 passed`；
- install-source property + install-source/Market integration：`118 passed`；
- 完整 `plugin/tests/unit/server`：`677 passed, 6 skipped`；
- 相关 Python Ruff 与 `git diff --check`：通过；
- 未改变用户可见错误码或安装行为，只把路径决策移入 package-management adapter，
  并把校验时机收进 stale snapshot 复核后的 operation-lock 临界区。

Plugin CLI install-result 边界收口后的增量验证（2026-08-28）：

- Coordinator + Plugin CLI 聚焦集：`92 passed`；
- install-source property + install-source/Market integration：`118 passed`；
- 完整 `plugin/tests/unit/server`：`679 passed, 6 skipped`；
- 相关 Python Ruff 与 `git diff --check`：通过；
- 聚焦集首次运行有一条未修改 replacement 测试在 Windows rename 上遇到临时
  `WinError 5`；独立重跑及整组新 basetemp 重跑均通过，未发现代码回归。

Plugin CLI deployment adapter/失败清理边界收口后的增量验证（2026-08-28）：

- adapter + Coordinator 新边界聚焦集：`25 passed`；
- adapter + Coordinator + Plugin CLI 合同：`93 passed`；
- install-source property + install-source/Market integration：`118 passed`；
- 完整 `plugin/tests/unit/server`：`680 passed, 6 skipped`；
- 相关 Python Ruff 与 `git diff --check`：通过；
- 扩大聚焦集首次运行再次在同一条未修改的 Windows rename 测试遇到临时 `WinError 5`；
  独立重跑及全组新 basetemp 重跑均通过。

Local replacement Coordinator 收口后的增量验证（2026-08-28）：

- Coordinator + adapter + #2958 safe-upgrade 聚焦集：`49 passed`；
- Coordinator + adapter + Plugin CLI 完整合同：`95 passed`；
- install-source property + install-source/Market integration：`118 passed`；
- 完整 `plugin/tests/unit/server`：`682 passed, 6 skipped`；
- 相关 Python Ruff 与 `git diff --check`：通过。

最终 mixed-responsibility 调用图审计后的验证（2026-08-28）：

- package replacement + Lifecycle adapter + Market safe-upgrade：`57 passed, 4 skipped`；
- install-source property + install-source/Market integration：`118 passed`；
- 完整 `plugin/tests/unit/server`：`688 passed, 6 skipped`；
- 相关 Python Ruff 与 `git diff --check`：通过；
- 自动化包管理边界收口完成；
- production frontend build 通过；短暂启动静态预览并使用系统 Edge 拦截全部 API、注入假数据，
  已人工确认保留数据入口、可删除/代码仍存在禁用态、路径脱敏、独立长按确认、精确
  `confirm_delete=true` DELETE 请求和删除后列表刷新，浏览器无 page/console error；测试后预览
  进程已停止且端口已释放；
- 隔离的 package-format/CLI 真实子进程工作流：`5 passed, 1 warning`；覆盖临时目录中的
  build/inspect/bundle install，并确认开发者 CLI 不能绕过 Plugin Center 执行运行时安装；warning
  是既存 `PLUGIN_CONFIG_ROOT` deprecation；
- 未执行真实 Market 网络、真实插件进程或完整 Electron 分发场景。

此前包含 Registry、Coordinator、日志与 integration 的扩大回归命令：

```powershell
uv run pytest `
  plugin/tests/unit/server/test_registry_cutover_initialization.py `
  plugin/tests/unit/server/test_registry_cutover_preflight.py `
  plugin/tests/unit/server/test_plugin_registry_codec.py `
  plugin/tests/unit/server/test_json_plugin_registry.py `
  plugin/tests/unit/server/test_registry_shadow.py `
  plugin/tests/unit/server/test_runtime_overrides.py `
  plugin/tests/unit/server/test_plugin_candidate_selection.py `
  plugin/tests/unit/server/test_plugin_registry_service.py `
  plugin/tests/unit/server/test_installation_coordinator.py `
  plugin/tests/unit/server/test_plugin_cli_selection.py `
  plugin/tests/unit/server/test_plugin_cli_route.py `
  plugin/tests/unit/server/test_plugin_cli_safe_upgrade.py `
  plugin/tests/unit/server/test_market_bridge_safe_upgrade.py `
  plugin/tests/unit/server/test_logs_latest_file_filter.py `
  plugin/tests/integration/test_install_source_end_to_end.py `
  plugin/tests/integration/test_market_bridge_e2e.py -q
```

当时结果：`337 passed, 1 warning in 21.27s`。warning 是既存的
`plugin.settings.PLUGIN_CONFIG_ROOT` deprecation，不是本轮新增失败。

此前其余验证：

- 所有当前变更/未跟踪 Python 文件执行 Ruff：通过；
- `frontend/plugin-manager`: `npm.cmd run type-check`：通过；
- `npm.cmd run check:i18n`：8 locale、702 keys、无 placeholder mismatch；
- `npx.cmd vitest run src/api/logs.test.ts`：`1 passed`；
- `git diff --check`：通过。

未运行：真实 Market 网络、真实插件子进程 replacement/rollback、Electron/web 人工 UI、
生产配置目录 migration/cutover。不得把上述自动化结果描述成这些场景已验证。

## 8. 下一步执行顺序

Phase 2 已关闭。按以下顺序进入 Phase 3，避免提前切换 authority：

1. 已完成：canonical Registry writer 复用 #2958 的 Windows `os.replace` retry，并保留
   原始异常；Registry 自身损坏 read-only degrade 与 legacy authority 损坏留证均有测试；
2. 已完成：显式 Registry provider 的缺失、stale candidate、manifest 身份漂移均
   fail-closed，全量/单插件 refresh 与候选 validate 不再退回 scanner/legacy selection；
3. 已完成并接 startup：只读 preflight 接受已 reconcile 的 v2 `LockFile`，严格读取
   selection/state-owner 与 runtime overrides，生成并校验内存 Registry snapshot；
4. 已完成并接 startup：显式路径、固定备份 manifest、初始 Registry/commit marker、原子创建、
   失败恢复和幂等重启合同；
5. 已覆盖 sidecar 缺失/损坏留证、Registry 已存在/损坏、partial backup、stale snapshot、
   创建后崩溃、并发重复启动与生产 blocked/legacy/registry 三态；
6. `registry_shadow` 已接入初始化前后硬门禁；mismatch 不得写入或发布 authority；
7. 已完成并接 startup：生产路径工厂已核对 ConfigManager legacy fallback 与 runtime config
   路径；Selection/StateOwner/runtime intent 旧 API 与 install-source manager 实际调用面均
   已提供 Registry 单写兼容门面，不做长期双写；
8. 已完成：reconcile/cutover/Registry 发布早于 runtime discovery/autostart；退出和 startup
   失败均撤销 authority；
9. 已完成关键 authority 高层合同：selected-running fallback 删除成功路径、目标启动失败
   runtime/selection rollback、跨实例 commit 可见性和读损坏 fail-closed；
10. 已完成：整包目录暂存、Registry retire、最终清理、失败恢复与 marker 驱动的启动重试已
    收口为 `CandidateRemovalCoordinator`；删除失败会恢复 fallback 前的 runtime/receipt；
11. 已完成边界抽取：profile ownership/sharing、safe staging、恢复、最终删除和 deferred cleanup
    record 已迁入 `package_management/profile_cleanup.py`；Lifecycle 暂保留兼容 wrapper；
12. 已完成：profile 与 candidate code retirement 已合并为一个注入式删除事务；Registry retire
    前失败恢复两类资源，commit 后清理失败进入独立 deferred 状态；
13. 已完成：迁移 profile 单测到 package-management 边界，删除 Lifecycle 私有兼容 wrapper，
    并把 concrete removal service 构造移到 infrastructure；
14. 已完成：默认删除已改为仅退休候选代码并保留用户数据；精确候选的 Registry/legacy
    `mark_profile_removed()` 与独立 profile 数据删除协调事务（stage → commit → cleanup，失败
    恢复）已完成；admin-only 窄 HTTP API/错误映射也已接入 serialized plugin-operation lock。
    last-candidate authority 也已证明不会从 scanner/legacy 复活；
15. 已完成：插件管理页新增独立“保留数据”入口、Registry retained-profile 安全投影和逐条长按
    删除；入口不依赖仍存在的插件卡片，响应不含本机路径，普通卸载与数据删除仍为两个操作；
16. 已完成完整服务端与 install-source/Market 扩大回归；
17. 已完成 `PluginCliService` installer-result 解析规则抽取：Coordinator 直接解析结果，
    Port 不再重复解释，旧私有方法保留薄兼容转发；
18. 已完成：具体 Plugin CLI installation adapter 与安装失败后的候选目录/profile/archive
    清理已迁入 package-management；Service 仅保留来源 receipt 回退与 Lifecycle 激活回调；
19. 已完成：`PluginCliService.install()` 的通用本地 upgrade/downgrade/reinstall
    replacement 编排已接入 Coordinator/adapter，并原样复用 #2958 的 staging、backup、
    profile preserve、rollback 和 Lifecycle 行为；
20. 已完成最终包管理调用图审计：Lifecycle adapter 不再转发 package filesystem，Route、
    Service、Lifecycle 没有发现新的 package filesystem + Registry + runtime 三权直接混写；
21. 已完成架构冻结和完整 dirty diff 验收；未发现需要继续扩大抽象或改动生产代码的问题。
    最新前端复核为 type-check 通过、8 locale `721` keys 一致、聚焦 API `9 passed`；之后只能在
    不读取真实用户数据、不连接真实 Market、且不启动长期服务的隔离临时环境中执行人工
    UI/短进程验证。

第一步实现期间也不要触碰真实用户配置目录；全部 migration/cutover 测试使用临时目录和
fake ports。若方案需要新的生产依赖、长期后台任务或明显额外启动成本，先征求用户同意。

## 9. 通用验证要求

- Python 命令从仓库根目录通过 `uv run` 执行；
- Coordinator 单元测试使用 fake ports，覆盖成功、stale snapshot、登记失败、
  start 失败和 rollback；
- 迁移每个实际入口后运行原有 Route、PackageService、safe-upgrade 与 Market 测试；
- 并发/取消测试使用 event/barrier，不使用任意 sleep；
- 最终运行 ruff、相关 pytest、前端契约测试和 `git diff --check`；
- 未运行的真实 Market、真实插件进程、Electron 场景必须单独列出。

## 10. 接手动作

开始任何修改前核对：

```powershell
git rev-parse --show-toplevel
git branch --show-current
git rev-parse HEAD
git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}'
git remote -v
git status --short
git diff --check
```

只在本 worktree 修改和验证；其他 worktree 只读且不得借用运行状态。
