# Plugin package-management refactor working plan

> TEMPORARY WORKING DOCUMENT
>
> This file is the execution contract for the package-management refactor. It is
> not product documentation and must not be published as a user-facing Plugin V2
> specification. When the refactor finishes, replace it with durable architecture
> documentation or remove it after the accepted decisions have been transferred.

## 中文执行摘要

本轮重构的硬范围只有四条主线：

1. 先完成当前 identity-selection 分支的删除候选 P1 与重复错误提示修复；
2. 保留 #2958 的安全实现，只把分散的包文件职责抽进独立模块；
3. 让本地导入与 Market 安装共用一个薄协调事务；
4. 最后把 `plugins.lock.json`、候选 selection/state-owner 与稀疏 runtime
   intent 迁入独立的唯一 Registry，并从 Lifecycle 移除包目录和 package
   profile 删除策略。

本轮明确不做不可变 digest Package Store、AuthorityId、插件 state 物理迁移、
旧插件 clean break、包格式修改、开发者 CLI 重写或插件内部 OCR/Textractor 等
资源安装重构。任何需要跨越这些边界的变化都必须停下并重新确认范围。

阶段安排为：Phase 0 修 P1（2–3 天）；Phase 1 只抽边界不改行为（3–4 天）；
Phase 2 统一 Market/本地协调入口（4–5 天）；Phase 3 合并持久 Registry
（5–7 天）；Phase 4 瘦身 Lifecycle（3–4 天）；Phase 5 全量验证与正式文档
（2–3 天）。单人基础工程量预计 19–26 个工作日；包含评审、Windows 专有问题、
CI 和一次迁移返工缓冲后，现实预期为 22–30 个工作日，约 4.5–6 个自然周。

每个 Phase 应独立审查，通常对应一个 PR。不得把纯文件搬迁、行为修改和持久化
切换混入同一阶段。Phase 3 切换后不允许继续双写两份持久化真相。

