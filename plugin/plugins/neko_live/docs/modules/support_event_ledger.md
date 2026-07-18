# 可信支持事件账本设计 Spec

> 状态：**Draft — 待维护者审批**
> 对应迁移矩阵项：`query_gifts` → 应吸收（只吸收为未来「可信支持事件账本」的脱敏聚合查询）
> 文档更新日期：2026-07-18

---

## 目录

- [1. 为什么需要账本](#1-为什么需要账本)
- [2. 现有状态审计](#2-现有状态审计)
- [3. 设计目标与非目标](#3-设计目标与非目标)
- [4. 数据模型](#4-数据模型)
- [5. 存储方案](#5-存储方案)
- [6. 写入路径](#6-写入路径)
- [7. 查询接口](#7-查询接口)
- [8. 容量与保留策略](#8-容量与保留策略)
- [9. 隐私与安全约束](#9-隐私与安全约束)
- [10. 与现有 `SupportEventScheduler` 的关系](#10-与现有-supporteventcheduler-的关系)
- [11. 测试门禁](#11-测试门禁)
- [12. 风险与降级](#12-风险与降级)
- [13. Decision Points](#13-decision-points)

---

## 1. 为什么需要账本

当前 `live_support_events` 的 `SupportEventScheduler` 只在**内存**中维护优先级队列和连击状态机。

### 现状问题

| 场景 | 问题 |
|------|------|
| NEKO Live 重启/重载 | 所有支持事件记录丢失，主播和观众看不到历史 |
| 直播结束断开 | 数据清零，无法复盘本场支持统计 |
| 贡献面板 | 无法显示"本场收到 X 个礼物 / Y 个 SC"等聚合数据 |
| 主播查账 | 无法回溯"谁在什么时候送了什么"（脱敏版） |

### 旧插件做法（不直接复用）

旧 `bilibili_danmaku` 用 SQLite 完整记录所有字段：

- `gift` 表：room_id, uname, uid, gift_name, gift_id, coin_type, total_coin, number, ulevel, admin, guard, medal 等 **12 个字段**
- `guard` 表：room_id, uname, uid, gift_name, guard_level, price, start_time, end_time 等
- 问题：字段过多，包含原始用户等级/管理标记等非必要信息，不符合 NEKO Live 的隐私最小化原则

---

## 2. 现有状态审计

### NEKO Live 已落地能力

| 组件 | 能力 | 持久化？ |
|------|------|---------|
| `SupportEventScheduler` | 优先级队列 + 连击状态机 + provider ID 去重 | ❌ 纯内存 |
| `BiliLiveIngestModule._recent_support_event_keys` | 350ms ingest 层去重 | ❌ 纯内存 |
| `live_support_events._support_context()` | 事件类型/礼物名/金额/舰长等级 脱敏提取 | N/A |
| `audit_store` | 操作审计记录 | ✅ 但仅限操作级，不支持聚合查询 |
| `viewer_store` | 观众档案（含印象字段） | ✅ 但不含支持事件明细 |

### 旧 bilibili_danmaku 可参考（不直接复制）

| 旧组件 | 可吸收设计 | 应丢弃 |
|--------|-----------|--------|
| SQLite gift 表 schema | 字段结构设计思路 | 完整字段集（过多隐私字段） |
| `GiftAggregator` 窗口聚合 | 按 (uid, gift_name) 合并思想 | 其 callback 推送方式（neko_live 走 pipeline） |
| `query_gifts` entry | 聚合查询接口形态 | 返回原始明细 |
| `query_stats` entry | 统计接口形态 | 具体实现 |

---

## 3. 设计目标与非目标

### 目标

1. **持续记录**：每次 dispatch 完成的支持事件（gift/SC/guard）写入持久化账本
2. **脱敏聚合查询**：支持按 room/session/event_type 做脱敏统计，不暴露原始 payload
3. **有界存储**：固定容量上限，超限自动淘汰（FIFO 或按时间）
4. **本场统计**：Dashboard 可展示"本场收到 X 个 gift，Y 个 SC"等聚合数字
5. **故障隔离**：账本写入失败不影响实时调度和输出

### 非目标（不在此 spec 范围）

- ❌ 不做原始弹幕/进场/关注持久化（`query_danmaku` 已标记为明确废弃）
- ❌ 不做贡献排名（`contribution_rank` 已标记为当前版本不采集）
- ❌ 不做观看时长记录（`watch_time` 已标记为当前版本不采集）
- ❌ 不做跨场累计/终身统计（本 spec 只做单场 + 有限近场）
- ❌ 不做主播提现/价值转换/财务对账
- ❌ 不做实时通知/Webhook（那是独立功能）
- ❌ 不替换现有 `audit_store`（账本和审计是不同关注点：账本存储脱敏事件记录供查询，审计存储操作日志供诊断）

---

## 4. 数据模型

### 4.1 原始记录（写入用）

```python
@dataclass
class SupportLedgerRecord:
    # 标识
    provider_event_id: str       # 提供方事件 ID（去重/回溯用）
    event_type: str              # "gift" / "super_chat" / "guard"
    provider_event_type: str     # "SEND_GIFT" / "COMBO_SEND" / "GUARD_BUY" 等
    
    # 时间
    timestamp: float             # 收到时间（unix 秒）
    room_id: int                 # 直播房间号
    
    # 用户（脱敏——只存 UID + 昵称，不存用户等级/房管/舰长等级等）
    uid: str                     # 字符串 UID（含平台前缀）
    nickname: str                # 显示昵称（最长 80 字符）
    
    # 礼物核心字段
    gift_name: str               # 礼物名称（最长 80 字符）
    gift_value: int              # 平台原始价值（金瓜子/积分/平台代币）
    value_cny: float             # 统一换算为人民币元（跨平台可比；B站 gold=CNY×1000，silver=0）
    coin_type: str               # "gold" / "silver" / 平台代币类型
    gift_count: int              # 数量
    
    # SC 专用
    sc_text: str                 # SC 文本摘要（最长 200 字符，超长截断）
    
    # Guard 专用
    guard_level: int             # 1=总督, 2=提督, 3=舰长
    
    # 元信息
    trace_id: str                # 事件链路 trace_id（用于排查）
    live_session_generation: int # 会话代际
```

### 4.2 聚合查询结果（对外暴露）

```python
@dataclass
class SupportLedgerSummary:
    room_id: int
    session_generation: int
    
    # 按事件类型聚合
    total_gifts: int             # 礼物总数（count）
    total_sc: int                # SC 总数
    total_guard: int             # 舰长总数
    
    # 按金额聚合
    total_gold_value: int        # 总金瓜子价值
    total_sc_value: int          # 总 SC 金额
    total_cny: float             # 总人民币价值（跨平台所有事件 value_cny 之和）
    
    # 按用户聚合（前 N 名，只含 uid + nickname + count，不含金额排名）
    top_users: list[TopUser]     # 最多 events 的观众，上限 10
    
    # 时间范围
    first_event_at: float
    last_event_at: float

@dataclass
class TopUser:
    uid: str
    nickname: str
    event_count: int
```

### 4.3 明细查询结果（对外暴露）

```python
@dataclass
class SupportLedgerEntry:
    event_type: str              # "gift"/"super_chat"/"guard"
    timestamp: float
    uid: str
    nickname: str
    gift_name: str
    gift_value: int
    gift_count: int
    coin_type: str
    guard_level: int             # 仅 guard 事件有值
    sc_text: str                 # 仅 SC 事件有值，最长 80 字符截断
```

**不暴露的字段**：原始 payload、provider_event_id、trace_id、live_session_generation、用户等级/房管/舰长等级标记（这些是调度/审计内部字段，不是公开消费内容）

---

## 5. 存储方案

### 推荐方案：本地 JSON-L（追加写）

**理由**：
- 与现有 `viewer_store.py`（JSON 文件）技术栈一致
- 追加写（append-only）性能好，不需要数据库连接
- 容量限制通过文件大小和条目数双重控制
- 不需要额外依赖（现有 Python 标准库足够）
- 可审计：追加日志本身就是不可变记录

**文件结构**：

```
plugin/plugins/neko_live/data/
├── support_ledger/
│   ├── v1/                              # schema 版本目录
│   │   ├── room_12345_001.ledger        # 每个文件最多 10000 条；`control.json` 缓存每个文件的预聚合统计（文件路径 → 事件类型计数 + 时间范围 + 文件大小），避免 `get_room_summary` 全量扫描
│   │   ├── room_12345_002.ledger
│   │   └── ...
│   └── control.json                     # 控制信息
```

每条记录为一行 JSON（JSON Lines 格式）：

```jsonl
{"ts":1712345678.12,"et":"gift","pet":"SEND_GIFT","uid":"bilibili:12345","nick":"测试用户","gn":"小电视","gv":1000,"gc":1,"ct":"gold","rid":12345,"tid":"trace-xxx"}
{"ts":1712345679.50,"et":"super_chat","pet":"SUPER_CHAT_MESSAGE","uid":"bilibili:67890","nick":"SC用户","gn":"Super Chat","gv":30000,"gc":1,"ct":"gold","st":"主播你好！","rid":12345,"tid":"trace-yyy"}
```

字段名缩写（节省存储空间，每节省约 30% 的 JSON 体积）：

| 缩写 | 全称 |
|------|------|
| `ts` | timestamp |
| `et` | event_type |
| `pet` | provider_event_type |
| `uid` | uid（含平台前缀） |
| `nick` | nickname |
| `gn` | gift_name |
| `gv` | gift_value（平台原始价值） |
| `vc` | value_cny（人民币元，跨平台可比） |
| `gc` | gift_count |
| `ct` | coin_type |
| `rid` | room_id |
| `tid` | trace_id |
| `gl` | guard_level |
| `st` | sc_text |
| `sg` | session_generation |

### 候选方案：SQLite

**优点**：查询灵活，支持 SQL 聚合
**缺点**：需要管理连接/线程安全，与"不复用旧插件 SQLite schema"原则冲突

### 不采用方案：PluginStore

`PluginStore` 是 K-V 存储，不适合做有序追加日志，查询效率低。

---

## 6. 写入路径

### 写入时机

仅在 `SupportEventScheduler` 实际 **dispatch 成功** 后写入。

```
EventBus → live_support_events._on_bus_event()
  → scheduler.submit()              排队
  → scheduler._dispatch_once()      出队 dispatch
    → ctx.handle_live_payload()     进 pipeline
    → pipeline → neko_dispatcher    实际输出
    → [新] ledger.record()            ← 写入账本
```

### 为什么不写入排队的、未 dispatch 的或 dispatch 失败的

| 原因 | 说明 |
|------|------|
| **防浪费** | 排队后被高优挤掉的事件不应计入（从未实际产出） |
| **防双记** | dispatch 失败的事件会释放 provider_event_id，后续可能重入；记成功才是最终态 |
| **防夸大** | combo 连击过程中多次更新不应独立记账，只有 finalized 后的 dispatch 才记 |

### 写入实现

```python
class SupportLedger:
    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self._buffer: list[dict] = []           # 内存缓冲
        self._buffer_lock = asyncio.Lock()
        self._flush_interval = 5.0              # 每 5 秒或 50 条刷一次
        self._flush_limit = 50
        self._file_handle: Optional[IO] = None
        self._current_file_idx: int = 0
        self._current_file_records: int = 0
        self._max_records_per_file = 10000
        self._max_total_records = 100000
        self._flush_task: Optional[asyncio.Task] = None

    async def record(self, payload: dict) -> None:
        """追加一条成功 dispatch 的支持事件记录（异步、非阻塞）"""
        entry = self._to_entry(payload)
        async with self._buffer_lock:
            self._buffer.append(entry)
            if len(self._buffer) >= self._flush_limit:
                await self._flush()

    async def _flush(self) -> None:
        """将缓冲区写入文件，写入失败不阻塞、不影响实时输出"""
        ...

    async def close(self) -> None:
        """关闭账本，刷出剩余 buffer"""
        ...
```

---

## 7. 查询接口

### 7.1 `get_room_summary(room_id, session_generation) -> SupportLedgerSummary`

用于 Dashboard 展示"本场收到 X 个礼物 / Y 个 SC"。

### 7.2 `query_entries(room_id, event_type=None, uid=None, session_generation=None, limit=50) -> list[SupportLedgerEntry]`

用于开发者沙盒/复盘界面查看明细。`session_generation` 可选，默认返回当前场次；传入 `None` 返回该房间所有历史。

**不提供的查询**：
- ❌ 跨房间聚合
- ❌ 跨用户对比/排名
- ❌ 时间范围查询（超出本场范围）

### 7.3 集成到现有的模块接口

```python
class LiveSupportEventsModule(BaseModule):
    # 新增
    def get_ledger_summary(self, room_id: int) -> SupportLedgerSummary:
        ...

    def get_ledger_entries(self, room_id: int, limit: int = 50) -> list[SupportLedgerEntry]:
        ...
```

### 7.4 暴露为 Plugin Entry（开发者工具）

```python
@plugin_entry(
    id="query_support_ledger",
    name="查询支持事件账本",
    description="查询本场直播收到的礼物/SC/舰长记录（脱敏聚合）",
    input_schema={...}
)
async def query_support_ledger(self, summary: bool = False, limit: int = 50, **_):
    if not self.runtime.config.developer_tools_enabled:
        return Err(SdkError("developer mode is disabled"))
    if summary:
        return Ok({"summary": ...})
    return Ok({"entries": [...]})
```

---

## 8. 容量与保留策略

### 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_records_per_file` | 10,000 | 单个文件最大条目，超限创建新文件 |
| `max_total_records` | 100,000 | 总记录上限，超限淘汰最旧文件 |
| `max_rooms` | 50 | 最多记录的房间数 |
| `file_retention_days` | 30 | 文件保留天数（从最后写入算起）；同时受 `max_total_records` 兜底 |

### 淘汰策略

1. **按房间数**：超过 `max_rooms` 个房间时，淘汰最久未更新的房间
2. **按总数**：总记录数超过 `max_total_records` 时，删除最旧的文件
3. **按时间**：文件最后更新时间超过 `file_retention_days` 时，后台清理

### 数据目录清理

`LedgerStore` 提供 `cleanup(force=False)` 方法，由插件 `startup` 和 `shutdown` 生命周期调用，或由定期 `timer_interval` 调用：

```python
@timer_interval(id="ledger_cleanup", seconds=3600, auto_start=True)
async def ledger_cleanup(self, **_):
    self._ledger.cleanup()
```

---

## 9. 隐私与安全约束

| 规则 | 说明 |
|------|------|
| **不存原始 payload** | 只投影脱敏字段，不存 cookies/tokens/signatures |
| **不存用户等级** | 不保存 ulevel/admin/guard/vip/svip 等用户属性 |
| **UID 带平台前缀** | `bilibili:12345` 而不是裸数字 `12345` |
| **昵称截断** | 最长 80 字符 |
| **SC 文本截断** | 最长 200 字符（写入）/ 80 字符（查询返回） |
| **不暴露 provider_event_id** | 对查询结果不可见（仅内部去重用） |
| **不暴露 trace_id** | 对查询结果不可见 |
| **不暴露 session_generation** | 对查询结果不可见 |
| **不写入 log 系统** | 账本内容不通过 logger 输出 |
| **不进入 viewer_store** | 观众档案不包含支持事件明细，只保留"是否支持过"的布尔标记 |

---

## 10. 与现有 `SupportEventScheduler` 的关系

```
SupportEventScheduler (现有)         SupportLedger (新增)
├─ submit(payload)                   ─
├─ _dispatch_once(payload)           ─
│   └─ await dispatch(payload)        → 写入条件：dispatch 成功
├─ reset()                            → close_and_clear()
├─ close()                            → flush_and_close()
└─ status() → 纯内存统计               + include_disk_stats() → 含持久化统计
```

### 设计原则

- **`SupportEventScheduler` 不直接依赖 `SupportLedger`**：用一个可选的 `on_dispatched` 回调解耦
- **写入失败不阻塞调度**：账本写入抛异常时 catch 并记 audit，调度线程继续运行
- **账本是调度器的下游观察者**，不是调度器的组成部分

---

## 11. 测试门禁

| 测试 | 说明 |
|------|------|
| `test_ledger_records_on_successful_dispatch` | 一次成功 dispatch → 账本增加一条 |
| `test_ledger_does_not_record_failed_dispatch` | dispatch 异常 → 账本不变 |
| `test_ledger_does_not_record_queued_only` | 只排队未 dispatch → 账本不变 |
| `test_ledger_buffer_flush_on_count` | 50 条缓冲自动刷文件 |
| `test_ledger_buffer_flush_on_timer` | 5 秒无写入自动刷 |
| `test_ledger_file_rotation` | 超 10000 条创建新文件 |
| `test_ledger_total_limit_enforced` | 总条目超限淘汰最旧文件 |
| `test_ledger_cleanup_old_files` | 超 30 天文件被清理 |
| `test_ledger_cached_summary_after_write` | 写入后 `control.json` 预聚合缓存更新 |
| `test_ledger_cached_summary_on_read` | `get_room_summary` 优先走缓存，写入后无效化 |
| `test_ledger_query_summary` | 聚合统计正确 |
| `test_ledger_query_entries` | 明细查询返回脱敏字段 |
| `test_ledger_query_entries_with_session` | `session_generation` 筛选正确 |
| `test_ledger_failure_does_not_block_dispatch` | 写入异常 → dispatch 继续 |
| `test_ledger_privacy_no_raw_fields` | 查询结果不含 provider_event_id/trace_id/session_generation |
| `test_ledger_stress_writes` | 并发写入不丢数据（1000 条并发） |
| `test_ledger_concurrent_read_write` | 写入时读取 `get_room_summary` 不阻塞/不返回脏数据 |

运行方式：

```powershell
uv run pytest plugin/plugins/neko_live/tests/test_support_ledger.py -q
```

---

## 12. 风险与降级

| 风险 | 影响 | 措施 |
|------|------|------|
| 账本文件损坏 | 当前文件数据丢失 | 每条写入独立 JSON line，不影响其他文件；损坏文件跳过 |
| 磁盘写满 | 写入失败 | catch 异常，记 audit，不阻塞调度 |
| 并发写入 | 数据竞争 | `asyncio.Lock` 保护 buffer；文件写也是单线程协程 |
| 目录权限 | 无法创建文件 | fallback 到 `data_path()` 默认目录，记 audit |
| 容器重启丢失缓存 | 尚未 flush 的数据丢失 | buffer 减少到 5 秒/50 条；可接受（最多丢数十条） |
| 被旧插件数据格式污染 | 数据不一致 | schema 版本号 `v1` + 清晰字段名，不做向后兼容腾挪 |

### 降级路径

- 账本完全不可用（IOError/CannotWrite）时：**静默降级**，调度器照常运行
- Dashboard 显示 `ledger_unavailable: true` 标记
- 写入失败累计超过 10 次后记一次 `audit.record("support_ledger_failed", ...)` 并停止重试直到下次 startup

---

## 13. Decision Points

以下选项需要维护者在实现前拍板：

### DP-1: 存储格式

| 选项 | 优点 | 缺点 |
|------|------|------|
| **推荐：JSON-L 文件** | 无依赖、追加写性能好、可审计、与现有 viewer_store 一致 | 查询需要扫描文件（但文件小、场景少） |
| SQLite | 查询灵活、支持 SQL 聚合 | 与旧插件 schema 相似度高易混淆、多线程管理 |
| PluginStore K-V | 现成存储 | 不适合有序日志、查询效率低 |

### DP-2: 写入范围

| 选项 | 优点 | 缺电 |
|------|------|------|
| **推荐：仅记录 dispatch 成功** | 准确反映实际产出 | 不包含排队后被丢弃的事件 |
| 记录 queue admission（含淘汰） | 完整审计 | 数据量膨胀，部分事件从未产出 |

### DP-3: Dashboard 可见性

| 选项 | 说明 |
|------|------|
| **推荐：只在开发者模式下暴露** | Dashboard 在开发者模式开启时显示"支持事件账本"视图 |
| 普通模式也可见 | 适合所有主播，但需要更多的 UI 设计和 i18n 工作 |
| 仅通过 API 暴露 | 面板不展示，后端插件 entry 可查询 |

### DP-4: value_cny 换算规则

| 选项 | 说明 |
|------|------|
| **推荐：B站 gold / 1000** | B站 gold 瓜子 1000 = 1 元；silver 瓜子 fixed=0 元；SC 金额已标为元×1000 |
| 平台插件自定义 | 每个 provider 在 ingest 层传入已换算的 `value_cny`，账本只存不换算 |
| 硬编码汇率表 | 不可维护，否决 |

`value_cny` 在 `_to_entry()` 中计算，写入时已归一化。Silver 瓜子、免费礼物、无价值事件统一记为 0.0 元。

### DP-5: 旧文件清理责任

| 选项 | 说明 |
|------|------|
| **推荐：插件自主定时清理** | `timer_interval` 每小时检查一次 |
| 依赖 panel 手动清理 | 用户自己点"清空历史"按钮 |
| 无清理（无限增长） | 已否决——必须设上限 |

---

## 附录 A：迁移矩阵对照

| 旧 `bilibili_danmaku` 入口 | 本 spec 对应 | 状态 |
|---|---|---|
| `query_gifts(summary=False)` | `query_support_ledger(summary=False)` 返回脱敏明细 | 本 spec 覆盖 |
| `query_gifts(summary=True)` | `get_room_summary()` 返回聚合统计 | 本 spec 覆盖 |
| `DanmakuStorage._CREATE_GIFT` SQLite | JSON-L 文件格式 | 新设计，不复用 |
| `GiftAggregator` 窗口聚合 | 由 `SupportEventScheduler` 替代 | 已有，不在此 spec |
| `query_stats` | 交由 Dashboard Runtime Health 收敛 | 不在此 spec |

## 附录 B：文件格式示例

```
# 文件路径: data/support_ledger/v1/room_12345_001.ledger
# 第 1 行是 schema 版本标记（可选）
# 后续每行一条 JSON 记录

{"ts":1712345678.12,"et":"gift","pet":"SEND_GIFT","uid":"bilibili:12345","nick":"测试用户A","gn":"小电视","gv":1000,"vc":1.0,"gc":1,"ct":"gold","rid":12345,"tid":"trace-001","sg":1}
{"ts":1712345678.50,"et":"gift","pet":"COMBO_SEND","uid":"bilibili:12345","nick":"测试用户A","gn":"小电视","gv":1000,"vc":1.0,"gc":1,"ct":"gold","rid":12345,"tid":"trace-002","sg":1}
{"ts":1712345679.00,"et":"super_chat","pet":"SUPER_CHAT_MESSAGE","uid":"bilibili:67890","nick":"SC用户","gn":"Super Chat","gv":30000,"vc":30.0,"gc":1,"ct":"gold","st":"第一次来！主播你好","rid":12345,"tid":"trace-003","sg":1}
{"ts":1712345680.00,"et":"guard","pet":"GUARD_BUY","uid":"bilibili:11111","nick":"舰长用户","gn":"舰长","gv":198000,"vc":198.0,"gc":1,"ct":"gold","gl":3,"rid":12345,"tid":"trace-004","sg":1}
{"ts":1712345690.00,"et":"gift","pet":"SEND_GIFT","uid":"bilibili:22222","nick":"小礼物用户","gn":"辣条","gv":100,"vc":0.0,"gc":10,"ct":"silver","rid":12345,"tid":"trace-005","sg":1}
```
