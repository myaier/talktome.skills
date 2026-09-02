# -*- coding: utf-8 -*-
"""TalkToMe 短信登录 —— 交互式，验证码只在本机输入。

用法：
    python login.py

流程：输入手机号 → 收短信 → 输入验证码 → 写入 <skill>/.env
写完就可以直接跑 deploy.py，不用再传 token。

为什么单独有这个脚本：手工登录要 agent 代收验证码、再把 accessToken 和
refreshToken 当命令行参数传给 `deploy.py --save-token`，两个长期凭据会留在
对话记录和 shell history 里。走这个脚本，它们从头到尾只在本机进程内流转。

注意：同一手机号两次发送验证码至少间隔 60 秒。
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 同目录的 deploy.py：复用它的 .env 读写，保证两边写同一个文件、同一种格式
sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy import DEFAULT_BASE, ENV_PATH, write_env  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

BASE = DEFAULT_BASE


def proxy_hint() -> str:
    """清空代理的命令因 shell 而异，别只教 cmd 的写法。"""
    if os.name == "nt":
        return 'PowerShell: $env:HTTPS_PROXY=""   cmd: set HTTPS_PROXY='
    return "unset HTTPS_PROXY HTTP_PROXY"


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"\n[HTTP {e.code}] {body}")
        return None
    except Exception as e:
        print(f"\n[网络错误] {type(e).__name__}: {e}")
        print(f"如果本机开了代理，试试先清空它：{proxy_hint()}")
        return None


def main():
    phone = input("手机号（+86，只输 11 位数字）: ").strip()
    if not (phone.isdigit() and len(phone) == 11):
        sys.exit("手机号格式不对")

    print("发送验证码…")
    if post("/api/auth/sms/send", {"phone": phone, "cc": "86"}) is None:
        sys.exit("发送失败。60 秒内不要重发，先看上面的报错。")
    print("已发送，请查收短信。")

    code = input("验证码: ").strip()

    body = {"phone": phone, "cc": "86", "code": code, "source": "skill"}
    res = post("/api/auth/sms/verify", body)
    if res is None:
        # 旧版服务端可能不认 source 枚举；验证码未被消耗，去掉字段重发一次
        print("带 source 失败，去掉该字段重试（验证码仍有效）…")
        body.pop("source")
        res = post("/api/auth/sms/verify", body)
    if res is None:
        sys.exit("登录失败。")

    at, rt = res.get("accessToken"), res.get("refreshToken")
    if not at or not rt:
        sys.exit(f"响应里没有 token：{json.dumps(res, ensure_ascii=False)[:300]}")

    # 用 deploy.py 的 write_env：合并写，不会抹掉 .env 里其他的键
    write_env({"TALKTOME_ACCESS_TOKEN": at, "TALKTOME_REFRESH_TOKEN": rt})
    print(f"\n登录成功，会话已写入 {ENV_PATH}")
    if res.get("isNewUser"):
        print("（这个手机号是新注册的）")

    print("\n下一步：")
    print("  python deploy.py --src <分身目录>     # 创建/更新分身并上传")
    print("  python deploy.py --list               # 看看账号下已有哪些分身")


if __name__ == "__main__":
    main()
