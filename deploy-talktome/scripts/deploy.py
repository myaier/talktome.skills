# Deploy a TalkToMe persona directory via the public API (https://prod-backend.talkto.bio).
# Dependency-free (urllib only). One file per request; uploads are idempotent, so re-running resumes safely.
#
# Required inputs: agent name (from --name / config.yaml "name:" / the dir name) and a non-empty soul.md.
# Everything else (greeting.md / knowledge/ / skills/ / config.yaml) is optional — absent or empty is fine.
#
# The agentId is managed automatically: the first deploy writes it to <src>/.talktome.json, and every later
# run on the same dir picks it up — users only ever refer to the agent by name/dir. --agent-id overrides;
# --list recovers a lost mapping (match by name).
#
# Usage (token is read from <skill>/.env once saved — pass --token only to override):
#   save session:   python deploy.py --save-token <accessToken> <refreshToken>     # right after the SMS login
#   deploy:         python deploy.py --src <persona-dir> [--name <n>]
#   resume/update:  python deploy.py --src <persona-dir>                           # agentId auto-read from .talktome.json
#                     [--update-persona]     also push name/soul/greeting changes to the existing agent
#                     [--replace-skills]     delete each skill first (use when skill files were renamed/removed)
#                     [--replace-knowledge]  delete existing knowledge files first (same reason)
#   set handle:     python deploy.py --src <dir> --set-handle <slug>               # checked first; SET ONCE, locked after
#   publish:        python deploy.py --src <dir> --publish                         # outward-facing: confirm with the owner first
#   list agents:    python deploy.py --list                                        # name / agentId / handle / status
#   show config:    python deploy.py --src <dir> --show                            # profile + soul + greeting + file inventory
# Expired access tokens refresh automatically (rotating refresh token, persisted back to .env).
#
# API notes (all endpoints are POST, base https://prod-backend.talkto.bio, auth = Authorization: Bearer <accessToken>):
#   /api/agents/create   {agentName, soulContent<=100k chars, greeting?<=2000 chars} -> {agent:{id,...}}
#   /api/agents/update   {agentId, agentName?, soulContent?, greeting?, handle?}  handle can be set ONCE (then locked)
#   /api/agents/publish  {agentId}                  makes talkto.bio/{handle} public; requires handle
#   /api/handles/check   {handle}                   -> {available, reason?, suggestion?}
#   /api/knowledge-docs/upload?agentId=&filename=   raw file bytes; filename must not contain '/'
#                                                   (images are OCR'd into readable text automatically)
#   /api/knowledge-docs/delete {agentId, filename, isImage?}
#   /api/agent-skills/upload?agentId=&skillName=&path=   raw file bytes; path may contain subdirs
#       limits: 2MB/file, 100 files and 20MB total per agent; skillName matches ^[a-z0-9][a-z0-9_-]{0,63}$
#   /api/agent-skills/delete {agentId, skillName}   deletes the whole skill — used by --replace-skills, because
#                                                   the platform sync is additive (renamed/removed files would linger)
#   /api/knowledge-docs/list, /api/agent-skills/list  {agentId}  -> verification
import argparse
import hashlib
import json
import mimetypes
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE = "https://prod-backend.talkto.bio"
SKIP_SKILL_DIRS = {"scripts"}  # skill script files are not used in online chat; skip to keep the sync light

