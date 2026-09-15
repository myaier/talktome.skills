# 名片创建与交付 API

这些调用由 `scripts/deploy.py` 实现。正式基址 `https://prod-backend.talkto.bio`；联调使用 `--base https://int-backend.talkto.bio`。所有接口 POST JSON，鉴权 `Authorization: Bearer <accessToken>`，来源 `x-client-source: skill`。令牌只从技能 `.env` 读写，401 时刷新并立即保存旋转后的令牌，最多重试一次。

## 登录与部署

交互式 `scripts/login.py` 调用 `/api/auth/sms/send`（phone、cc=86）及 `/api/auth/sms/verify`（phone、cc、code、source=skill），直接把返回的 accessToken/refreshToken 存本机 `.env`，禁止输出。只有明确 400 source 枚举不支持才移除 source 重试；验证码错误、超时、限流不自动重试。手机号及验证码由用户自己输入。

`deploy.py --src <目录>` 创建或复用 `.talktome.json` 的 agentId，上传人设、头像、知识与回答技能。已有 Agent 更新人设需 `--update-persona`；删除/替换知识或技能须确认范围。`--show` 读服务端资料及文件目录以核对；`--set-handle <slug>` 同时设置锁定的主页与邮箱，先请用户确认；`--publish` 发布。

## card.json

仅支持下列字段，脚本创建后调用 `/api/agents/update`（补充 agentId）。不要上传未经用户确认的联系方式。字段缺省表示保留；空数组表示清空，必须明确确认。

```json
{
  "shareIntro": "我做产品设计，欢迎聊聊从想法到上线的过程。",
  "shareTags": ["产品设计", "原型", "协作"],
  "socials": [{"platform": "website", "url": "https://example.com", "label": "我的网站"}]
}
```

shareIntro 最多100字（建议70字内）；shareTags 最多3个，每个1–6字；socials 最多50项，platform 1–50字、url 1–500字、label可选且最多100字。

已有 AI 名片文案草稿可通过 `/api/agents/card-copy/ensure`、`/api/agents/card-copy/status`（agentId）获取。草稿必须给用户确认，之后才调用 update，不把生成结果直接当作用户已确认的内容。

## 小程序二维码卡片

`POST /api/agents/miniprogram-card`：

```json
{"agentId":"<本账号的 Agent UUID>","envVersion":"release"}
```

envVersion 可选 release（默认）/trial/develop。Agent 必须属于当前账号且已发布并设置 handle。复用现有 `/api/agents/miniprogram-code` 的微信码生成和头像逻辑，增加纸风筝蓝白卡片边框及名片地址，不改变原始小程序码像素。正式/体验/开发版要与目标使用环境一致。

成功返回 `{imageBase64, contentType:"image/png", scene, envVersion}`。脚本 `--card <path.png>` 校验 PNG 并保存图片。现有 `/miniprogram-code` 仍返回原始码供老客户端使用。未发布、越权、微信接口错误必须作为失败处理，不伪造卡片；重试用原 agentId。图片通过宿主直接发给用户，附链接并请其微信扫码核对。


### Creator-reviewed conversation starters

Authenticated owner endpoints (Bearer session):
- `POST /api/agents/card-copy/ensure` with `{agentId}` starts or reuses a generation job, returning `{state, confirmed, draft}`. It does not publish generated content.
- `POST /api/agents/card-copy/status` with `{agentId}` polls without generation. `confirmed` and `draft` each contain `intro`, `tags`, and `questions`. Poll every second, up to 60 seconds.
- After explicit user review, `POST /api/agents/update` with `{agentId, shareIntro, shareTags, suggestedQuestions}` saves all three parts together. `suggestedQuestions` must have exactly three distinct, trimmed, nonempty strings of at most 80 characters each; intro must be nonempty and at most100 characters, tags at most3 items, each at most6 characters. The public profile exposes only confirmed questions, never private drafts.
- If confirmation fails, retain the draft and stop sharing. After successful confirmation, use `/api/agents/miniprogram-card` as documented above to deliver the actual mini-program card.

Do not put service keys in the guide or command line. Never treat a generated draft as user approval.