当前执行状态（2026-08-28）：Phase 0 与 Phase 1 已分别提交；`c502c2ac`
当时提前交付了尚未接线的 Registry 数据模型/codec/纯迁移试验；该原型现已接入下述
startup authority。Phase 2 已在 `36733387` 完成自动化退出门：Local import、Market fresh install、Market
upgrade/reinstall package deployment 与外层 replacement transaction 已统一进入
`InstallationCoordinator`。Coordinator 保证 stale snapshot 复核和 replace 位于同一
operation lock 内，并在文件回滚后恢复旧来源；concrete package replacement、profile
保护、recorded profile 路径解析与 stop/start 适配已移出 Route，且 recorded profile
会在 operation lock 内完成 stale snapshot 复核后再解析和校验。Market bridge 只保留
transport、默认路径策略输入、任务
观察和错误码翻译，直接发送 typed request，不再依赖 `install_source_override` 跨层字典。
Service 中该参数仅作为旧调用方兼容入口。其他候选变更入口留待 Registry cutover 后收口。
Coordinator 现也统一解析 installer 的 `unpacked_plugins`/legacy `installed_plugins`、新建
profile 目录和单插件 Market 目标；这些规则已从 concrete Port 移除。`PluginCliService`
暂保留原私有解析方法作为薄兼容门面，不再拥有另一份实现。
具体 `PluginCliInstallationAdapter` 及安装失败后的候选目录/profile/archive 清理也已迁入
package-management 边界；Service 只向 adapter 提供来源 receipt 回退、安装调用和 Lifecycle
激活等窄兼容回调，不再直接执行这组文件删除。
通用本地 upgrade/downgrade/reinstall 的已确认 replacement 也已进入 Coordinator：Coordinator
组合 deploy、manifestless backup 复核和安装后身份校验；adapter 原样调用 #2958 的
`replace_plugin()`、运行态启停和 backup cleanup。Service 仍负责计划确认、路径策略、稳定错误
映射与最终来源登记。
最终调用图审计又移除了 `plugins/upgrade_support.py` 中仅供旧测试使用的 package
filesystem/replacement 转发；该模块现只提供运行态探测、停止和启动。相关测试已改为直接验证
package-management owner。Route/Lifecycle 中不再存在同时直接组合 package filesystem、
Registry 写入和 runtime mutation 的生产路径。
Registry 原型的未提交修订现已使用独立 schema v1 / `PluginRegistrySnapshot`，补入
`plugin_runtime_overrides.json` 的 `enabled`/`auto_start` 稀疏意图，严格拒绝 future
schema、损坏 authority 字段和重复候选，并新增短文件锁、重读/CAS、原子替换 Store 与
确定性并发测试。共享原子 writer 已复用 #2958 的 Windows `os.replace` 退避重试，并保证
临时文件清理失败不会遮蔽原始替换异常。`PluginRegistryService` 的显式 snapshot provider
现已形成 fail-closed authority：全量 refresh、单插件 refresh、候选列表/校验/删除预案均只
定点读取 Registry 声明的 manifest；provider 已配置但未初始化、候选路径丢失或 manifest
身份漂移时返回结构化 503，不再退回 scanner/legacy selection。生产 startup 现已在 reconcile
和 cutover/recovery 完成后动态发布 provider；本任务没有启动真实服务或迁移真实用户状态。
另已加入纯函数 shadow comparison，可把旧
三文件内存迁移投影与候选 Registry 按 authority 字段比较，只返回聚合 mismatch 类别和数量，
不暴露 PluginId、路径、URL 或 receipt 内容。只读 cutover preflight 现已接受已 reconcile 的
v2 `LockFile` 快照，通过显式路径严格读取 selection/state-owner 与 runtime overrides，执行
无损迁移检查、严格 codec round-trip 和 shadow 硬门禁。初始化事务进一步要求
operation lock 内 snapshot 与 canonical disk bytes 一致，使用固定 manifest 备份三份 legacy
原字节，并对 partial backup、重复启动、并发 initializer、Registry 创建后崩溃和损坏 Registry
降级做确定性恢复。原子 commit marker 绑定 backup manifest 与初始 Registry；已提交 cutover
后允许 Registry 独立推进 revision，重启不再咨询退役 legacy，但会继续验证备份链、初始化
血缘和当前 Registry。所有测试只使用临时目录，不会创建真实用户 Registry。生产路径工厂
现已区分 legacy fallback 与唯一 runtime Registry 写路径；进程内发布桥可让既存服务延迟
取得 authority；Selection/StateOwner/runtime intent 旧 API 已在发布后单写 Registry，且
no-op 不推进 revision。InstallSourceManager 的生产调用面也已有 Registry facade，覆盖来源
记录、查询、回滚和软删除，同时禁止回写 legacy lock。legacy 损坏 authority 已有固定原字节
failure-backup；startup 已实现 registry/legacy/blocked 三态，并保证发布先于 runtime discovery。
动态 provider 每次从共享 Registry 文件重读，能观察其他进程的 commit；读失败结构化 503，
startup 无法判定 authority 时也保持 blocked，不得恢复目录扫描。authority 模式已经新增两条
高层事务合同：删除当前运行的 Market 候选会先启动并提交 builtin fallback、再 retire 旧包；
目标候选启动失败会恢复旧 metadata/实例且不改变 Registry Selection/StateOwner。候选代码退役
现已进入独立 `CandidateRemovalCoordinator`：先原子移入 scanner 不可见的隐藏暂存区，Registry
retire 失败则恢复原目录；fallback 已提交时，外层 Lifecycle 会切回旧候选并恢复原授权 receipt；
retire 成功但最终清理失败时写入 commit marker，启动时只重试带精确 marker 的目录。profile
ownership、共享判定、暂存/恢复和 deferred record 策略现已抽入独立
`package_management/profile_cleanup.py`，不再由 Lifecycle 定义文件策略。
`CandidateRemovalCoordinator` 现已把 profile 暂存、candidate code 暂存、Registry retire、
双资源失败恢复、最终清理和两类 deferred cleanup 组合成一个注入式事务；Registry 提交后的
清理失败只形成显式安全残留，不再触发伪回滚。授权/runtime rollback 与高层 fallback 编排仍在
Lifecycle。profile 安全合同现已直接测试 package-management 边界，旧 wrappers 已删除，具体
Package/Profile service 与 deferred cleanup record 路径由 infrastructure composition root
组装。Lifecycle 不再导入 package filesystem 实现。默认删除现已只退休候选代码并明确返回
`user_data_preserved=true`，8 locale 确认文案同步承诺保留关联配置与用户数据。用户已明确要求
把独立前端危险操作纳入本轮；该入口和最终自动化组合验证现已完成，Phase 4 尚余隔离的
Electron/web 人工验证。显式 package-profile 删除事务的第一块
持久化基础由 legacy `InstallSourceManager` 与 Registry facade 对称提供：
`mark_profile_removed()`：它按精确候选清空 `profile_dir` 并提交
`profile_installed=false`，保留 package id、来源、软删除审计、Selection 与 StateOwner；操作
本身不接触文件，legacy 写盘失败会恢复进程内旧 snapshot，重复提交不产生新 revision。其上
现已新增独立 `PackageProfileRemovalCoordinator`：只接受 Registry 已退休、原代码目录已消失且
`profile_installed=true` 的 imported/market 候选；它复用既有 sharing/symlink/path policy，按
stage → Registry commit → finalize 排序执行，commit 失败恢复 profile，commit 后清理失败写入
既有 deferred-cleanup record。现已通过 admin-only
`DELETE /plugin/{plugin_id}/package-profile` 接入：请求必须给出精确 user candidate key 与
`confirm_delete=true`，整个调用由既有跨进程 serialized plugin-operation lock 包裹，稳定映射
404/409/500/503 错误且响应不暴露本地 profile 路径。插件管理页现已提供始终可发现的“保留数据”
入口；它通过 admin-only `GET /plugins/retained-package-profiles` 读取 Registry retired tombstone 中
仍有显式 package-profile ownership 的候选，列表只返回 PluginId、来源、package id 和候选 key，
不返回本机路径。每条删除使用独立长按确认并调用上述精确候选 DELETE；普通卸载确认框仍然只删
代码。列表读失败会清空旧投影，实际删除仍在 operation lock 内重新验证 ownership、sharing、
symlink 与路径安全。
last-candidate authority 高层合同也已补齐：删除唯一候选会保留 Registry retired tombstone，清空
Selection/runtime intent、保留 StateOwner；即使完全相同的目录与 manifest 字节随后重新出现，
新的 authoritative refresh/restart projection 仍返回零候选，不触发 full scanner、不注册、不
autostart，也不回写 legacy lock。该合同证明现有生产逻辑正确，无需再增加一层实现。

