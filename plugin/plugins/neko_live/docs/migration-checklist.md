# 迁移执行检查清单

> 更新日期：2026-07-18
> 对应：`docs/bilibili-danmaku-migration-matrix.md`（旧插件 47 个入口的逐项决策）
> 说明：本文跟踪迁移的执行进度。决策依据和详细分类见迁移矩阵，本文只记录"做了还是没做"。

---

## ✅ 已完成

### 直播主链路（已由 NEKO Live 替代，14 项）

- [x] 直播间连接 / 断开（`connect_live_room` / `disconnect_live_room`）
- [x] 直播状态查询（`lookup_live_room`）
- [x] 弹幕实时监听 ingest（`modules/bili_live_ingest/`）
- [x] WBI 签名 + buvid3 反 -352
- [x] 扫码登录 + 加密凭据存储（`adapters/bili_auth_service.py` + `credential_store`）
- [x] 登录检测 / 退出登录（`bili_login_status` / `bili_logout`）
- [x] 首次出场头像锐评（`avatar_roast`）
- [x] 后续弹幕接话（`danmaku_response`）
- [x] 礼物 / SC / Guard 短句致谢（`live_support_events`）
- [x] 观众档案（`viewer_store`）
- [x] 安全门 / 限流 / 急停（`safety_guard`）
- [x] 开发者沙盒 / 调试入口（`developer_sandbox`）
- [x] 直播间状态面板（Hosted UI dashboard）
- [x] 主动营业 / 冷场陪播 / 开场暖场（`active_engagement` / `idle_hosting` / `warmup_hosting`）

### 明确废弃（12 项）

- [x] 旧 LLM 指导系统（`llm_client.py` / `orchestrator.py` / `background_llm_agent.py` / `intelligence_card.py`）
- [x] 旧配置指导编辑（`get_guidance_config` / `update_guidance_config` / `test_guidance`）
- [x] 旧独立 LLM 配置（`get_bg_llm_config` / `test_bg_llm`）
- [x] 旧 SQLite DanmakuStorage（原始弹幕/进场/关注/排行）
- [x] 旧 HTTP API（`http_api.py` 的 netProxy / 事件注入 / status/ping）
- [x] 旧外部客户端桥（`ws_bridge.py`）
- [x] 旧独立 Dashboard（`static/index.html`）
- [x] `ask_neko_*` 复合写入入口（`ask_neko_bili_reply` / `ask_neko_bili_send_dynamic` / `ask_neko_bili_send_message` / `ask_neko_send_danmaku`）
- [x] `query_danmaku` / `query_interact`（弹幕/进场历史查询）
- [x] `send_danmaku` 写操作
- [x] `save_credential` 明文凭据写入口
- [x] 不维护第二套 LLM 配置

---

## 🚧 进行中

### 可信支持事件账本

| 入口 | 状态 | 对应文档 / PR |
|------|------|--------------|
| `query_gifts`（吸收） | Spec 审批中 | `docs/modules/support_event_ledger.md` |
| `query_stats`（NEKO Live 替代） | Dashboard Runtime Health 已覆盖 | — |

---

## ⏭ 已记录，延期

### 主播账号身份保护

- 迁移矩阵标记：应吸收，但已**延期**
- 当前决策：只从已验证登录 UID 派生，要求显式确认，不新增自由文本姓名匹配
- 对应文档：`docs/development.md`「延期能力：主播账号身份保护」
- 触发条件：维护者重新评审后从 `main` 建独立分支

---

## ⏸ 待启动：独立插件

以下 19 个旧入口应拆出独立插件（`bili_content_tools` + `bili_write_tools`），不作为 NEKO Live 的一部分。仅当维护者确认仍需要这些能力时才启动。

### 只读内容工具（bili_content_tools）

| 入口 | 说明 |
|------|------|
| `bili_search` | 通用视频搜索 |
| `bili_hot_videos` | 通用热门视频 |
| `bili_hot_buzzwords` | 通用热词 |
| `bili_weekly_hot` | 每周热门 |
| `bili_rank` | 分区排行 |
| `bili_video_info` | 视频详情 |
| `bili_comments` | 评论读取 |
| `bili_subtitle` | 字幕读取 |
| `bili_danmaku` | 录播弹幕（非直播） |
| `bili_user_info` | 用户信息 |
| `bili_user_videos` | 用户投稿 |
| `bili_favorite_lists` | 收藏夹列表 |
| `bili_favorite_content` | 收藏夹内容 |

### 写操作工具（bili_write_tools）

| 入口 | 说明 |
|------|------|
| `bili_reply` | 评论/回复 |
| `bili_send_dynamic` | 发动态 |
| `bili_send_message` | 私信 |
| `bili_list_tools` | 工具目录 |
| `send_danmaku` | 直播发弹幕 |
| `set_danmaku_max_length` | 弹幕长度配置 |

---

## 📋 退役门槛

删除旧 `bilibili_danmaku` 目录前必须满足：

- [ ] 47 个入口均有稳定决策，没有未分类项
- [ ] 所有"应吸收"项已实现并验证，或由维护者明确取消
- [ ] 独立插件项已迁移，或明确决定不再维护
- [ ] NEKO Live 与旧插件不再需要同房并行加载
- [ ] 删除 PR 从最新 `main` 创建，不包含新功能、不堆叠未合并 PR
- [ ] 删除后完整插件测试、CLI check、文档链接和分发构建检查通过

---

## 参考

- [迁移矩阵](bilibili-danmaku-migration-matrix.md) — 47 个入口的逐项决策
- [支持事件账本 Spec](modules/support_event_ledger.md) — 可信支持事件账本设计
- [开发文档](development.md) — 架构规范与模块边界
