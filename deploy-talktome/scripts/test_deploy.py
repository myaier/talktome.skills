"""deploy.py 的单元测试 —— 只依赖标准库，和被测脚本一样不引任何包。

    python scripts/test_deploy.py          # 或 python -m unittest discover scripts

这里测的是**名片确认和二维码交付那三条命令**（--card-copy / --set-card / --qrcode）。
它们和其余步骤有一点不同：前面那些失败了顶多是"没传上去"，这三条失败的方式更隐蔽 ——
把 AI 草稿当成已确认发出去、把 base64 字符串当成交付物、把半份名片提交上去被服务端拒掉。
所以守的是请求体和落盘产物，不是"有没有报错"。

网络整个打桩：urlopen 被换成一个按调用顺序吐预设 JSON 的假对象，同时把每次请求的
path 和 body 记下来供断言。不碰真后端，也不需要 token。
"""
import base64
import io
import json
import sys
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy  # noqa: E402


class FakeResponse(io.BytesIO):
    """urlopen 的返回值要能当上下文管理器用。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def run_deploy(argv, responses):
    """跑一次 deploy.main()，返回 (stdout, 请求列表)。

    responses 按顺序吐给每次 urlopen；请求列表里每项是 (path, 解析后的 body)。
    """
    calls = []
    queue = list(responses)

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        body = req.data
        try:
            parsed = json.loads(body.decode("utf-8")) if body else None
        except (UnicodeDecodeError, ValueError):
            parsed = body  # 上传接口是裸字节，不是 JSON
        calls.append((req.full_url, parsed))
        return FakeResponse(json.dumps(queue.pop(0)).encode("utf-8"))

    out = io.StringIO()
    with mock.patch.object(urllib.request, "urlopen", fake_urlopen), \
         mock.patch.object(sys, "argv", ["deploy.py", *argv]), \
         redirect_stdout(out):
        deploy.main()
    return out.getvalue(), calls


def persona_dir(tmp, agent_id="agent-1"):
    """一个已经部署过的分身目录：有 .talktome.json，所以命令不用传 --agent-id。"""
    src = Path(tmp)
    (src / ".talktome.json").write_text(
        json.dumps({"agentId": agent_id, "base": deploy.DEFAULT_BASE}), encoding="utf-8"
    )
    return src


READY = {
    "state": "ready",
    "confirmed": {"intro": "", "tags": [], "questions": []},
    "draft": {
        "intro": "帮你把复杂产品问题讲清楚。",
        "tags": ["产品", "AI 协作"],
        "questions": ["怎么把模糊需求讲清楚？", "产品和研发怎么对齐？", "你踩过哪些坑？"],
    },
}


class CardCopyTests(unittest.TestCase):
    def test_polls_status_instead_of_submitting_again(self):
        """state=generating 表示已经有任务在跑。再提交一次就是又一次模型调用。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            generating = {"state": "generating", "confirmed": {}, "draft": {}}
            with mock.patch.object(deploy.time, "sleep"):
                out, calls = run_deploy(
                    ["--token", "t", "--src", str(src), "--card-copy"],
                    [generating, generating, READY],
                )

            paths = [c[0] for c in calls]
            self.assertEqual(sum("card-copy/ensure" in p for p in paths), 1, "ensure 被提交了不止一次")
            self.assertEqual(sum("card-copy/status" in p for p in paths), 2)
            self.assertIn("card draft written", out)

    def test_prefers_the_draft_over_what_is_already_public(self):
        """草稿是针对**当前**素材写的；改过人设之后，它才是反映现状的那一版。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            status = {
                "state": "ready",
                "confirmed": {"intro": "上一版已公开的介绍", "tags": ["旧"], "questions": ["旧一", "旧二", "旧三"]},
                "draft": READY["draft"],
            }
            run_deploy(["--token", "t", "--src", str(src), "--card-copy"], [status])

            card = json.loads((src / "card.json").read_text(encoding="utf-8"))
            self.assertEqual(card["intro"], READY["draft"]["intro"])
            self.assertEqual(card["questions"], READY["draft"]["questions"])

    def test_falls_back_to_the_confirmed_copy_when_there_is_no_draft(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            status = {"state": "ready", "confirmed": READY["draft"], "draft": {"intro": "", "tags": [], "questions": []}}
            run_deploy(["--token", "t", "--src", str(src), "--card-copy"], [status])

            card = json.loads((src / "card.json").read_text(encoding="utf-8"))
            self.assertEqual(card["intro"], READY["draft"]["intro"])

    def test_does_not_write_a_card_it_could_not_get(self):
        """拿不到文案就退出，不留一个空 card.json —— 那会让下一步 --set-card 覆盖掉已公开的版本。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            empty = {"state": "failed", "confirmed": {}, "draft": {}}
            with self.assertRaises(SystemExit):
                run_deploy(["--token", "t", "--src", str(src), "--card-copy"], [empty])
            self.assertFalse((src / "card.json").exists())


