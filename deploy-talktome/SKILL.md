---
name: deploy-talktome
description: 制作并发布 TalkToMe 分身——把用户的知识和资料变成一个发布在 talkto.bio、能替用户和访客对话的 AI 分身。当用户想把某些内容"做成分身"时使用，素材可以是文件、文档、笔记、文章、聊天记录，或当前对话里的讨论内容。触发语句如："帮我整理 XX 文件的内容，制作一个 talktome 分身"、"把我们关于 XX 的讨论做成一个分身"、"我想做一个能替我讲 XX 的分身"、"部署/上线我的 talktome 分身"。
---

# 把资料做成 TalkToMe 分身并上线

输入是用户的原始素材：文档、笔记、聊天记录、或当前对话里的讨论内容。产出是一个发布在 `talkto.bio/{handle}`、可以替用户和访客对话的 AI 分身。

流程总览：整理素材 → 生成人设/开场白/知识库（经用户确认）→ 手机号登录 → 创建上传 → 用户确认 handle → 发布 → 把链接交给用户测试。

## 第一步：整理素材，生成分身目录

1. **对齐定位（先问再做）**：分身叫什么、给访客提供什么、不做什么——一句话定位要能写进开场白。
2. **生成分身目录**（位置问用户，默认在当前工作目录下建 `talktome-agents/<名字>/`）：
   ```
   <名字>/
   ├── soul.md        # 人设，第一人称，≤2000 字
   ├── greeting.md    # 开场白 ≤300 字
   ├── knowledge/     # 素材整理成的知识文档
   └── skills/        # 可选：要固化方法论时才做
   ```
   平台统一上限（生成时就要遵守，脚本和服务端都会校验）：**名字 ≤10 字、人设 ≤2000 字、开场白 ≤300 字**。写超了优先精炼内容，细节信息放进 knowledge/ 而不是塞人设。
   - **soul.md** 结构：我是谁 / 我怎么帮你 / 我怎么说话 / 边界。只能基于素材写，素材里没有的信息（联系方式、经历、数据）一律不编——线上分身也被平台约束"只认人设+知识库"。
   - **greeting.md**：说清能做什么 + 引导访客说第一句话。
   - **knowledge/**：素材按主题拆成若干 .md，**平铺、自描述文件名**（上传不支持子目录）；聊天/讨论类素材要整理成文档（去闲聊、相对日期改绝对日期、按主题归并），不要直接扔原始记录。
   - **skills/** 仅当需要固化一套"怎么干活"的方法论时才做；每个技能一个子目录，`SKILL.md` 必须带 frontmatter `name`/`description`。
3. **把 soul.md 和 greeting.md 草稿给用户过目**，确认后继续。

## 第二步：登录（拿 accessToken）

API 基址 `https://prod-backend.talkto.bio`：

**先看有没有存过会话**：技能目录下 `.env` 里有 `TALKTOME_ACCESS_TOKEN` 就直接跳过登录（过期了脚本会自动用 refreshToken 续期并回写）。没有才走：

1. 问用户手机号 → `POST /api/auth/sms/send` body `{"phone":"<手机号>","cc":"86"}`
2. 问用户收到的验证码 → `POST /api/auth/sms/verify` body `{"phone":"...","cc":"86","code":"...","source":"skill"}` → 响应含 `accessToken` / `refreshToken` / `expiresIn`
3. **立刻存下会话**：`python scripts/deploy.py --save-token <accessToken> <refreshToken>`——写进技能目录 `.env`（已 gitignore），之后所有命令不用再传 token

- **新用户无需注册**：没注册过的手机号走同一流程自动建号（响应里 `isNewUser: true`）。`"source":"skill"` 是本渠道的注册归因标记，默认带上；若 verify 返回 400 提示 source 枚举不认 `skill`（旧版服务端），**去掉 source 字段重发同一个验证码即可**（验证码没有被消耗），登录照常，只是这次注册不带渠道归因。
- 同一手机号两次发送验证码**至少间隔 60 秒**；发送失败不要自动重试，先告知用户再定。

## 第三步：部署（跑自带脚本）

```bash
python scripts/deploy.py --src <分身目录>
```

必填只有两样：**名字**（`--name`，或目录里 config.yaml 的 `name:`，或目录名兜底）和**非空的 soul.md**。开场白、知识库、技能都可以没有——缺了就跳过，不报错。

脚本会自动完成：创建分身 → 知识库摊平上传（预查重名）→ 技能逐文件上传（校验 SKILL.md frontmatter，warnings 非空要修）→ 拉取清单核对。**增量上传**：重跑时按内容哈希（服务端 etag）跳过未变化的文件，只传新增/有改动的，中断后重跑即续传。接口路径与限额等细节都在脚本头部注释里，排障时读脚本即可。

**agentId 不需要用户知道**：首次部署后脚本把它写进 `<分身目录>/.talktome.json`，之后对同一目录的所有操作自动读取。用户报分身名字时，先看工作目录下各分身目录的 `.talktome.json` 找到对应目录；找不到就 `python scripts/deploy.py --list` 列出账号下全部分身（名字/agentId/handle/状态）按名字匹配，再用 `--agent-id` 显式指定并借此重建 `.talktome.json`。

**查看分身当前线上配置**（用户问"我的分身现在是什么配置/人设是什么/传了哪些文件"时）：

```bash
python scripts/deploy.py --src <分身目录> --show   # 目录不在手边时：--agent-id <uuid> --show（id 用 --list 查）
```

输出：基本信息（名字/handle/发布状态/邮箱/更新时间）+ 人设与开场白全文 + 知识库/技能文件清单。改分身前先 `--show` 一眼，确认线上现状再动手。

**修改已有分身**（用户说"改一下我的分身/更新人设/换知识库"时）：改动分身目录里的文件后重跑（`.talktome.json` 在就不会重复创建）：

```bash
# 知识库/技能内容变了（同名覆盖）：
python scripts/deploy.py --src <目录>
# 名字/人设/开场白也要更新：加 --update-persona
# 知识文件有改名/删除：加 --replace-knowledge；技能文件有改名/删除：加 --replace-skills
```

## 第四步：handle 与发布（两步都必须用户确认）

1. **请用户起主页名 handle**（即 `talkto.bio/{handle}`），然后校验并设置：
   ```bash
   python scripts/deploy.py --src <分身目录> --set-handle <handle>
   ```
   脚本会先过平台校验，不可用时给出原因和建议名，换个名字重试。**handle 设置后不可更改**，设置前必须让用户明确确认。
2. **用户确认发布**后：
   ```bash
   python scripts/deploy.py --src <分身目录> --publish
   ```
3. **把 `https://talkto.bio/{handle}` 发给用户**，请用户亲自打开和分身对话测试：确认分身会查阅知识库、技能行为符合预期、口吻符合人设。有问题改目录后重新部署。

## 注意

- 知识/技能改动对**新会话**生效（进行中的会话不热更新）
- 分身使用平台统一的对话模型，分身目录里的模型配置不影响线上