用户随后明确要求：排除最早的 `refactor/plugin-installation-lifecycle` 大重构，将其余
插件中心相关工作收敛到当前 lifecycle worktree。因此 `feature/plugin-log-clear` 已
作为独立可验证切片移植到本工作树；这项显式决定覆盖“一条工作线只含一个 Phase”
的一般偏好，但不扩大 Registry、包格式、Lifecycle 或 Market 的架构范围。

## 1. Baseline and purpose

The refactor starts from the safety behavior delivered by PR #2958 and the
identity-selection semantics prototyped on `refactor/plugin-identity-selection`.

- PR #2958 is the safety foundation. Its archive validation, staging, operation
  lock, replacement, rollback, profile protection, and error contracts must be
  preserved rather than rewritten.
- The identity-selection work is the semantic foundation. Logical plugin ids,
  explicit candidates, serialized switching, state ownership, data-access
  authorization, and Market release-chain rules remain valid.
- The immediate P1 is part of this refactor: deleting the selected running
  external candidate can expose builtin metadata without starting the builtin
  process. Candidate removal must become one coordinated lifecycle transaction.
- The current implementation spreads package-management responsibilities across
  the Plugin CLI application service, Market bridge, upgrade helpers, install
  source manager, Registry, Lifecycle, and frontend orchestration. This refactor
  gives those responsibilities one explicit boundary without replacing the safe
  installer.

## 2. Hard scope contract

### 2.1 Required outcomes

This refactor is complete only when all of the following are true:

1. Removing the currently selected candidate validates and starts an eligible
   fallback before committing the new selection or retiring the old package.
   Failure restores the old selection, metadata, code availability, and running
   instance. State ownership is not cleared by candidate removal.
2. Package-format operations remain in `plugin.neko_plugin_cli`; runtime package
   storage and file transactions live behind a dedicated package-management
   module; runtime Lifecycle contains no package-directory or package-profile
   deletion policy.
3. Local import and Market install/upgrade/reinstall use the same thin
   installation coordinator. Market retains transport, OAuth, download,
   external SHA verification, progress, and reporting only.
4. One durable plugin Registry becomes the sole source of truth for installed
   candidates, selected candidate, enabled intent, and state owner. The current
   install-source and candidate-selection files must not remain active dual-write
   authorities after cutover.
5. Registry resolution remains pure. Refreshing a read model or runtime catalog
   must not implicitly change a running candidate.
6. User-visible failures have one presentation owner. Candidate switch/removal
   failures must not produce both the raw interceptor toast and a localized UI
   toast.

### 2.2 Explicit non-goals

The following are outside this refactor and require a separate design decision:

