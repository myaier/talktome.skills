# TalkToMe.skills

TalkToMe 的 Claude Code 技能集合。每个子目录是一个独立技能（`SKILL.md` + 可选 `scripts/`）。

## 技能列表

- [deploy-talktome](deploy-talktome/) — 把用户的资料/讨论做成 TalkToMe 分身：整理出人设/开场白/知识库，经确认后发布到 talkto.bio

## 本仓库怎么接入 Claude Code

技能文件在本目录统一管理；Claude Code 通过项目 `.claude/skills/` 下的目录 junction 发现它们：

```bash
# Windows（无需管理员）
mklink /J <project>\.claude\skills\deploy-talktome <path>\TalkToMe.skills\deploy-talktome
# macOS / Linux
ln -s <path>/TalkToMe.skills/deploy-talktome <project>/.claude/skills/deploy-talktome
```

也可以直接把子目录整个拷贝到 `~/.claude/skills/`（用户级，所有项目可用）。
