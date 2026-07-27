# deploy-talktome

Claude Code 技能：把你的资料做成 [TalkToMe](https://talkto.me) 分身并发布上线。

## 能做什么

给它一堆文件、笔记或一段讨论，说"做成一个 talktome 分身"，它会：

1. 整理素材，起草人设（soul）、开场白（greeting）、知识库文档——草稿经你确认
2. 走你的手机号登录 TalkToMe，创建分身并上传知识库/技能
3. 你确认主页名（handle）和发布后，把 `talkto.bio/{handle}` 链接交给你测试

## 安装

把 `deploy-talktome/` 放到 Claude Code 的技能目录：

```bash
# 用户级（所有项目可用）
cp -r deploy-talktome ~/.claude/skills/deploy-talktome
# 或项目级
cp -r deploy-talktome <project>/.claude/skills/deploy-talktome
```

## 使用

在 Claude Code 里直接说：

```
帮我整理 ./docs/产品手册.md 的内容，制作一个 talktome 分身
把我们刚才关于定价策略的讨论做成一个分身
```

也可以直接跑部署脚本（零依赖，Python 3.9+；token 通过技能里的手机号登录流程获取）：

```bash
python scripts/deploy.py --src <分身目录>
python scripts/deploy.py --agent-id <uuid> --set-handle <名字>
python scripts/deploy.py --agent-id <uuid> --publish
```

## 目录结构

```
deploy-talktome/
├── SKILL.md          # 技能定义：制作 → 登录 → 部署 → 确认发布
├── scripts/deploy.py # 部署脚本（接口细节见脚本头部注释）
└── README.md
```

## 注意

- 主页名（handle）只能设置一次；发布永远需要你本人确认
- 登录短信发到你自己的手机，分身建在你自己的 TalkToMe 账号名下