- changing the `.neko-plugin` or `.neko-bundle` package format;
- rewriting the PR #2958 installer or weakening any safety transaction;
- introducing an immutable digest-addressed Package Store;
- replacing candidate keys with final `PackageRef` or adding `AuthorityId`;
- physically migrating plugin state to a new authority-scoped directory layout;
- declaring a clean break from legacy plugins or legacy plugin data;
- automatically deleting user data when candidate code is removed;
- redesigning the Plugin Manager UI beyond the required candidate-removal and
  error-presentation changes;
- changing developer `init`, `build`, `analyze`, `release`, or `publish` flows;
- absorbing plugin-internal resource installers such as Textractor, Tesseract,
  or RapidOCR model installation;
- adding a production dependency, database, long-running poller, or background
  service;
- unrelated Registry, Lifecycle, Market, CLI, frontend, or localization cleanup.

If implementation pressure suggests crossing one of these boundaries, stop that
phase and obtain a new scope decision. Passing tests is not permission to expand
the boundary.

## 3. Target ownership model

### 3.1 Package-format library

Existing `plugin/neko_plugin_cli/core` remains responsible for archive structure,
safe paths, bounded reads, manifest parsing, dependency validation, payload hash,
inspection, and extraction primitives. Developer tooling remains outside the
runtime package manager.

### 3.2 Runtime package-management module

Use a directory, not a giant service file:

```text
plugin/server/application/package_management/
  package_service.py
  artifacts.py
  install_plan.py
  filesystem.py
  replacement.py
  coordinator.py
  ports.py

plugin/server/infrastructure/package_management/
  json_registry.py
  locks.py
```

Names may follow narrower project conventions discovered during implementation,
but the dependency directions and ownership rules below are fixed.

#### PackageService

Owns package artifacts and filesystem transactions:

- inspect and verify through the existing package-format library;
- stage and promote package payloads;
- quarantine, restore, retire, and clean package code;
- validate managed roots, links/reparse points, and package-owned profiles;
- perform directory backup/restore and return structured rollback results.

It must not choose a candidate, write Selection/StateOwner, start or stop a
plugin, download Market URLs, or show UI errors.

#### PluginRegistry

Owns durable desired state:

- installed candidate records and source/provenance;
- selected candidate;
- enabled/runtime intent;
- state owner and data-access grant;
- schema version and monotonic revision.

It must not persist PID, running/starting/crashed actual state, staging paths,
quarantine jobs, or long-term rollback journals. It must not infer Selection or
StateOwner from an arbitrary directory scan.

Every write must use a short cross-process registry-file critical section:

1. acquire the file lock;
2. re-read and validate the latest snapshot;
3. check the expected revision/CAS precondition;
4. apply one deterministic mutation;
5. write a temporary file and atomically replace the registry;
6. publish the new frozen in-process snapshot;
7. release the file lock.

Per-plugin lifecycle locks protect long transactions. For bundle operations,
locks are acquired by normalized PluginId order. The registry file lock is never
held while downloading, unpacking, importing code, or starting a process.

#### RuntimeLifecycle port

Owns runtime actual state only:

- probe running state;
- start an exact already-resolved candidate;
- stop and reload;
- host registration and cleanup;
- startup timeout/failure policy;
- event, handler, and tool cleanup.

It must not scan directories to select code, delete a package, mutate package
provenance, or commit Selection/StateOwner.

#### InstallationCoordinator

Is the only layer allowed to compose PackageService, PluginRegistry, Resolver,
and RuntimeLifecycle. It owns install, replace, switch, and candidate-removal
transactions plus their rollback ordering. Routes translate transport DTOs into
coordinator requests; they do not assemble transactions themselves.

### 3.3 Required dependency direction

```text
Package Format
      |
      v
PackageService        PluginRegistry        RuntimeLifecycle
          \                |                /
           \               |               /
                 InstallationCoordinator
                           ^
                           |
             Local routes / Market transport
```

There must be no reverse dependency from Registry or PackageService to concrete
Lifecycle, no concrete JSON dependency inside Lifecycle, and no direct Market
dependency on package filesystem helpers.

## 4. Persistence cutover contract

JSON remains the intended storage format for this refactor; no new database or
production dependency is justified at the current scale. The durable component
is a distinct Registry store backed by `plugin_registry.json`; it must not reuse
the legacy lock filename.

The target schema starts at version 1, groups data by logical PluginId, and
contains candidates, selection, sparse enabled/auto-start intent, state owner,
source/provenance, schema version, and revision. Candidate keys may remain the
current `(root_id, directory_name)` in this refactor; introducing digest
PackageRef is explicitly deferred.