class SetCardTests(unittest.TestCase):
    def write_card(self, src, **overrides):
        card = {"intro": READY["draft"]["intro"], "tags": READY["draft"]["tags"],
                "questions": list(READY["draft"]["questions"])}
        card.update(overrides)
        (src / "card.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")

    def test_sends_intro_tags_and_questions_in_one_request(self):
        """服务端只收整份：只更新问题、不带介绍，会把没人读过的介绍也标成已确认。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            self.write_card(src)
            _, calls = run_deploy(["--token", "t", "--src", str(src), "--set-card"], [{"agent": {}}])

            self.assertEqual(len(calls), 1)
            path, body = calls[0]
            self.assertIn("/api/agents/update", path)
            self.assertEqual(body["shareIntro"], READY["draft"]["intro"])
            self.assertEqual(body["shareTags"], READY["draft"]["tags"])
            self.assertEqual(body["suggestedQuestions"], READY["draft"]["questions"])

    def assert_rejected(self, src, expect_in_message, **overrides):
        self.write_card(src, **overrides)
        with self.assertRaises(SystemExit) as ctx:
            run_deploy(["--token", "t", "--src", str(src), "--set-card"], [])
        self.assertIn(expect_in_message, str(ctx.exception))

    def test_rejects_a_half_filled_card_before_the_request(self):
        """本地拦住，错误才指得出是哪个字段；让服务端回 400 的话 agent 还得自己猜。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            self.assert_rejected(src, "distinct questions", questions=["只有一个"])
            self.assert_rejected(src, "distinct questions", questions=["一样", "一样", "不一样"])
            self.assert_rejected(src, "intro must be", intro="")

    def test_rejects_questions_too_long_to_sit_on_one_line(self):
        """问题在主页上一行一条、是点一下就问出去的 —— 长度是版式约束，不是偏好。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            long_q = "长" * (deploy.CARD_QUESTION_MAX + 1)
            self.assert_rejected(src, f"<= {deploy.CARD_QUESTION_MAX} chars", questions=["一", "二", long_q])

    def test_rejects_too_many_or_too_long_tags(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            self.assert_rejected(src, "tags", tags=["a", "b", "c", "d"])
            self.assert_rejected(src, "tags", tags=["七个字的标签啊"])

    def test_refuses_to_guess_when_card_json_is_missing(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            with self.assertRaises(SystemExit) as ctx:
                run_deploy(["--token", "t", "--src", str(src), "--set-card"], [])
            self.assertIn("card.json not found", str(ctx.exception))


class QrCodeTests(unittest.TestCase):
    PNG = b"\x89PNG\r\n\x1a\n" + b"fake"

    def _response(self, **extra):
        r = {"imageBase64": base64.b64encode(self.PNG).decode("ascii"), "contentType": "image/png", "scene": "xiaofeng"}
        r.update(extra)
        return r

    def test_writes_an_image_file_not_base64(self):
        """base64 字符串不是交付物 —— 用户要的是一张能发到群里的图。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            response = self._response(hasQrCode=True)
            out, calls = run_deploy(["--token", "t", "--src", str(src), "--qrcode"], [response])

            self.assertIn("/api/agents/share-card", calls[0][0])
            written = (src / "share-card.png").read_bytes()
            self.assertEqual(written, self.PNG)
            self.assertNotIn(response["imageBase64"], out, "把 base64 打到了终端上")

    def test_default_deliverable_is_the_card_not_the_bare_code(self):
        """默认出的是名片图。光一个码发出去，收到的人不知道背后是谁。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            _, calls = run_deploy(["--token", "t", "--src", str(src), "--qrcode"], [self._response()])
            self.assertIn("/api/agents/share-card", calls[0][0])
            self.assertEqual(calls[0][1].get("ratio"), "timeline")

    def test_plain_code_asks_for_the_bare_code(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            _, calls = run_deploy(
                ["--token", "t", "--src", str(src), "--qrcode", "--plain-code"],
                [self._response(envVersion="trial")],
            )
            self.assertIn("/api/agents/miniprogram-code", calls[0][0])
            self.assertTrue((src / "qrcode.png").is_file())

    def test_warns_when_the_card_came_back_without_a_code(self):
        """没有码的卡是残的 —— 必须说出来，不能让 agent 把它当成品交出去。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            out, _ = run_deploy(["--token", "t", "--src", str(src), "--qrcode"], [self._response(hasQrCode=False)])
            self.assertIn("WARNING", out)

    def test_honours_an_explicit_output_path(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            target = Path(tmp) / "码.png"
            run_deploy(["--token", "t", "--src", str(src), "--qrcode", str(target)], [self._response()])
            self.assertEqual(target.read_bytes(), self.PNG)


class SocialsTests(unittest.TestCase):
    """socials 是可选的，而且服务端是整表替换 —— 漏发等于清空，这几条盯的就是这个。"""

    def _write_card(self, src, **extra):
        card = {"intro": "做产品的人", "tags": ["产品"], "questions": ["问题一", "问题二", "问题三"]}
        card.update(extra)
        (src / "card.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")

    def test_absent_key_leaves_links_untouched(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            self._write_card(src)
            _, calls = run_deploy(["--token", "t", "--src", str(src), "--set-card"], [{"agent": {}}])
            self.assertNotIn("socials", calls[0][1], "card.json 里没写 socials 却发了，会把已有链接清空")

    def test_sends_links_with_optional_label(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            self._write_card(src, socials=[
                {"platform": "website", "url": "https://example.com", "label": "我的网站"},
                {"platform": "github", "url": "https://github.com/me"},
            ])
            _, calls = run_deploy(["--token", "t", "--src", str(src), "--set-card"], [{"agent": {}}])
            self.assertEqual(calls[0][1]["socials"], [
                {"platform": "website", "url": "https://example.com", "label": "我的网站"},
                {"platform": "github", "url": "https://github.com/me"},
            ])

    def test_rejects_a_link_without_a_url(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            self._write_card(src, socials=[{"platform": "weibo"}])
            with self.assertRaises(SystemExit) as ctx:
                run_deploy(["--token", "t", "--src", str(src), "--set-card"], [{"agent": {}}])
            self.assertIn("url", str(ctx.exception))

    def test_card_copy_refresh_keeps_the_owners_links(self):
        """重新拉一次文案不该把用户自己填的链接冲掉 —— card.json 是同一个文件。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            self._write_card(src, socials=[{"platform": "website", "url": "https://example.com"}])
            run_deploy(["--token", "t", "--src", str(src), "--card-copy"],
                       [{"state": "ready", "draft": {"intro": "新写的介绍", "tags": ["新"], "questions": ["一", "二", "三"]},
                         "confirmed": {}}])
            after = json.loads((src / "card.json").read_text(encoding="utf-8"))
            self.assertEqual(after["intro"], "新写的介绍")
            self.assertEqual(after["socials"], [{"platform": "website", "url": "https://example.com"}])

    def test_rejects_socials_that_is_not_a_list(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            self._write_card(src, socials={"website": "https://example.com"})
            with self.assertRaises(SystemExit) as ctx:
                run_deploy(["--token", "t", "--src", str(src), "--set-card"], [{"agent": {}}])
            self.assertIn("socials", str(ctx.exception))


class KnowledgeHousekeepingTests(unittest.TestCase):
    """知识库的删 / 移 / 对齐。

    这三条之前完全没有，agent 想删掉一个文件只能 --replace-knowledge 把线上全删了重传 ——
    文件多的时候又慢又险，还会把用户在 App 里整理好的目录一起铲掉。所以这里守的是
    「只动该动的那个」：删单个文件不碰别的、移动用的是 move 而不是删了重传、prune 不碰没变的。
    """

    TREE = {
        "files": [
            {"filename": "resume.md", "size": 10, "etag": "a"},
            {"filename": "projects/x.md", "size": 20, "etag": "b"},
            {"filename": "pics/logo.png", "size": 30, "etag": "c", "isImage": True},
        ],
        "folders": ["projects", "pics"],
    }

    def test_removes_one_file_and_nothing_else(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            _, calls = run_deploy(
                ["--token", "t", "--src", str(src), "--rm-knowledge", "projects/x.md"],
                [self.TREE, {"ok": True}],
            )
            deletes = [c for c in calls if "knowledge-docs/delete" in c[0]]
            self.assertEqual(len(deletes), 1, "只该删一个")
            self.assertEqual(deletes[0][1]["filename"], "projects/x.md")

    def test_carries_the_image_flag(self):
        """图片存在服务端的 image/ 子树下，isImage 传错就删不掉，而且不会报错。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            _, calls = run_deploy(
                ["--token", "t", "--src", str(src), "--rm-knowledge", "pics/logo.png"],
                [self.TREE, {"ok": True}],
            )
            self.assertIs(calls[-1][1]["isImage"], True)

    def test_removing_a_folder_uses_the_folder_endpoint(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            out, calls = run_deploy(
                ["--token", "t", "--src", str(src), "--rm-knowledge", "projects"],
                [self.TREE, {"ok": True}],
            )
            self.assertIn("knowledge-docs/folder/delete", calls[-1][0])
            self.assertEqual(calls[-1][1]["path"], "projects")
            self.assertIn("1 files inside", out, "删目录前要说清楚里面有多少东西")

    def test_refuses_to_delete_something_that_is_not_there(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            with self.assertRaises(SystemExit) as ctx:
                run_deploy(["--token", "t", "--src", str(src), "--rm-knowledge", "nope.md"], [self.TREE])
            self.assertIn("not found online", str(ctx.exception))

    def test_move_uses_the_move_endpoint_not_delete_and_reupload(self):
        """删了重传会丢掉服务端的 OCR 副本，而且中途失败文件就没了。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            _, calls = run_deploy(
                ["--token", "t", "--src", str(src), "--mv-knowledge", "resume.md", "about/resume.md"],
                [self.TREE, {"ok": True}],
            )
            self.assertIn("knowledge-docs/move", calls[-1][0])
            self.assertEqual(calls[-1][1]["sourcePath"], "resume.md")
            self.assertEqual(calls[-1][1]["destinationPath"], "about/resume.md")
            self.assertEqual(calls[-1][1]["kind"], "file")
            self.assertFalse(any("delete" in c[0] for c in calls), "移动不该走删除")

    def test_move_refuses_to_overwrite(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            with self.assertRaises(SystemExit) as ctx:
                run_deploy(
                    ["--token", "t", "--src", str(src), "--mv-knowledge", "resume.md", "projects/x.md"],
                    [self.TREE],
                )
            self.assertIn("already exists", str(ctx.exception))

    def test_prune_deletes_only_what_is_gone_locally(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            kdir = src / "knowledge"
            (kdir / "projects").mkdir(parents=True)
            (kdir / "resume.md").write_text("x", encoding="utf-8")
            (kdir / "projects" / "x.md").write_text("y", encoding="utf-8")
            # 本地没有 pics/logo.png，也没有 pics/ 这个目录 -> 两样都该被清掉
            out, calls = run_deploy(
                ["--token", "t", "--src", str(src), "--prune-knowledge"],
                [self.TREE, {"ok": True}, {"ok": True}],
            )
            deleted = [c[1].get("filename") for c in calls if "knowledge-docs/delete" in c[0]]
            self.assertEqual(deleted, ["pics/logo.png"], "只该删本地已经没有的那一个")
            folders = [c[1]["path"] for c in calls if "folder/delete" in c[0]]
            self.assertEqual(folders, ["pics"])
            self.assertIn("1 file(s), 1 folder(s) removed", out)

    def test_prune_refuses_when_there_is_no_local_knowledge_dir(self):
        """本地没有 knowledge/ 多半是跑错目录，照做就是把线上清空。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            with self.assertRaises(SystemExit) as ctx:
                run_deploy(["--token", "t", "--src", str(src), "--prune-knowledge"], [])
            self.assertIn("refusing to prune", str(ctx.exception))


class CoverTests(unittest.TestCase):
    """封面图：名片页顶部头像后面那一条，和头像共用一套上传逻辑。"""

    PNG = b"\x89PNG\r\n\x1a\n" + b"cover-bytes"

    def _src(self, tmp):
        src = persona_dir(tmp)
        (src / "soul.md").write_text("我是谁", encoding="utf-8")
        return src

    def test_cover_in_the_persona_dir_is_picked_up(self):
        with TemporaryDirectory() as tmp:
            src = self._src(tmp)
            (src / "cover.png").write_bytes(self.PNG)
            _, calls = run_deploy(
                ["--token", "t", "--src", str(src)],
                [{"agent": {}, "url": "https://oss/cover.png"},
                 {"files": []}, {"skills": []}, {"files": []}, {"skills": []}],
            )
            covers = [c for c in calls if c[0].endswith("/api/agents/cover")]
            self.assertEqual(len(covers), 1, "目录里的 cover.png 要被自动认出来")
            self.assertEqual(covers[0][1]["contentType"], "image/png")
            self.assertEqual(base64.b64decode(covers[0][1]["base64"]), self.PNG)

    def test_unchanged_cover_is_not_reuploaded(self):
        """每次上传都写一个新的对象 key，不跳过的话每跑一次就在 OSS 上多一份死对象。"""
        with TemporaryDirectory() as tmp:
            src = self._src(tmp)
            (src / "cover.png").write_bytes(self.PNG)
            run_deploy(["--token", "t", "--src", str(src)],
                       [{"agent": {}, "url": "u"}, {"files": []}, {"skills": []}, {"files": []}, {"skills": []}])
            out, calls = run_deploy(["--token", "t", "--src", str(src)],
                                    [{"files": []}, {"skills": []}, {"files": []}, {"skills": []}])
            self.assertFalse(any(c[0].endswith("/api/agents/cover") for c in calls))
            self.assertIn("unchanged", out)

    def test_rejects_an_unsupported_format(self):
        with TemporaryDirectory() as tmp:
            src = self._src(tmp)
            bad = Path(tmp) / "cover.gif"
            bad.write_bytes(b"GIF89a")
            with self.assertRaises(SystemExit) as ctx:
                run_deploy(["--token", "t", "--src", str(src), "--cover", str(bad)], [])
            self.assertIn("unsupported cover type", str(ctx.exception))


class SkillHousekeepingTests(unittest.TestCase):
    """删技能 / 删技能里的一个文件。

    技能的同步和知识库一样只增不减，所以本地删掉一个文件不等于线上删掉。
    这里守的是「别删错东西」：技能名或文件名对不上时要当场报错，而不是发一个删不到东西的请求。
    """

    SKILLS = {"skills": [
        {"skillName": "pricing", "totalBytes": 30, "hasSkillMd": True,
         "files": [{"path": "SKILL.md", "size": 10, "etag": "a"},
                   {"path": "refs/table.md", "size": 20, "etag": "b"}]},
        {"skillName": "faq", "totalBytes": 5, "hasSkillMd": True,
         "files": [{"path": "SKILL.md", "size": 5, "etag": "c"}]},
    ]}

    def test_deletes_the_whole_skill_when_no_path_is_given(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            out, calls = run_deploy(
                ["--token", "t", "--src", str(src), "--rm-skill", "pricing"],
                [self.SKILLS, {"ok": True, "deleted": 2}],
            )
            self.assertIn("agent-skills/delete", calls[-1][0])
            self.assertEqual(calls[-1][1]["skillName"], "pricing")
            self.assertNotIn("path", calls[-1][1], "不给 path 才是整个技能删掉")
            self.assertIn("2 files", out)

    def test_deletes_one_file_inside_a_skill(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            _, calls = run_deploy(
                ["--token", "t", "--src", str(src), "--rm-skill", "pricing", "refs/table.md"],
                [self.SKILLS, {"ok": True, "deleted": 1}],
            )
            self.assertEqual(calls[-1][1]["path"], "refs/table.md")
            self.assertEqual(calls[-1][1]["skillName"], "pricing")

    def test_warns_when_removing_the_entry_file(self):
        """没有 SKILL.md，xchat 就不再把这个目录注册成技能 —— 剩下的文件全变死数据。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            out, _ = run_deploy(
                ["--token", "t", "--src", str(src), "--rm-skill", "pricing", "SKILL.md"],
                [self.SKILLS, {"ok": True, "deleted": 1}],
            )
            self.assertIn("WARNING", out)
            self.assertIn("不再被加载", out)

    def test_refuses_an_unknown_skill(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            with self.assertRaises(SystemExit) as ctx:
                run_deploy(["--token", "t", "--src", str(src), "--rm-skill", "nope"], [self.SKILLS])
            self.assertIn("no such skill online", str(ctx.exception))

    def test_refuses_an_unknown_file(self):
        """路径打错时当场说，别发一个删不到东西的请求然后报告成功。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            with self.assertRaises(SystemExit) as ctx:
                run_deploy(["--token", "t", "--src", str(src), "--rm-skill", "pricing", "nope.md"], [self.SKILLS])
            self.assertIn("no such file in pricing", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
