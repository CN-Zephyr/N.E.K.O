# B站直播协议层：bili_live_ingest

> 维护者注：本层代码直接继承自旧 `bilibili_danmaku` 插件，是 NEKO Live 的"协议适配器"。
> B站协议变化（WS 握手、WBI 签名、-352 风控、弹幕数据格式）主要影响这一层。
> 本文档记录 2026-07-18 已知的协议版本和行为。

---

## 目录

- [协议总览](#协议总览)
- [WBI 签名算法](#wbi-签名算法)
- [buvid3 获取与使用](#buvid3-获取与使用)
- [WebSocket 连接与弹幕协议](#websocket-连接与弹幕协议)
- [认证流程](#认证流程)
- [心跳](#心跳)
- [重连机制与 Generation 锁](#重连机制与-generation-锁)
- [-352 风控降级策略](#-352-风控降级策略)
- [LiveEvent 信封](#liveevent-信封)
- [头像抓取与身份解析](#头像抓取与身份解析)
- [事件支持元数据](#事件支持元数据)
- [旧插件保留 vs 新增](#旧插件保留-vs-新增)

---

## 协议总览

B站直播弹幕走 WebSocket 协议，端点固定为 `wss://broadcastlv.chat.bilibili.com/sub`。

数据流：

```
DanmakuListener (danmaku_core.py)
  └─ WebSocket binary frames
      └─ _process_packet → _dispatch_message
          ├─ on_event(cmd, LiveDanmaku)     ← 富模型事件（主路径）
          ├─ on_danmaku/on_gift/on_sc      ← 旧字典回调（deprecated）
          └─ on_live/on_preparing/on_error  ← 连接生命周期

on_event → BiliLiveIngestModule._on_live_event
  └─ _to_live_event(cmd, event) → LiveEvent
      └─ bus.publish(type, LiveEvent)
          ├─ live_events     ← "danmaku" (普通弹幕)
          └─ live_support_events ← "gift"/"super_chat"/"guard"
```

---

## WBI 签名算法

### 用途

调用 `getDanmuInfo`（弹幕服务器信息）时必须带 WBI 签名，否则部分房间会返回 `-352` 风控。

### 算法

```
1. 从 https://api.bilibili.com/x/web-interface/nav 的
   wbi_img.img_url / sub_url 取文件名（去掉扩展名）：
     img_key = img_url.rsplit("/",1)[-1].split(".")[0]
     sub_key = sub_url.rsplit("/",1)[-1].split(".")[0]

2. 拼合后按重排表取前 32 位：
     raw = img_key + sub_key        # 总长 64
     mixin_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]

3. 签名请求：
     params = {"id": room_id, "type": 0}
     params["wts"] = int(time.time())
     filtered = {k: v去除!'()* for k,v in sorted(params.items())}
     query = urlencode(filtered)
     w_rid = md5(query + mixin_key).hexdigest()
     params["w_rid"] = w_rid
```

### 重排表（固定不变）

```python
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
```

### 缓存策略

- **TTL**: 12 小时（`_wbi_key_ttl = 43200` 秒）
- **条件触发**：只有 `img_key` 和 `sub_key` 都非空且长度为 32 时才更新
- **失败行为**：获取失败时跳过签名，以无签名请求重试一次；如果仍然失败则走 fallback 服务器列表

### 已知风险

- B站可能更改 WBI 页面字段名（`wbi_img` → 其他 key），需要跟进 `x/web-interface/nav` 接口变化
- 两个 key 格式可能从文件名改为其他形态（目前是 `xxx.png` 取 `xxx`）

---

## buvid3 获取与使用

### buvid3 是什么

B站用于标记匿名浏览器的 cookie。风控系统通过 buvid3 识别是否为"真人浏览器"。
无 buvid3 或 buvid3 过期时，弹幕连接和 HTTP API 都会返回 `-352`。

### 获取流程

```
1. 首选：从 credential（扫码登录）中直接读取 buvid3
2. 次选：访问 https://www.bilibili.com，从 Set-Cookie 中提取 buvid3
3. 备用：从响应头 raw Set-Cookie 里逐行匹配 "buvid3="
```

获取到的 buvid3 会回写入 `credential.buvid3`（若有），并保存在 `_buvid3_temp` 供认证包使用。

### 使用位置

| 位置 | 用途 | 文件 |
|---|---|---|
| WebSocket 认证包 | `body.buvid` + `body.buvid3`（两字段同时写入，兼容新旧协议） | `danmaku_core.py:_build_auth_body()` |
| HTTP 查询头 | `Cookie: buvid3=xxx` | `danmaku_core.py:_get_danmaku_server_info()`, `BiliLiveIngestModule._do_room_lookup()` |
| 登录 credential | 扫码登录后从 Fernet 加密存储中读取 | `adapters/bili_auth_service.py` |

### buvid3 vs 登录态

| 状态 | 行为 | 风控效果 |
|---|---|---|
| 未登录 + 无 buvid3 | 所有 API 返回 -352 | ❌ 全部失败 |
| 未登录 + 临时 buvid3 | 部分 API 可用（弹幕 WS 可能通） | ⚠️ 看 IP 权重 |
| 已登录（无 buvid3） | SESSDATA 自动关联 buvid3 | ✅ 最高 |
| 已登录 + buvid3 | 全链路无风控 | ✅✅ |

---

## WebSocket 连接与弹幕协议

### 协议地址

- 主地址：`wss://broadcastlv.chat.bilibili.com/sub`
- 备用列表（按可靠性排序）：
  - `wss://tx-gz-live-comet-01.chat.bilibili.com/sub`
  - `wss://live-comet-01.chat.bilibili.com/sub`
  - `wss://live-comet-02.chat.bilibili.com/sub`
  - `wss://broadcastlv.chat.bilibili.com/sub`（最终保底）

连接时会先调用 `getDanmuInfo` API 获取 B站 返回的 host_list，再以此列表尝试连接。
始终追加 `broadcastlv` 作为最终保底（去重后）。

### 数据包格式

```
header (16 字节) + body
```

**header** (`struct >IHHII`):

| 偏移 | 大小 | 字段 | 说明 |
|------|------|------|------|
| 0 | 4 | total_len | 整包长度（含 header） |
| 4 | 2 | header_len | header 长度（固定 16） |
| 6 | 2 | proto_ver | 协议版本（见下） |
| 8 | 4 | operation | 操作码（见下） |
| 12 | 4 | seq | 序列号（固定 1） |

### 协议版本 (`proto_ver`)

| 值 | 常量 | 含义 |
|----|------|------|
| 0 | `JSON` | JSON 明文 |
| 1 | `HEARTBEAT` | 心跳 / 认证 |
| 2 | `ZLIB` | zlib 压缩（当前默认，兼容性最好） |
| 3 | `BROTLI` | brotli 压缩（性能更好，但需要 `brotli` 库） |

### 操作码 (`operation`)

| 值 | 常量 | 方向 | 用途 |
|----|------|------|------|
| 2 | `HEARTBEAT` | Client→Server | 心跳 |
| 3 | `HEARTBEAT_REPLY` | Server→Client | 心跳回复（含人气值） |
| 5 | `SEND_MSG` | Server→Client | 普通消息（弹幕等） |
| 7 | `AUTH` | Client→Server | 认证 |
| 8 | `AUTH_REPLY` | Server→Client | 认证回复 |

### 压缩处理

收到 `proto_ver=2`（zlib）或 `3`（brotli）时，先解压再递归拆包。
单个 zlib/brotli 包中可能包含多个子包，用 `_split_packets()` 拆分。

brotli 库是可选依赖：未安装时跳过 brotli 包（不影响 zlib 压缩包）。

### 支持的 CMD 列表

#### 基础指令（直接分发）

| CMD | 事件回调 | 说明 |
|-----|---------|------|
| `DANMU_MSG` | `on_event("DANMU_MSG", LiveDanmaku)` | 普通弹幕。**主入口** |
| `SEND_GIFT` | `on_event("SEND_GIFT", LiveDanmaku)` | 礼物。fallback 字典 + 回调 |
| `SUPER_CHAT_MESSAGE` | `on_event("SUPER_CHAT_MESSAGE", LiveDanmaku)` | SC |
| `INTERACT_WORD` | `on_event("INTERACT_WORD", LiveDanmaku)` | 进场/关注 |
| `LIVE` | `on_live()` | 开播 |
| `PREPARING` | `on_preparing()` | 下播。设置 `_live_ended=True` 阻止重连 |

#### 增强指令（`_CMD_HANDLERS` 分发）

| CMD | 工厂方法 | 说明 |
|-----|---------|------|
| `GUARD_BUY` | `from_guard_buy()` | 上舰（大航海） |
| `ENTRY_EFFECT` | `from_entry_effect()` | 高能用户进场特效 |
| `COMBO_SEND` | `_handle_combo_send()` | 礼物连击 |
| `LIKE_INFO_V3_CLICK` | `from_like()` | 点赞 |
| `ONLINE_RANK_V2` | `from_online_rank()` | 高能榜更新 |
| `ONLINE_RANK_TOP3` | `from_online_rank()` | 高能榜前三 |
| `NOTICE_MSG` | `from_notice()` | 公告（运营/系统） |
| `ANCHOR_LOT_START` | `from_anchor_lot()` | 天选时刻开始 |
| `ANCHOR_LOT_END` | `from_anchor_lot()` | 天选时刻结束 |
| `ROOM_BLOCK_MSG` | `from_block()` | 禁言 |
| `WATCHED_CHANGE` | `from_watched_change()` | 看过人数变化 |
| `ROOM_REAL_TIME_MESSAGE_UPDATE` | `_handle_room_update()` | 直播间实时数据 |
| `ROOM_CHANGE` | `_handle_room_change()` | 直播间信息变更 |
| `SUPER_CHAT_MESSAGE_JPN` | `from_sc()` | 日文 SC（同 SC handler） |
| `USER_TOAST_MSG` | `_trusted_user_toast_gift_payload()` | 上舰 toast（转为 SEND_GIFT 事件） |

#### 增强协议中注意的坑

- **USER_TOAST_MSG**: 可能包含粉丝团灯牌激活文本（"赠送粉丝团灯牌"），必须与真实礼物区分。当前通过 `_looks_like_fans_medal_activation_text()` 过滤
- **COMBO_SEND**: 礼物连击的 `combo_end` 标记用于区分"连击进行中"和"连击结束"，下游 `live_support_events` 使用连击状态机
- **cmd 可能带后缀**: 某些 cmd 格式为 `DANMU_MSG:4` 或 `LIVE:0`，统一用 `split(":")[0]` 取前部

### DANMU_MSG 数据格式（info 数组）

```python
# info 字段索引（关键下标）：
info[0]  → 弹幕元数据数组 [弹幕类型, 字体大小, 颜色, 发送时间戳(ms), ...]
info[1]  → 弹幕文本（str）
info[2]  → 用户数组 [uid, uname, is_admin, is_vip, is_svip, ...]
info[3]  → 粉丝牌数组 [level, name, up_name, room_id, color, ...] 或空
info[4]  → 用户等级数组 [user_level, ...]
info[7]  → 大航海等级（int：0=无, 1=总督, 2=提督, 3=舰长）
```

**历史 bug**: 旧插件把 `info[7]` 当作 list 下标（`info[7][3]` / `info[7][1]`），但它实际是 int。
这导致任意一条正常弹幕都触发 `TypeError`，被 `except` 吞掉后 `on_event("DANMU_MSG")` 永不触发。
已修复为 `int(guard_raw)` + 对可能的 list 形态做一次兜底。

---

## 认证流程

```
1. 连接 WebSocket → wss://host:port/sub
2. 发送认证包（operation=7）：
   {
     "uid": 0,                      # 未登录时=0
     "roomid": real_room_id,
     "protover": 2,                 # zlib（兼容性最好）
     "platform": "web",
     "type": 2,
     "key": token,                  # 来自 getDanmuInfo
     "buvid": buvid3,               # 新版字段
     "buvid3": buvid3               # 旧版字段
   }
3. 服务器回复（operation=8）：
   {"code": 0}   → 成功，开始接收弹幕
   {"code": -1}  → 失败，断开连接
```

认证包中 `buvid` / `buvid3` 两字段同时发送，兼容新旧协议。

---

## 心跳

- **间隔**: 30 秒
- **操作码**: `OPERATION_HEARTBEAT = 2`
- **负载**: `[object Object]`（固定文本）
- **心跳回复**: `operation=3`，前 4 字节为大端 int（人气值）
- **停止方式**: `_stop_event` 中断 + `CancelledError` 协程取消
- **异常处理**: send 失败时跳出循环（交给 `_connect_once` 的异常处理触发重连）

---

## 重连机制与 Generation 锁

### 重连逻辑（`DanmakuListener.start()`）

```python
max_retries = 10
retry_delay = 5  # 初始

while True:
    await _connect_once()
    if _stop_event: break
    if _live_ended: break     # PREPARING 后不再重连

    # 曾成功认证 → 重置重连计数（稳定连接断开算一次 clean）
    if _authenticated_in_attempt: retry_count = 0

    retry_count += 1
    if retry_count > max_retries:  # 停止
        break

    wait = min(retry_delay * retry_count, 60)  # 指数退避，最大 60s
    sleep(wait)
```

- 前 3 次打印日志，第 4 次起静默到第 10 次失败
- 收到 `PREPARING`（下播）后 `_live_ended=True`，立即停止重连
- `stop()` 先置 `running=False` + `_stop_event.set()`，再 `cancel` 心跳 + `close` ws

### Generation 锁（`BiliLiveIngestModule`）

```python
_listener_generation += 1   # 每次 start_listening 递增
```

用途：**防止旧 listener 的回调污染新连接**。

当用户手动停止并重新开始监听时，旧 listener 的回调可能晚于新 listener 到达。
`_on_live_event` 检查 `generation == self._listener_generation`，不匹配则静默丢弃。

作用域：
- `_on_live_event()` — 弹幕/事件回调
- `_on_live()` — 开播回调
- `_on_preparing()` — 下播回调
- `_on_error()` — 错误回调
- `_listener_task_done()` — 任务完成回调

### 连接态 `_owns_current_live_session()`

阻止跨平台/跨房间的事件流入当前 runtime：

```python
def _owns_current_live_session(self):
    if _stopping: return False
    router = ctx.live_provider
    if router.platform != "bilibili": return False
    if router.provider_for("bilibili") is not self: return False
    if router.configured_room_ref() != str(self._room_id): return False
    return True
```

---

## -352 风控降级策略

-352 是 B站 的反爬风控码，出现在：

1. **HTTP API 查询**（`getInfoByRoom`, `getDanmuInfo`）— 匿名请求
2. **弹幕 WebSocket 认证** — buvid3 过期/无效

### 降级策略

| 层面 | 措施 | 文件位置 |
|------|------|---------|
| **buvid3** | 无凭据时访问 B站首页拿临时 buvid3；认证包同时写 `buvid` + `buvid3` | `danmaku_core.py:_fetch_buvid3()`, `_build_auth_body()` |
| **WBI 签名** | `getDanmuInfo` 请求带 WBI 签名（`w_rid` + `wts`）；签名获取失败时跳过 | `danmaku_core.py:_get_danmaku_server_info()` |
| **room lookup** | ① 带临时 buvid3 → ② 撞 -352 后强制刷新 buvid3 重试一次 → ③ 仍失败则报友好错误 | `BiliLiveIngestModule._lookup_room_status_sync()` |
| **room lookup 登录态** | 已扫码登录时用完整登录 cookie（SESSDATA+bili_jct+DedeUserID+buvid3），最稳 | `BiliLiveIngestModule._credential_cookie()` |
| **弹幕 WS** | 多服务器 + 指数退避重连，综合缓解临时风控 | `DanmakuListener.start()` |
| **友好提示** | `-352` 返回"B站风控校验失败（-352）：匿名查询被反爬拦截..." | `BiliLiveIngestModule._friendly_lookup_message()` |

### 边界条件

- **查询失败 ≠ 监听失败**：lookup 走 HTTP (`getInfoByRoom`) 可能 -352，但 WS 弹幕可能通（因为 buvid3 有效）。所以 lookup 失败时仍允许连接直播
- **重试一次**：强制刷新 buvid3 只做一次，不硬刷加重风控
- **登录根治**：扫码登录后 -352 彻底消失（已验证：真机 1408555810 登录前 -352 → 登录后恢复）

---

## LiveEvent 信封

定义在 `core/contracts_events.py`。这是从 `bili_live_ingest` 向上层（EventBus → pipeline）的路由信封。

```python
@dataclass
class LiveEvent:
    type: str            # 路由键："danmaku"/"gift"/"super_chat"/"guard"/"entry"
    uid: str             # 字符串 UID（平台前缀化）
    payload: dict        # 公开安全字段（已脱敏）
    source: str          # 固定 "live"
    ts: float            # 时间戳
    schema_version: int  # 1
    raw: Any             # 富模型 LiveDanmaku 或 dict fallback
    session_generation: int  # 会话代际（用于跨代丢弃）
```

### CMD → TYPE 映射

```python
{
    "DANMU_MSG": "danmaku",
    "SEND_GIFT": "gift",
    "COMBO_SEND": "gift",
    "SUPER_CHAT_MESSAGE": "super_chat",
    "SUPER_CHAT_MESSAGE_JPN": "super_chat",
    "GUARD_BUY": "guard",
    "INTERACT_WORD": "entry",
}
```

未列出的 cmd 回落为 `cmd.lower()`。

### ViewerEvent（下游 View）

```python
@dataclass
class ViewerEvent:
    uid: str
    nickname: str
    avatar_url: str
    danmaku_text: str
    target_lanlan: str
    source: TriggerSource   # "live_danmaku" / "developer_sandbox"
    live_mode: LiveMode     # "co_stream" / "solo_stream"
    trace_id: str
    seen_at: str            # ISO 时间
    raw: dict               # 原始 payload（脱敏版）
```

---

## 头像抓取与身份解析

`BiliIdentityModule` (`modules/bili_identity/__init__.py`) 负责两件事：

### 1. 用户资料查询

```python
# 通过 bilibili_api.user.User.get_user_info() 获取
{
    "mid":  UID,
    "name": 昵称,
    "face": 头像 URL,
    "pendant": 挂件/装扮名,    # 出框头像的来源
    "email": 邮箱（非公开时可能为空）
}
```

登录态影响：
- **已登录**：走登录 session（`credential=bili_credential`），根治 -352
- **未登录**：匿名查询，可能 -352（不影响 AI 输出，只是缺头像）

### 2. 头像抓取

使用**自定义 HTTP 客户端**（非 aiohttp），包含 SSRF 防护：

```python
1. URL -> socket.getaddrinfo -> 解析 IP
2. IP 检查：排除 private/loopback/link-local/multicast/reserved
3. 建立 TCP 连接 -> HTTP GET（Host/Referer/User-Agent 伪装浏览器）
4. PIL 验证：Image.open → 确认可解码
5. 失败处理：decode 失败 → avatar_vision_ok=False（AI 看不到头像，只描述昵称/元信息）
```

缓存：`ctx.avatar_cache` (LRU, avatar URL → (bytes, mime))

---

## 事件支持元数据

支持事件（gift/super_chat/guard）在 `LiveDanmaku` 富模型上附带以下字段：

```python
provider_event_id: str       # 提供方事件 ID（去重用）
provider_timestamp_ms: int   # 提供方时间戳（ms）
combo_id: str                # 连击 ID
combo_count: int             # 连击数
combo_end: Optional[bool]    # True=连击结束
```

写入时机（`_apply_support_metadata`）：
- `SEND_GIFT` 原始包
- `COMBO_SEND` 连击包
- `SUPER_CHAT_MESSAGE` / `SUPER_CHAT_MESSAGE_JPN`
- `GUARD_BUY`
- `USER_TOAST_MSG`（转为 SEND_GIFT 后）

### 支持事件去重（ingest 层）

```python
SUPPORT_EVENT_DEDUPE_SECONDS = 0.35  # 350ms 窗口
SUPPORT_EVENT_DEDUPE_LIMIT = 4096    # 最大缓存条数
```

用 `OrderedDict` 做 LRU 去重缓存，key 构造：

| 事件类型 | key 构造 |
|---------|---------|
| COMBO_SEND | `provider_combo|provider_event_id|combo_id|combo_count|gift_value|end/progress` |
| 其他 gift/guard/SC | `provider|provider_event_id` |
| SC（无 provider_event_id） | `super_chat|cmd|uid|text` |
| 其他（无 provider_event_id） | `type|cmd|uid|gift_name|count|value` |

---

## 旧插件保留 vs 新增

### 从旧 `bilibili_danmaku` 保留

| 组件 | 位置 | 改动 |
|------|------|------|
| `DanmakuListener` 类 | `danmaku_core.py` | 整体保留，增补增强事件 handler |
| `LiveDanmaku` 数据类 + 工厂方法 | `livedanmaku.py` | 保留，修复 `info[7]` 类型 bug |
| WBI 签名（`_mixin_key` / `_wbi_sign`） | `danmaku_core.py` | 未改动 |
| WS 协议（header 结构/操作码/压缩） | `danmaku_core.py` | 未改动 |
| 心跳/重连逻辑 | `danmaku_core.py` | 保留，增补 `_listener_log` 收敛 |
| 扫码登录移植 | `adapters/bili_auth_service.py` | 从旧插件独立迁入 |

### NEKO Live 新增

| 新增内容 | 位置 | 说明 |
|---------|------|------|
| `_ListenerLog` | `__init__.py` | audit-only 日志，info/debug 丢弃（防隐私泄漏） |
| Generation 锁 | `__init__.py` | `_listener_generation` 防跨代事件污染 |
| 支持事件去重 | `__init__.py` | 350ms 窗口 + OrderedDict LRU |
| `_CMD_HANDLERS` 增强指令表 | `danmaku_core.py` | GUARD_BUY/COMBO_SEND/ENTRY_EFFECT 等 13 种 |
| `_apply_support_metadata()` | `danmaku_core.py` | provider_event_id/combo 等支持调度字段 |
| `_trusted_user_toast_gift_payload()` | `danmaku_core.py` | CMD 白名单有限的 USER_TOAST_MSG 解析 |
| 粉丝团灯牌检测 | `danmaku_core.py` | `_looks_like_fans_medal_*()` 过滤误报 |
| LiveEvent 信封 + EventBus 路由 | `core/contracts_events.py`, `core/event_bus.py` | 统一事件模型 |
| `_owns_current_live_session()` | `__init__.py` | 跨平台/跨房间事件隔离 |
| `_BROWSER_HEADERS` + buvid3 分离 | `__init__.py` | lookup 层独立管理 buvid3 |
| `_friendly_lookup_message()` | `__init__.py` | B站错误码→中文友好提示 |
| `BiliIdentityModule` 自定义 HTTP | `modules/bili_identity/` | DNS + IP 验证 + PIL 解码，非 aiohttp |
| `LiveRoomStatus` + 60s 缓存 | `__init__.py` | lookup 结果短期缓存防刷 |
| 头像缓存 LRU | `ctx.avatar_cache` | URL → (bytes, mime) 避免重复抓取 |
| 测试锁只读边界 | `tests/` | `test_bili_live_ingest_stays_readonly` |

### 已删除/废弃

| 旧能力 | 原因 |
|--------|------|
| `send_danmaku()` | 发弹幕属于写操作，neko_live 只读 ingest |
| `buvid3` 以外的 cookie 操作 | 凭据只走 `credential_store.py` |
| 旧 LLM/orchestrator/memory | neko_live 走统一 pipeline + dispatcher |
| `ws_bridge.py` | 外部客户端桥，无分发依赖者 |
| 旧独立 dashboard (static/index.html) | 被 Hosted UI 面板替代 |

---

## 维护清单

B站协议变化时需检查的点：

1. **WBI 签名**：`x/web-interface/nav` 的 `wbi_img` key 是否改名或移除
2. **buvid3**：Set-Cookie 域名/路径是否变化；buvid3 是否被其他 cookie 替代
3. **弹幕协议**：header 结构、operation 码、compression 类型变化
4. **DANMU_MSG info 数组**：下标偏移或字段类型变化（`info[7]` 的历史教训）
5. **-352 风控**：是否有新的反爬策略（如 WebSocket 握手增加签名要求）
6. **getDanmuInfo**：返回的 host_list 格式、token 长度限制变化
7. **认证包**：`protover` 推荐值、是否新增必填字段
8. **SC/Guard/礼物**：JSON 字段名变化（B站经常微调 payload 结构）
