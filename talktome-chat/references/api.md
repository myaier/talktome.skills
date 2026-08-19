# talktome-chat 用到的后端接口

排障时用。基址 `https://prod-backend.talkto.bio`（int：`https://int-backend.talkto.bio`）。
除特别说明外全部 **POST + JSON**；登录态接口带 `Authorization: Bearer <accessToken>`。
所有请求统一带 `x-client-source: skill`——用于埋点归因，以及把会话标记成 `visitor_agent`
（引擎侧据此把 agent 来客与人类访客分开；**分身的行为和 prompt 完全一样**，不会因为对面是程序就换个口吻）。
它**不参与任何鉴权**。
错误一律 `{"error": {"code": "...", "message": "..."}}`。

## 一、找分身

| 接口 | 请求 | 响应 |
| --- | --- | --- |
| `/api/public/agents/search` | `{query(≤500字), limit?(1..20), minSimilarity?(0..1)}` | `{agents:[{agentId, handle, name, greeting, soulExcerpt, avatarUrl, homepage, similarity}]}` |

- 接口本身**免登录**，但带上有效 token 时会**排除调用者自己的分身**（自己跟自己聊没意义）——
  所以技能的流程是先登录再检索，见 SKILL.md。
- 匹配走语义向量：分身的「名字 / 开场白 / 人物描述」三段分区文本在入库时算成 1024 维向量（千问 text-embedding-v3，由 xchat 算），检索时把用户需求也算成向量，按余弦相似度召回（pgvector）。
- 只召回 `status=published` 且有 handle、主人账号未注销的分身。
- `minSimilarity` 默认 0.5。实测口径：**对口的分身 0.63~0.77，库里没有对口的最高才 0.50**——所以返回空是正常且有意义的答案，别硬塞。
- 502 `SEARCH_UNAVAILABLE` = 服务端算不出向量。**几乎总是配置问题**（`EMBEDDING_API_KEY` 没配、
  或 pod 出不了公网连不到模型端点），不是瞬时故障——重试无用，看 backend 日志的 `[agent-search]` 一行。

## 二、跟 agent 聊（A2A 协议）

对话走 A2A，**对方是不是 TalkToMe 的分身都一样**：先拿 agent card，再按卡里声明的 `url` 发 JSON-RPC。
这也是这个技能能跟任意第三方 agent 说话的原因。

### 1. 拿卡

| 目标 | 卡在哪 |
| --- | --- |
| TalkToMe 分身 | `https://talkto.bio/{handle}/.well-known/agent-card.json` |
| 外部 agent | `{origin}/.well-known/agent-card.json`，拿不到再退到旧路径 `/.well-known/agent.json` |

卡里真正要读的三个字段：

- **`url`** —— 对话端点。**只认它，不猜路径**。实测的 A2A 网络里 `/a2a`、`/api/a2a`、`/` 都有人用。
- `capabilities.streaming` —— 决定用 `message/stream` 还是 `message/send`。
- `securitySchemes` —— 存在就说明对方要鉴权，我们没有它的凭据，聊不了。

⚠️ 拿到 HTML 而不是 JSON 是常见故障：站点把未知路径兜回了 SPA（HTTP 200 + 一坨 HTML）。脚本会明确报
「这个地址上没有 agent card」，不要当成 JSON 解析失败去排查对方的 agent。

### 2. 发一轮

`POST {card.url}`，JSON-RPC 2.0：

```jsonc
{"jsonrpc":"2.0","id":"...","method":"message/stream",   // 或 message/send
 "params":{"message":{"kind":"message","role":"user","messageId":"...",
                      "contextId":"<续聊时带上>",
                      "parts":[{"kind":"text","text":"..."}]}}}
```

流式响应是 SSE，每帧一个 JSON-RPC 响应对象，`result` 里是事件：

```
data: {"result":{"kind":"task","id":"...","contextId":"...","status":{"state":"working"}}}
data: {"result":{"kind":"artifact-update","artifact":{"parts":[{"kind":"text","text":"..."}]},"append":true}}
data: {"result":{"kind":"status-update","status":{"state":"completed",...},"final":true}}
```

非流式则一次返回一个 Task。**正文位置有三种写法都要认**：`status.message.parts[]`、`artifacts[].parts[]`、
或者直接返回一条 `kind:"message"`。

### 3. 会话延续 = `contextId`

首轮不带，服务端签发；之后每轮带上同一个就是接着聊。**脚本按 endpoint 记在本机**（不是按 handle：同一个
handle 在不同环境是不同的对话）。

TalkToMe 侧的实现细节：contextId 由服务端签发（32 字节随机），**客户端自己编的不会被采纳**——传一个我们
没签发过的，会开一条新会话并回一个新 id，而不是报错。所以永远以返回的 `contextId` 为准。

### 4. 任务终态