Cutover rules:

- migration reads `plugins.lock.json`, `plugin_candidate_selections.json`, and
  `plugin_runtime_overrides.json` under the plugin operation lock plus the short
  Registry-file lock;
- migration is deterministic and idempotent;
- old files are backed up or retained read-only until cutover validation passes;
- there is exactly one authoritative write target after cutover;
- a valid, explicitly empty Selection may be materialized once by the current
  Resolver during migration; corrupt or invalid explicit Selection/StateOwner
  never falls back to guessing from disk;
- existing state with no provable owner fails closed;
- a directory scanner may reconcile package presence during the compatibility
  period, but may not resurrect durable Selection or StateOwner;
- rollback before cutover may return to the old readers; rollback after cutover
  restores the pre-cutover snapshot rather than attempting reverse dual-write.

The filename decision is closed: use `plugin_registry.json`. The three legacy
files are migration inputs and read-only backups after cutover. Compatibility
APIs forward to the new Store; they must not continue writing old JSON. A
pre-cutover in-memory shadow comparison is allowed, but production dual-write is
not.

## 5. Candidate-removal transaction

Removing candidate code and deleting user data are separate operations. The
candidate-removal transaction is fixed as follows:

1. acquire the PluginId operation lock;
2. re-read Registry desired state and runtime actual state;
3. identify the exact candidate being removed;
4. calculate a fallback with the pure Resolver;
5. validate fallback metadata, state authorization, and startability;
6. if the removed candidate is running, stop it;
7. apply fallback metadata and start it when runtime intent requires running;
8. commit candidate removal and Selection in one Registry revision;
9. retain StateOwner unless an explicit, separately authorized data operation
   changes it;
10. retire or delete the old candidate code only after commit;
11. refresh projections without changing the running instance.

Before the Registry commit, any failure restores the old metadata and restarts
the old instance. After the Registry commit, package cleanup failure is reported
as deferred cleanup and must not reverse a valid running fallback. If no safe
fallback exists, removing a running selected candidate is rejected before stop
or filesystem mutation.

## 6. Phases and acceptance gates

Each phase should be independently reviewable and normally map to one PR. Do not
begin a later phase while an earlier phase has unresolved behavioral failures.

### Phase 0 — stabilize identity selection

Goal: finish the current identity-selection behavior before structural movement.

Scope:

- implement exact candidate removal semantics;
- make fallback validation/start/selection commit transactional;
- preserve old candidate and StateOwner on failure;
- remove duplicate raw/localized error presentation;
- add the missing P1 regression tests and manual scenario.

Exit gate:

- deleting the running Market candidate starts builtin when eligible;
- fallback startup failure restores the old Market instance and selection;
- deleting an unselected candidate does not disturb the running candidate;
- StateOwner survives candidate removal;
- candidate failures show one localized message;
- existing identity-selection backend and frontend suites pass.

Rollback: revert only Phase 0 commits; no persistence-schema cutover occurs.

Estimated engineering time: **2–3 working days**.

### Phase 1 — extract the package boundary without changing behavior

Goal: move existing safe behavior behind PackageService and explicit ports.

Scope:

- move artifact IO, staging/promotion, directory backup/restore, link/reparse
  guards, and cleanup helpers;
- keep PluginCliService as a compatibility facade;
- keep routes, response models, filesystem layout, and persistence formats;
- retain current Lifecycle adapters for stop/start through injected ports.

Exit gate:

- no new product behavior or API change;
- PR #2958 install-plan, safe-upgrade, operation-lock, archive, profile ownership,
  rollback, and error-contract tests pass unchanged;
- PackageService has no Registry, Market, frontend, or concrete Lifecycle import;
- diff review can map every moved helper to its previous behavior.

Rollback: mechanical revert; no data migration.

Estimated engineering time: **3–4 working days**.

### Phase 2 — unify local and Market coordination

Goal: make one coordinator own all package mutations.

Status (2026-08-28): **completed in `36733387`**.

Scope:

- introduce typed install/replace requests and results;
- build ports against the actual current synchronous/asynchronous contracts;
- route local import and Market install/upgrade/reinstall through the same
  coordinator;
- remove `install_source_override`-style cross-layer dictionaries;
- keep Market OAuth, download, SHA verification, progress, cancellation window,
  and reporting in Market transport;
- keep package downloads outside long plugin-operation locks.

