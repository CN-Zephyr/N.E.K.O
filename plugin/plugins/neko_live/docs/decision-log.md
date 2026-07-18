# NEKO Live 决策日志

> 记录架构、存储、产品方向等**已拍板**的决策及其理由。
> 新增条目：`### YYYY-MM-DD：决策标题`，一段背景、理由、否决方案和影响范围。

---

### 2026-07-18：存储格式选 JSON-L

**背景**：可信支持事件账本需要持久化存储。候选方案有 JSON-L 文件、SQLite、PluginStore K-V。

**决策**：采用 JSON-L 文件格式。

**理由**：
1. 无额外依赖（Python 标准库足够，不需要数据库驱动）
2. 追加写性能优于 SQLite（append-only，不需要维护索引）
3. 与现有 `viewer_store.py`（JSON 文件）技术栈一致，维护者不需要学习新存储
4. 可审计：追加日志本身就是不可变记录，可 `tail`、可 `grep`
5. 写入失败不影响实时输出（IOError 只 catch 记 audit，调度器正常跑）

**否决方案**：
- SQLite：与旧 `bilibili_danmaku` schema 相似度高易混淆，多线程连接管理复杂，违背"不直接复用旧 SQLite schema"的迁移矩阵结论
- PluginStore K-V：K-V 接口不适合有序追加日志，范围查询效率低

**影响范围**：`modules/live_support_events/` 新增 `SupportLedger` 类，不修改现有 `SupportEventScheduler`。文件路径 `data/support_ledger/v1/`。

---

### 2026-07-18：账本写入范围仅限 dispatch 成功

**背景**：支持事件从 EventBus 接收后经过 scheduler 排队、优先级比较、combo 合并，最后才 dispatch。支持事件是否写账本、何时写。

**决策**：仅在 `SupportEventScheduler._dispatch_once()` **成功完成**（没有抛异常）后写入账本。

**理由**：
1. 防浪费：排队后被高优挤掉的事件从未实际产出，不应计入
2. 防双记：dispatch 失败的事件会释放 `provider_event_id`，后续可能重入；记成功才是最终态
3. 防夸大：combo 连击过程中多次更新不应独立记账，只有 finalized 后的 dispatch 才记

**否决方案**：
- 排队时写：数据膨胀，不符合"实际产出"的消费语义
- dispatch 前写：失败后需要回滚，复杂度高

**影响范围**：`SupportEventScheduler` 新增 `on_dispatched` 可选回调参数，账本通过此回调接入。

---

### 2026-07-18：Dashboard 账本视图只在开发者模式暴露

**背景**：`get_room_summary` 和 `query_entries` 查询接口是否在普通主播面板展示。

**决策**：第一版只在**开发者模式**下显示账本视图。

**理由**：
1. 减少第一版 UI 工作量（不需要设计主播友好版的面板卡片和 i18n）
2. 先验证价值：通过开发者沙盒的实际使用确认查询接口是否满足需求
3. 不增加普通模式的面板复杂度

**否决方案**：
- 普通模式也可见：需要额外的 UI 设计和 i18n 工作，不符合"先验证价值"的原则
- 仅通过 API 暴露：开发者工具入口（plugin_entry）已足够

**影响范围**：`ui/panel.tsx` 不做账本页面；`query_support_ledger` plugin_entry 在开发者模式下可用。后续产品决策后可升级到普通面板。

---

### 2026-07-18：value_cny 换算规则

**背景**：不同直播平台使用不同的代币体系（B站 gold/silver 瓜子，抖音 dou+ 等），账本需要跨平台可比的价值字段。

**决策**：`value_cny` 在 `SupportLedger._to_entry()` 中计算，账本只存已换算结果，不维护汇率表。

| 平台 | 换算规则 |
|------|---------|
| B站 gold | `value_cny = gift_value / 1000`（1000 gold = 1 元） |
| B站 silver | `value_cny = 0.0`（免费礼物不折现） |
| B站 SC | SC 金额已标为元×1000，同上公式 |
| 其他平台 | provider 在 ingest 层传入已换算的 `value_cny` |

**理由**：
1. 账本只存不换——汇率变化由 ingest 层管理，账本不做定时重算
2. 归一化在写入时一次性完成，查询时不需要再做跨平台换算
3. 与 `viewer_store` 的设计一致：写入前已脱敏/归一化

**否决方案**：
- 账本维护汇率表：复杂度高，汇率变化需要后台定时任务
- 不做跨平台换算：无法横向比较 B站 和抖音的礼物价值

**影响范围**：`SupportLedgerRecord.value_cny: float`，写入时计算；`SupportLedgerSummary.total_cny` 为所有事件 `value_cny` 之和。

---

### 2026-07-18：旧文件清理责任归插件自主定时清理

**背景**：账本文件需要定期清理旧数据，由谁触发。

**决策**：插件使用 `@timer_interval(id="ledger_cleanup", seconds=3600)` 每小时自动清理一次。

**理由**：
1. 减少用户认知负担——用户不需要手动清理数据
2. 定时器由插件生命周期管理，`stop()` 后自动取消
3. 清理逻辑在 `SupportLedger` 内部，不暴露到外部接口

**否决方案**：
- 依赖 panel 手动清理：用户不知道需要清理，且产品体验差
- 不清理无限增长：已否决——必须设上限

**影响范围**：`LiveSupportEventsModule` 新增 `@timer_interval`，调用 `self._ledger.cleanup()`。
