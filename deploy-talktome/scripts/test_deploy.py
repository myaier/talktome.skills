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

    def test_writes_an_image_file_not_base64(self):
        """base64 字符串不是交付物 —— 用户要的是一张能发到群里的图。"""
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            response = {
                "imageBase64": base64.b64encode(self.PNG).decode("ascii"),
                "contentType": "image/png",
                "scene": "xiaofeng",
                "envVersion": "trial",
            }
            out, calls = run_deploy(["--token", "t", "--src", str(src), "--qrcode"], [response])

            self.assertIn("/api/agents/miniprogram-code", calls[0][0])
            written = (src / "qrcode.png").read_bytes()
            self.assertEqual(written, self.PNG)
            self.assertNotIn(response["imageBase64"], out, "把 base64 打到了终端上")

    def test_honours_an_explicit_output_path(self):
        with TemporaryDirectory() as tmp:
            src = persona_dir(tmp)
            target = Path(tmp) / "码.png"
            response = {"imageBase64": base64.b64encode(self.PNG).decode("ascii"), "contentType": "image/png"}
            run_deploy(["--token", "t", "--src", str(src), "--qrcode", str(target)], [response])
            self.assertEqual(target.read_bytes(), self.PNG)


if __name__ == "__main__":
    unittest.main(verbosity=2)