Exit gate:

- equivalent local and Market packages use the same staging/replacement path;
- stale-plan, lock-write, validation, restart, and rollback failures preserve the
  same stable error semantics;
- Market download does not hold the lifecycle/package mutation lock;
- no route directly calls package filesystem helpers plus Lifecycle in one flow;
- local, Market, integration, and frontend contracts pass.

Recorded exit evidence:

- Local, fresh Market, and Market replacement use the same typed coordinator and
  shared materialize/install/cleanup ordering;
- the concrete Market replacement adapter lives under infrastructure, so the
  route no longer imports package filesystem/replacement helpers or runtime
  lifecycle/operation-lock functions; recorded profile path resolution and
  symlink/package-id validation also run in that adapter after stale snapshot
  revalidation under the operation lock;
- focused Coordinator/Market tests: `39 passed`;
- Phase 2 plus PR #2958 safety contracts: `168 passed, 6 skipped`;
- current complete `plugin/tests/unit/server`: `677 passed, 6 skipped`;
- current install-source property and Market integration contracts: `118 passed`;
- Ruff, frontend type-check, eight-locale consistency, and `git diff --check`
  passed.

Real Market networking, real plugin subprocess replacement/rollback, and manual
Electron/web UI scenarios remain Phase 5 verification items; they are not claimed
by this automated gate.

Rollback: routes switch back to compatibility facades; persistence remains
unchanged.

Estimated engineering time: **4–5 working days**.

### Phase 3 — consolidate durable Registry state

Goal: replace install-source, candidate-selection, and sparse runtime-intent
authorities with one revisioned Registry.

Scope:

- define the grouped Registry schema and deterministic serializer (implemented
  and runtime-wired after successful startup cutover);
- reuse frozen snapshots and tmp+replace with the #2958 Windows retry and
  original-error preservation; preserve corrupt legacy authority evidence before
  first cutover and fail closed on corrupt Registry (implemented);
- add cross-process registry-file locking and re-read/CAS writes (implemented
  with deterministic lost-update tests);
- migrate candidates/provenance, Selection, sparse enabled/auto-start intent,
  StateOwner, and grant (pure migration and explicit-path read-only preflight
  implemented; deterministic backup, atomic initialization, production path
  factory, legacy API forwarding, install-source facade, and startup authority
  switch are implemented);
- update Registry/Resolver/query projections and startup ordering (production
  startup publishes authority before discovery/autostart; reads reload the shared
  file and fail closed on corruption instead of falling back to directory scan);
- stop scanner-driven Selection/StateOwner resurrection (implemented and wired
  after successful startup cutover; blocked startup never falls back to scanner).

Exit gate:

- migration is idempotent and preserves all representable source/selection/owner
  data;
- different processes updating different PluginIds cannot lose each other's
  changes;
- invalid revision and corrupt content fail closed;
- no active dual write remains;
- candidate removal does not clear StateOwner;
- Registry is initialized before runtime discovery/autostart;
- property, integration, corruption, recovery, and concurrency tests pass.

Rollback: restore the pre-cutover Registry snapshot or return to old readers only
before any successful cutover write. Never maintain reverse dual-write logic.

Estimated engineering time: **5–7 working days**.

### Phase 4 — remove package policy from Lifecycle

Goal: enforce the final module ownership after Registry cutover.

Scope:

- remove directory/profile deletion and install-source writes from Lifecycle;
- move deferred package cleanup to PackageService/infrastructure;
- make candidate-code removal and user-data deletion distinct APIs;
- reduce runtime Registry refresh to a read projection;
- update frontend actions and messages only as required by the split.

Current slice:

- candidate code staging/retirement/cleanup/recovery is owned by
  `CandidateRemovalCoordinator` and `PluginPackageService`;
- package-profile ownership, sharing, safe staging, restoration, final cleanup,
  and durable deferred-cleanup policy are owned by
  `package_management.profile_cleanup`;
- `CandidateRemovalCoordinator` now stages profile and candidate code, commits
  Registry retirement, restores both resources on pre-commit failure, and owns
  post-commit cleanup/deferred-cleanup reporting as one injected transaction;
- Lifecycle still owns runtime fallback and Selection receipt rollback;
- profile safety tests now target `package_management.profile_cleanup`
  directly, the private Lifecycle compatibility adapters are removed, and
  `infrastructure.package_management.removal_runtime` owns concrete service
  composition;
