# talktome-chat 用到的后端接口

排障时用。基址 `https://prod-backend.talkto.bio`（int：`https://int-backend.talkto.bio`）。
除特别说明外全部 **POST + JSON**，鉴权 `Authorization: Bearer <accessToken>`，并统一带 `x-client-source: skill`（只用于埋点归因，不参与鉴权）。
错误一律 `{"error": {"code": "...", "message": "..."}}`。

## 登录

| 接口 | 请求 | 响应 / 备注 |
| --- | --- | --- |
| `/api/auth/sms/send` | `{phone, cc}` | `{ok:true}`；`428 CAPTCHA_REQUIRED` = 风控要点选验证码（终端做不了，去 App 登一次） |
| `/api/auth/sms/verify` | `{phone, cc, code, source:"skill"}` | `{userId, accessToken, refreshToken, expiresIn, isNewUser, displayName, phone}`；没注册过的号自动建号，`source:"skill"` 是注册渠道归因 |
| `/api/auth/refresh` | `{refreshToken}` | 新的 access+refresh 对。**refresh token 一次性轮换**：旧的用第二次会触发服务端重放检测，整条 family 被吊销 → 只能重新登录。脚本用 `~/.talktome/refresh.lock` 串行化 |
| `/api/auth/logout` | `{refreshToken}` | 吊销本机这条 family |

access token 1 小时过期；refresh token 30 天滑动续期（每次轮换重置），超 30 天没用要重新短信登录。

## 分身与对话

| 接口 | 请求 | 响应 |
| --- | --- | --- |
| `/api/agents/list` | `{}` | `{agents:[{id, agent_name, handle, status, soul_content, greeting, avatar_url, ...}]}` |
| `/api/agents/chat` | `{agentId, message(1..4000), conversationId?}` | **SSE**，见下 |
| `/api/agents/chat/history` | `{agentId, conversationId, after?, limit?(1..200)}` | `{conversationId, messages:[{entryId, role, text, createdAt}], hasMore, nextAfter?}` |

`/api/agents/chat` 的 SSE 帧（`text/event-stream`，每帧一行 `data: {...}`）：

```
data: {"type":"meta","conversationId":"..."}   # 第一帧，新建会话时靠它拿 id
data: {"type":"delta","text":"..."}            # 流式正文，拼起来就是回复
data: {"type":"error","message":"..."}         # 出错（HTTP 仍是 200，流已经开了）
data: {"type":"done"}                          # 结束
```

- 不传 `conversationId` = 新开一条会话；传了 = 续聊。**服务端不存"我的会话列表"**，会话 id 由本脚本记在 `~/.talktome/state.json`，丢了就用 `--new` 重开。
- 会话属主由 xchat 的 `(agentId, userId, conversationId)` 三键保证：别人的会话 id 拿不到，返回 `404 CONVERSATION_NOT_FOUND`。
- 这条通道**不会**产生访客记录：不写 `visitors`、不进 `/api/conversations/list`、不被摘要 worker 处理、不计入 `/api/stats/overview` 的三个数字（单独记 `usage_events.kind='api_chat'`）。
- 失败**不要自动重发**——消息可能已经到达分身，重试会重复发。

## 访客线索

| 接口 | 请求 | 响应 |
| --- | --- | --- |
| `/api/conversations/list` | `{agentId?}` | `{conversations:[{id, agentId, agentName, visitorName, visitorPhone, visitorWechat, visitorEmail, roundCount, unread, lastMessageAt, summaryItems:[{id, content, acked}]}]}`；当前**返回全部**，"今天/未读"由脚本本地过滤（服务端过滤参数是 phase 2） |
| `/api/conversations/messages` | `{conversationId, after?, limit?(1..500)}` | `{conversation, messages, hasMore, nextAfter?, summaryItems}` |
| `/api/conversations/update` | `{conversationId, unread:false}` | 标记已读 |
| `/api/conversations/summary/ack` | `{itemId, acked}` | 单条摘要要点 |
| `/api/conversations/summary/ack-all` | `{conversationId}` | 整条会话的要点全标 |
| `/api/stats/overview` | `{}` | `{visitors, chats, emails}`（累计，访客口径） |
| `/api/user/profile` | `{}` | `{profile:{userId, displayName, phone, email, ...}}` |

## 错误码 → 脚本行为

| HTTP | code | 处理 |
| --- | --- | --- |
| 401 | `TOKEN_EXPIRED` / `INVALID_TOKEN` | 自动 refresh 后重试一次 |
| 401 | `INVALID_REFRESH` / `REFRESH_EXPIRED` / `REFRESH_REUSE` | 清本地凭据，退出码 41，引导重新登录 |
| 403 | `ACCOUNT_DELETED` | 账号已注销，终止 |
| 404 | `AGENT_NOT_FOUND` | 不是本人的分身 / id 打错 → 重新 `agents` 核对 |
| 404 | `CONVERSATION_NOT_FOUND` | 会话不存在或不属于这个分身；本机记的会话失效时自动重开一条 |
| 428 | `CAPTCHA_REQUIRED` | 退出码 42，请用户去 App 登一次 |
| 502 | `XCHAT_UNAVAILABLE` | 对话服务暂时不可用，提示稍后再试，不自动重发消息 |

非流式请求在 5xx / 网络故障时自动退避重试 2 次（1s、4s）；SSE 不重试。
