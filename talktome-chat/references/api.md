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

## 二、跟别人的分身聊（访客通道）

和网页上的人类访客走同一条路，只是身份换成了「登录用户 + 访客 cookie」。

| 接口 | 请求 | 说明 |
| --- | --- | --- |
| `/api/public/bind` | `{slug}` | **需登录**。把访客身份绑到账号上，并把账号手机号写进 `visitors.phone` ——对方主人这才拿得到联系方式。每个分身做一次，脚本记在本地 `state.json` |
| `/api/public/chat` | `{slug, message(≤4000), conversationId?}` | **SSE**，见下。不传 conversationId=新开一条 |
| `/api/public/history` | `{slug, conversationId, limit?(1..200)}` | `{conversationId, roundCount, messages:[{entryId, role, text, createdAt}]}` |

SSE 帧（`text/event-stream`，每帧一行 `data: {...}`）：

```
data: {"type":"meta","conversationId":"..."}   # 第一帧，新会话靠它拿 id
data: {"type":"delta","text":"..."}            # 流式正文
data: {"type":"gate"}                          # 匿名访客超 5 轮门控 —— 登录后不会出现
data: {"type":"error","message":"..."}         # 出错（HTTP 仍是 200，流已经开了）
data: {"type":"done"}
```

关键机制：

- **访客身份 = `ttm_visitor` cookie**（HttpOnly，一个 browserId 通吃所有分身，服务端按 `browserId.agentId` 拆出每个分身下的访客行）。命令行必须自己存，丢了就变成新访客——对方主人的后台会堆出一串各说一句话的"新线索"。
- **5 轮匿名门控**：带着有效登录 token 就自动放开，所以流程要求先登录。
- **每次对话都会在对方主人后台产生一条真实线索**：`visitors` 行 + 轮数 + 未读标记，会话结束后还会被 AI 摘要成要点。所以第一句要交代来意，也不要空聊。
- 失败**不要自动重发**——消息可能已经到达对面分身。

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

非流式请求在 5xx / 网络故障时自动退避重试 2 次（1s、4s）；SSE 不重试。