- default deletion now retires candidate code only and returns
  `user_data_preserved=true`; frontend confirmation copy across all 8 locales
  states that package configuration and user data are retained;
- the exact-candidate Registry/legacy mutation needed after an explicit profile
  deletion now exists as `mark_profile_removed()`; it clears only profile
  ownership metadata, preserves candidate audit and identity receipts, and is
  idempotent, but it is not exposed as an API and never deletes files itself;
- `PackageProfileRemovalCoordinator` now composes strict retired-candidate
  validation, safe profile staging, that Registry commit, rollback, final
  cleanup and deferred cleanup without touching candidate code or runtime;
- an admin-only exact-candidate HTTP endpoint now invokes that coordinator under
  the existing cross-process plugin-operation lock and requires an explicit
  confirmation field; it returns stable error codes without local paths;
- the last-candidate authority contract now proves a retired Registry tombstone
  remains authoritative across refresh/restart projections even if matching
  bytes reappear on disk; no scanner resurrection or legacy write occurs;
- the plugin manager now has an always-discoverable retained-data dialog backed
  by a path-redacted Registry projection; each exact candidate deletion has a
  separate press-and-hold confirmation;
- isolated Electron/web manual verification remains before the Phase 4 exit gate.

The final automated combined gate now passes for the backend: the complete
server unit suite reports `676 passed, 6 skipped`, and install-source property
plus install-source/Market integration reports `118 passed`. Frontend type-check,
8-locale consistency, focused API tests, and the production build pass. The
frontend unit baseline still has one unrelated failing assertion in the
unchanged `src/utils/i18nLabel.test.ts`; all other non-Playwright frontend unit
tests pass. Electron/web manual verification remains outstanding.

The subsequent Market boundary audit moved recorded profile path planning out
of the route and into the infrastructure replacement adapter. It now executes
after the coordinator revalidates the captured install-source snapshot while
holding the cross-process operation lock. The focused gate is `39 passed`, the
integration gate remains `118 passed`, and the complete server unit suite is now
`677 passed, 6 skipped`.

The following Plugin CLI boundary slice made install-result interpretation a
Coordinator responsibility instead of a concrete-port responsibility. Local
and Market flows now share normalization of current `unpacked_plugins`, legacy
`installed_plugins`, promoted/reused profiles, and the single-plugin Market
constraint. Plugin CLI private methods remain forwarding compatibility facades.
The focused Plugin CLI/Coordinator gate is `92 passed`, the integration gate is
`118 passed`, and the complete server unit suite is `679 passed, 6 skipped`.

The next slice moved the concrete Plugin CLI installation adapter and failed
deployment cleanup into the package-management boundary. Candidate directories,
new package-profile directories, and owned archives are removed there after the
Registry receipt rollback callback; Plugin CLI no longer performs those
filesystem deletions. The focused adapter/CLI gate is `93 passed`, integration
remains `118 passed`, and the complete server unit suite is `680 passed, 6
skipped`.

The confirmed local upgrade/downgrade/reinstall slice now also delegates its
replacement composition to the Coordinator. The adapter still invokes the
unchanged #2958 replacement primitive for stop, backup, install, validation,
profile preservation, restart, and rollback. Plugin CLI retains confirmation,
path policy, stable error mapping, and final provenance recording. The focused
safety gate is `49 passed`, the wider Plugin CLI gate is `95 passed`, integration
remains `118 passed`, and the complete server unit suite is `682 passed, 6
skipped`.

The final mixed-responsibility audit removed the obsolete package filesystem
and replacement re-exports from `plugins/upgrade_support.py`; it now contains
only runtime running-state, stop, and start adapters. Remaining Market route
filesystem writes are transport download/task-state operations, and the
resource installer route remains outside plugin package management. The focused
boundary gate is `57 passed, 4 skipped`, integration remains `118 passed`, and
the complete server unit suite is `688 passed, 6 skipped`.

The architecture is now frozen. A complete dirty-diff acceptance review found
no remaining production path that bypasses the separated code-removal and
profile-removal contracts. The latest frontend acceptance rerun also passes:
Vue type-check, production build, 8-locale consistency (`721` keys), and the
focused plugin/log API tests (`9 passed`). A short-lived system-Edge run against
the production frontend build, with every API intercepted by synthetic data,
verified retained-profile discovery, blocked active-code state, path-redacted
display, the separate press-and-hold confirmation, the exact confirmed DELETE
payload, and post-delete list refresh without page/console errors. Full Electron
distribution and real plugin-process scenarios remain outside the Phase 4 gate.
The existing isolated package-format/CLI subprocess workflow also passes
(`5 passed`): build, inspect, bundle install into temporary roots, and the guard
that prevents developer CLI from bypassing Plugin Center runtime installation.