# Session storage: <skill-dir>/.env (gitignored). Written after the SMS login, then reused by every run —
# --token is only needed to override. Keys: TALKTOME_ACCESS_TOKEN / TALKTOME_REFRESH_TOKEN.
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def read_env() -> dict:
    env: dict = {}
    if ENV_PATH.is_file():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def write_env(patch: dict) -> None:
    env = {**read_env(), **patch}
    ENV_PATH.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", help="Bearer access token (default: TALKTOME_ACCESS_TOKEN from <skill>/.env)")
    ap.add_argument("--save-token", nargs=2, metavar=("ACCESS", "REFRESH"),
                    help="store a token pair into <skill>/.env and exit (run right after the SMS login)")
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"API base URL (default {DEFAULT_BASE})")
    ap.add_argument("--src", help="persona dir (soul.md required; greeting/knowledge/skills optional)")
    ap.add_argument("--agent-id", help="existing agent uuid (resume/update/set-handle/publish)")
    ap.add_argument("--name", help="agent name (default: name: in config.yaml if present, else dir name)")
    ap.add_argument("--update-persona", action="store_true",
                    help="with --agent-id: also push name/soul/greeting from --src to the existing agent")
    ap.add_argument("--set-handle", help="check + set the homepage slug (talkto.bio/{handle}); SET ONCE, locked after")
    ap.add_argument("--publish", action="store_true", help="publish the agent (outward-facing; confirm with the owner first)")
    ap.add_argument("--list", action="store_true", help="list this account's agents (name / agentId / handle / status)")
    ap.add_argument("--show", action="store_true",
                    help="print the agent's current server-side config: profile + soul + greeting + knowledge/skill inventory")
    ap.add_argument("--replace-skills", action="store_true", help="delete each skill before uploading")
    ap.add_argument("--replace-knowledge", action="store_true", help="delete existing knowledge files before uploading")
    args = ap.parse_args()

    base = args.base.rstrip("/")

    if args.save_token:
        write_env({"TALKTOME_ACCESS_TOKEN": args.save_token[0], "TALKTOME_REFRESH_TOKEN": args.save_token[1]})
        print(f"tokens saved to {ENV_PATH}")
        return

    env = read_env()
    token = args.token or env.get("TALKTOME_ACCESS_TOKEN")
    if not token:
        sys.exit("no token: run the SMS login then `--save-token <access> <refresh>` (or pass --token)")
    auth = {"token": token, "refresh": env.get("TALKTOME_REFRESH_TOKEN")}

    def try_refresh() -> bool:
        """Rotate the session via /api/auth/refresh. The old refresh token dies on use — persist the new pair
        IMMEDIATELY, or the session is lost."""
        if not auth["refresh"]:
            return False
        req = urllib.request.Request(
            base + "/api/auth/refresh",
            data=json.dumps({"refreshToken": auth["refresh"]}).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                tokens = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError):
            return False
        auth["token"], auth["refresh"] = tokens["accessToken"], tokens["refreshToken"]
        write_env({"TALKTOME_ACCESS_TOKEN": auth["token"], "TALKTOME_REFRESH_TOKEN": auth["refresh"]})
        print("[auth] access token expired — refreshed and saved")
        return True

    def call(path: str, body: bytes, content_type: str, _retried: bool = False) -> dict:
        req = urllib.request.Request(
            base + path, data=body, method="POST",
            headers={
                "Authorization": f"Bearer {auth['token']}",
                "Content-Type": content_type,
                # Analytics attribution only (never authorization): lets the backend label
                # skill-driven API usage as source_platform=skill. Unknown to old servers → ignored.
                "x-client-source": "skill",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401 and not _retried and try_refresh():
                return call(path, body, content_type, _retried=True)
            sys.exit(f"HTTP {e.code} on {path}: {e.read().decode('utf-8', 'replace')[:300]}")

    def post_json(path: str, obj: dict) -> dict:
        return call(path, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json")

    # ---- persona-dir ↔ agentId mapping (.talktome.json inside the persona dir) ----
    # Users refer to agents by name, never by uuid: the id is written here at create time and
    # picked up automatically on every later run. Lost dir → recover the id via --list (match by name).
    manifest_path = Path(args.src) / ".talktome.json" if args.src else None

    def read_manifest() -> dict:
        if manifest_path and manifest_path.is_file():
            try:
                return json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
        return {}

    def write_manifest(patch: dict) -> None:
        if not manifest_path:
            return
        data = {**read_manifest(), **patch, "base": base}
        manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def resolve_agent_id(required: bool) -> str | None:
        """--agent-id wins; else the persona dir's manifest — but only when the manifest was written against
        the SAME base (an id from another environment would be a foreign key here)."""
        agent_id = args.agent_id
        if not agent_id:
            mf = read_manifest()
            if mf.get("agentId"):
                if mf.get("base") in (None, base):
                    agent_id = mf["agentId"]
                else:
                    print(f"[manifest] ignoring agentId written for {mf.get('base')} (current base: {base})")
        if required and not agent_id:
            sys.exit("no agent id: pass --agent-id, or --src a dir that was deployed before "
                     "(.talktome.json), or find the id with --list")
        return agent_id

    # ---- standalone actions ----
    if args.list:
        agents = post_json("/api/agents/list", {})["agents"]
        for a in agents:
            print(f"{a['agent_name']}\t{a['id']}\thandle={a.get('handle') or '-'}\tstatus={a.get('status')}")
        if not agents:
            print("(no agents under this account)")
        return

    if args.show:
        agent_id = resolve_agent_id(required=True)
        agent = post_json("/api/agents/get", {"agentId": agent_id})["agent"]
        kn = post_json("/api/knowledge-docs/list", {"agentId": agent_id})["files"]
        sk = post_json("/api/agent-skills/list", {"agentId": agent_id})["skills"]
        print(f"name:      {agent.get('agent_name')}")
        print(f"agentId:   {agent.get('id')}")
        handle = agent.get("handle")
        print(f"handle:    {handle or '(not set)'}" + (f"  → https://talkto.bio/{handle}" if handle else ""))
        print(f"status:    {agent.get('status')}")
        email = agent.get("email_localpart")
        print(f"email:     {email + '@talkto.bio' if email else '(not set)'}")
        print(f"updated:   {agent.get('updated_at')}")
        soul = agent.get("soul_content") or ""
        greeting = agent.get("greeting") or ""
        print(f"\n--- soul ({len(soul)} chars) ---\n{soul or '(empty)'}")
        print(f"\n--- greeting ({len(greeting)} chars) ---\n{greeting or '(empty)'}")
        print(f"\n--- knowledge ({len(kn)} files) ---")
        for f in sorted(kn, key=lambda x: x["filename"]):
            print(f"  {f['filename']}  ({f['size']}B{', image' if f.get('isImage') else ''})")
        print(f"\n--- skills ({len(sk)}) ---")
        for s in sk:
            print(f"  {s['skillName']}: {len(s['files'])} files, {s['totalBytes']}B, hasSkillMd={s['hasSkillMd']}")
            for f in sorted(s["files"], key=lambda x: x["path"]):
                print(f"    {f['path']}  ({f['size']}B)")
        return

    if args.set_handle:
        agent_id = resolve_agent_id(required=True)
        check = post_json("/api/handles/check", {"handle": args.set_handle})
        if not check.get("available"):
            sys.exit(
                f"handle unavailable: {args.set_handle} (reason={check.get('reason')}, "
                f"suggestion={check.get('suggestion')})"
            )
        post_json("/api/agents/update", {"agentId": agent_id, "handle": args.set_handle})
        write_manifest({"agentId": agent_id, "handle": args.set_handle})
        print(f"handle set: talkto.bio/{args.set_handle}  (locked — it can never be changed)")
        return

    if args.publish:
        agent_id = resolve_agent_id(required=True)
        post_json("/api/agents/publish", {"agentId": agent_id})
        agent = post_json("/api/agents/get", {"agentId": agent_id})["agent"]
        write_manifest({"agentId": agent_id, "handle": agent.get("handle"), "published": True})
        print(f"published: https://talkto.bio/{agent['handle']}")
        return

    # ---- deploy (create or resume/update) ----
    if not args.src:
        sys.exit("--src required for deploy (or use --list / --set-handle / --publish)")
    src = Path(args.src)

    def read_persona() -> dict:
        """Read name/soul/greeting from the dir. Name and soul are REQUIRED non-empty; greeting is optional.
        Platform-wide content limits (enforced server-side too): name 10 / soul 2000 / greeting 300 chars."""
        soul_file = src / "soul.md"
        soul = soul_file.read_text(encoding="utf-8").strip() if soul_file.is_file() else ""
        if not soul:
            sys.exit(f"soul.md missing or empty — the persona is required: {soul_file}")
        if len(soul) > 2000:
            sys.exit(f"soul.md too long ({len(soul)} > 2000 chars) — condense the persona")
        name = args.name
        if not name:
            cfg = src / "config.yaml"
            m = re.search(r"^name:\s*(.+)$", cfg.read_text(encoding="utf-8"), re.M) if cfg.is_file() else None
            name = (m.group(1).strip() if m else "") or src.name
        name = name.strip()
        if not name:
            sys.exit("agent name is required (--name, config.yaml name:, or a non-empty dir name)")
        if len(name) > 10:
            sys.exit(f"agent name too long ({len(name)} > 10 chars): {name}")
        persona: dict = {"agentName": name, "soulContent": soul}
        greeting_file = src / "greeting.md"
        if greeting_file.is_file():
            greeting = greeting_file.read_text(encoding="utf-8").strip()
            if len(greeting) > 300:
                sys.exit(f"greeting too long ({len(greeting)} > 300 chars)")
            if greeting:
                persona["greeting"] = greeting
        return persona

    def upload_mime(p: Path) -> str:
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        # send JSON files as an opaque byte stream so intermediate JSON body parsers never touch them
        return "application/octet-stream" if mime == "application/json" else mime

    agent_id = resolve_agent_id(required=False)
    if agent_id:
        print(f"[reuse] agentId={agent_id}")
        if args.update_persona:
            persona = read_persona()
            post_json("/api/agents/update", {"agentId": agent_id, **persona})
            print(f"[update] name/soul{'/greeting' if 'greeting' in persona else ''} pushed "
                  f"(soulChars={len(persona['soulContent'])})")
    else:
        persona = read_persona()
        agent = post_json("/api/agents/create", persona)["agent"]
        agent_id = agent["id"]
        write_manifest({"agentId": agent_id, "name": persona["agentName"]})
        print(f"[create] agentId={agent_id} name={persona['agentName']} soulChars={len(persona['soulContent'])}")

    # knowledge: optional dir; flattened to basenames (the API rejects '/' in filenames)
    kdir = src / "knowledge"
    kfiles = sorted(p for p in kdir.rglob("*") if p.is_file()) if kdir.is_dir() else []
    names = [p.name for p in kfiles]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        sys.exit(f"flattening collision in knowledge/: {sorted(dupes)}")
    # incremental resume: the server's ETag is the file's MD5 (single-shot uploads), so unchanged files are skipped
    def md5_hex(data: bytes) -> str:
        return hashlib.md5(data).hexdigest().lower()

    def etag_of(f: dict) -> str:
        return (f.get("etag") or "").strip('"').lower()

    remote_kn: dict = {}
    if args.replace_knowledge:
        old = post_json("/api/knowledge-docs/list", {"agentId": agent_id})["files"]
        for f in old:
            post_json("/api/knowledge-docs/delete",
                      {"agentId": agent_id, "filename": f["filename"], "isImage": f.get("isImage", False)})
        print(f"[knowledge] deleted {len(old)} old files")
    elif kfiles:
        remote_kn = {f["filename"]: etag_of(f)
                     for f in post_json("/api/knowledge-docs/list", {"agentId": agent_id})["files"]}
    kn_skipped = 0
    for p in kfiles:
        data = p.read_bytes()
        if remote_kn.get(p.name) == md5_hex(data):
            kn_skipped += 1
            continue
        r = call(
            f"/api/knowledge-docs/upload?agentId={agent_id}&filename={urllib.parse.quote(p.name)}",
            data, upload_mime(p),
        )
        print(f"[knowledge] {p.name} ({r['file']['size']}B)")
    if kn_skipped:
        print(f"[knowledge] {kn_skipped} unchanged files skipped")

    # skills: optional dir; nested paths preserved
    sdir = src / "skills"
    skill_dirs = sorted(d for d in sdir.iterdir() if d.is_dir()) if sdir.is_dir() else []
    remote_sk: dict = {}
    if skill_dirs and not args.replace_skills:
        for s in post_json("/api/agent-skills/list", {"agentId": agent_id})["skills"]:
            for f in s["files"]:
                remote_sk[(s["skillName"], f["path"])] = etag_of(f)
    for skill in skill_dirs:
        skill_name = skill.name
        if args.replace_skills:
            r = post_json("/api/agent-skills/delete", {"agentId": agent_id, "skillName": skill_name})
            print(f"[skill] {skill_name}: deleted {r['deleted']} old files")
        files = sorted(
            p for p in skill.rglob("*")
            if p.is_file() and not (set(p.relative_to(skill).parts[:-1]) & SKIP_SKILL_DIRS)
        )
        sk_skipped = 0
        for p in files:
            rel = p.relative_to(skill).as_posix()
            data = p.read_bytes()
            if remote_sk.get((skill_name, rel)) == md5_hex(data):
                sk_skipped += 1
                continue
            r = call(
                f"/api/agent-skills/upload?agentId={agent_id}&skillName={skill_name}"
                f"&path={urllib.parse.quote(rel)}",
                data, upload_mime(p),
            )
            note = ""
            if rel == "SKILL.md":
                meta, warn = r.get("skillMeta"), r.get("warnings") or []
                note = f"  frontmatter={'ok' if meta and not warn else 'MISSING'} warnings={warn}"
            print(f"[skill] {skill_name}/{rel} ({r['file']['size']}B){note}")
        if sk_skipped:
            print(f"[skill] {skill_name}: {sk_skipped} unchanged files skipped")

    # verify
    kn = post_json("/api/knowledge-docs/list", {"agentId": agent_id})
    sk = post_json("/api/agent-skills/list", {"agentId": agent_id})
    print(f"\n[verify] knowledge: {len(kn['files'])} files")
    for s in sk["skills"]:
        print(f"[verify] skill {s['skillName']}: {len(s['files'])} files, {s['totalBytes']}B, hasSkillMd={s['hasSkillMd']}")
    print(f"\nDONE agentId={agent_id}")
    print("next: --set-handle <slug> (owner picks the name, set once), then --publish (owner confirms)")


if __name__ == "__main__":
    main()