| state | 含义 | 脚本行为 |
| --- | --- | --- |
| `completed` | 正常答完 | 正常返回 |
| `auth-required` | 要先注册/登录才能继续 | 退出码 41 |
| `input-required` | 对方在等你补充 | 正常返回并提示可以接着说 |
| `failed` / `rejected` / `canceled` | 这轮没成 | 报错退出 |

HTTP 层还有一个：**429 = 对方限流**，响应头 `Retry-After` 给可重试时间。TalkToMe 这边是每个分身每天
100 轮的 A2A 上限（JSON-RPC error code `-32000`）。别原地重试。

### 5. 身份与凭据边界

- **TalkToMe 分身**：带上 `Authorization: Bearer <accessToken>`（可选，卡里 `securitySchemes.talktomeUser`
  声明了它）。服务端凭它做三件事：解开 5 轮门控、把这条会话的访客行绑到账号、把账号手机号写给对方主人
  当联系方式。不带就是匿名——能聊 5 轮，但主人只看到「某个 agent 问过」，联系不上。
  匿名调用还受每分身每天 100 轮的上限；登录后不受该上限。
- **登录 token 只发给 talkto.bio / prod-backend.talkto.bio 这两个 origin**（`is_talktome_origin()`）。
  一个"通用" A2A 客户端如果给每个 endpoint 都带 Authorization，等于把用户凭据交给他聊过的每个陌生 agent。
- 失败**不要自动重发**——消息可能已经到达对面。

### 6. 读历史

A2A 没有"读历史"这个方法（`tasks/get` 只针对未完成的任务，TalkToMe 这边完成即丢）。所以 `transcript`
读的是**本机记录**：每轮的发送与回复存在 `~/.talktome/state.json`，换台机器就没有了。

## 三、登录

| 接口 | 请求 | 响应 / 备注 |
| --- | --- | --- |
| `/api/auth/sms/send` | `{phone, cc}` | `{ok:true}`；`428 CAPTCHA_REQUIRED` = 风控要点选验证码（终端做不了，去 App 登一次） |
| `/api/auth/sms/verify` | `{phone, cc, code, source:"skill"}` | `{userId, accessToken, refreshToken, expiresIn, isNewUser, displayName, phone}`；没注册过的号自动建号 |
| `/api/auth/refresh` | `{refreshToken}` | 新的 access+refresh 对。**refresh token 一次性轮换**：旧的用第二次会触发重放检测，整条 family 被吊销 → 只能重新登录。脚本用 `~/.talktome/refresh.lock` 串行化 |
| `/api/auth/logout` | `{refreshToken}` | 吊销本机这条 family |

access token 1 小时过期；refresh token 30 天滑动续期（每次轮换重置）。

## 四、其它

| 接口 | 请求 | 响应 |
| --- | --- | --- |
| `/api/user/profile` | `{}` | `{profile:{userId, displayName, phone, ...}}`（`whoami`） |

> 「看自己分身收到了哪些访客线索」不在这个技能范围内（那是 App 信息页的事）。相关接口
> `/api/conversations/*`、`/api/stats/overview` 都还在，将来要做单独的技能可以直接用。

## 错误码 → 脚本行为

| HTTP | code | 处理 |
| --- | --- | --- |
| 401 | `TOKEN_EXPIRED` / `INVALID_TOKEN` | 自动 refresh 后重试一次 |
| 401 | `INVALID_REFRESH` / `REFRESH_EXPIRED` / `REFRESH_REUSE` | 清本地凭据，退出码 41，引导重新登录 |
| 401 | `UNAUTHORIZED`（bind） | 没登录就想聊 → 退出码 41 |
| 403 | `ACCOUNT_DELETED` | 账号已注销，终止 |
| 404 | `AGENT_NOT_FOUND` | handle 打错 / 该分身已下线 → 重新 `find` |
| 404 | `CONVERSATION_NOT_FOUND` | 会话不存在或不属于你 → 用 `--new` 重开 |
| 428 | `CAPTCHA_REQUIRED` | 退出码 42，请用户去 App 登一次 |
| 502 | `SEARCH_UNAVAILABLE` | 服务端配置/依赖问题，**别重试**，告诉用户联系管理员 |
| 502 | `XCHAT_UNAVAILABLE` | 对话服务不可用，不自动重发消息 |

A2A 侧（JSON-RPC，HTTP 通常是 200，错误在 body 的 `error.code` 里，是**数字**）：

| code | 含义 | 处理 |
| --- | --- | --- |
| -32000 | 对方限流（TalkToMe = 每分身每天 100 轮） | HTTP 429 + `Retry-After`，别原地重试 |
| -32600 / -32601 / -32602 | 信封/方法/参数不对 | 我们这边的 bug，不要重试 |
| -32005 | 对方只收 text/plain，我们发了别的 | 不要重试 |
| -32001 | 任务查不到（TalkToMe 完成即丢） | 正常，不影响对话 |
| -32603 | 对方内部错误 | 报告用户，不自动重发

非流式请求在 5xx / 网络故障时自动退避重试 2 次（1s、4s）；SSE 不重试。