Exit gate:

- Lifecycle imports no package filesystem or concrete Registry persistence code;
- PackageService never starts/stops a plugin directly;
- only Coordinator composes package, Registry, and runtime mutations;
- deleting code never deletes state without a separate explicit operation;
- delete, fallback, deferred cleanup, restart, UI, and Electron/web entry tests pass.

Rollback: compatibility adapters may temporarily restore old call routing, but
directory scanning must not regain authority to select running code.

Estimated engineering time: **3–4 working days**.

### Phase 5 — full verification and durable documentation

Goal: validate the combined architecture and retire this working document.

Scope:

- focused unit/property/integration suites after every phase;
- final backend regression, frontend tests/typecheck/i18n parity, and diff audit;
- Windows manual scenarios for install, upgrade, reinstall, downgrade, candidate
  switch, selected-candidate removal, fallback failure, restart, and cleanup;
- verify web and Electron entry behavior;
- write durable architecture and migration documentation;
- remove this temporary file after accepted decisions are transferred.

Exit gate:

- all required verification is recorded with exact commands/results;
- no unexplained diff, temporary output, active dual persistence, or known P1
  remains;
- rollback/recovery instructions and known limitations are documented.

Estimated engineering time: **2–3 working days**.

## 7. Expected schedule

Assumption: one engineer working primarily on this refactor, normal local CI
capacity, no new product requirements, and reviews returned within one working
day per phase.

| Milestone | Cumulative expected time |
| --- | ---: |
| Identity P1 stabilized | Day 2–3 |
| Package boundary extracted | Day 5–7 |
| Local and Market coordination unified | Day 9–12 |
| Registry persistence consolidated | Day 14–19 |
| Lifecycle boundary enforced | Day 17–23 |
| Full verification/documentation complete | Day 19–26 |

Base engineering estimate: **19–26 working days**.

Allowing for review feedback, Windows-only failures, CI queue time, and one
schema-migration iteration, the realistic delivery expectation is **22–30 working
days, approximately 4.5–6 calendar weeks for one engineer**.

The first user-visible correction (P1 plus duplicate toast) should be available
for review in **2–3 working days**. The package-boundary-only milestone should be
reviewable in roughly **1–1.5 weeks**. Persistence consolidation is the largest
risk and should not be promised inside the boundary-extraction milestone.

The estimate must be revised before continuing if any of the following appears:

- backward migration must preserve data not represented by either current file;
- bundle transactions need new product semantics;
- cross-process registry writers exist outside the shared operation-lock domain;
- old plugins must be automatically converted to a new code/state layout;
- a new storage technology or production dependency is requested;
- changes are required in plugin business code or unrelated runtime modules.

## 8. Mandatory verification contract

At minimum, retain and run the focused contracts covering:

- package inspect/archive limits and package-format properties;
- install planning, explicit confirmation, and stale-plan rejection;
- fresh install, upgrade, downgrade, and reinstall;
- backup, validation, restart, cleanup, complete rollback, and incomplete rollback;
- profile ownership, sharing, custom roots, symlinks/reparse points, and deferred
  cleanup;
- cross-task and cross-process operation locking, cancellation, and re-entry;
- Market download/hash/fallback behavior and install-source provenance;
- deterministic Registry round trips, migration, corruption, CAS, and lost-update
  prevention;
- explicit candidate selection, data authorization, release-chain inheritance,
  selected-candidate removal, fallback startup, and restoration on failure;
- frontend confirmation, one-toast error ownership, candidate state refresh, all
  supported locale keys, web entry, and Electron entry behavior.

Tests must use deterministic synchronization rather than arbitrary sleeps. Unit
tests must not require the real network or real user configuration/state.

## 9. Branch and review policy

- Phase 0 finishes on the current identity-selection line unless implementation
  evidence proves it cannot be corrected safely.
- Structural phases begin from the latest accepted branch containing PR #2958
  and the completed identity-selection behavior.
- Keep one phase per reviewable PR where practical. Do not mix schema cutover
  with mechanical file movement.
- Do not commit or push merely because a phase is locally complete; follow the
  task's explicit Git authorization.
- Before every phase, verify worktree, branch, upstream, dirty state, and affected
  module-specific instructions. Before delivery, review every changed file and
  map it to this contract or a required test/documentation update.
