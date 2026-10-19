#!/usr/bin/env python3
"""Activity ID -> current form -> authenticated draft. Python standard library only."""
import argparse
import getpass
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE = os.environ.get('TALKTOME_EVENT_API_BASE', 'https://prod-backend.talkto.bio').rstrip('/')
ROOT = Path(os.environ.get('TALKTOME_EVENT_STATE_DIR', str(Path(__file__).resolve().parent.parent / '.state')))

class ApiError(Exception):
    def __init__(self, code, message, status=0):
        super().__init__(message)
        self.code, self.status = code, status

def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.chmod(temp, 0o600)
    temp.replace(path)

def read_json(path, fallback=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else fallback

class Client:
    def __init__(self, base=BASE, root=ROOT):
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost')):
            raise ValueError('Only HTTPS or local test HTTP is supported')
        self.base = base
        self.root = root / hashlib.sha256(base.encode()).hexdigest()[:16]
        self.session_file = self.root / 'session.json'
        self.session = read_json(self.session_file, {})

    def call(self, path, payload, auth=True, raw=False, retried=False):
        headers = {'Content-Type': 'application/octet-stream' if raw else 'application/json', 'x-client-source': 'skill'}
        if auth:
            token = self.session.get('accessToken')
            if not token:
                raise ApiError('LOGIN_REQUIRED', '请先运行 login，由本人在本机完成手机号验证。')
            headers['Authorization'] = 'Bearer ' + token
        req = urllib.request.Request(self.base + path, data=payload if raw else json.dumps(payload).encode(), headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and auth and not retried and self.session.get('refreshToken'):
                tokens = self.call('/api/auth/refresh', {'refreshToken': self.session['refreshToken']}, auth=False)
                self.save_session(tokens)
                return self.call(path, payload, auth=auth, raw=raw, retried=True)
            try:
                err = json.loads(exc.read()).get('error', {})
            except (ValueError, AttributeError):
                err = {}
            raise ApiError(err.get('code', 'HTTP_ERROR'), err.get('message', '服务暂不可用，请稍后重试'), exc.code) from None

    def save_session(self, tokens):
        if not tokens.get('accessToken') or not tokens.get('refreshToken'):
            raise ApiError('INVALID_SESSION', '登录服务没有返回有效会话')
        self.session = {'accessToken': tokens['accessToken'], 'refreshToken': tokens['refreshToken']}
        write_private(self.session_file, self.session)

    def context_path(self, activity):
        if not re.fullmatch(r'[a-f0-9]{16}', activity):
            raise ApiError('INVALID_ACTIVITY_ID', '请复制报名页的活动编号')
        return self.root / ('form-' + activity + '.json')

    def form(self, activity, persist=True):
        path = self.context_path(activity)
        form = self.call('/api/public/events/get', {'activityId': activity}, auth=False)
        if persist:
            write_private(path, form)
        return form

    def save(self, activity, answers):
        context = self.context_path(activity)
        previous = read_json(context)
        if previous is None:
            self.form(activity)
            raise ApiError('FORM_REVIEW_REQUIRED', '已获取真实表单，请按当前题目核对答案后重新保存。')
        current = self.form(activity, persist=False)
        kept, changed = reconcile(previous['questions'], current['questions'], answers)
        if changed:
            write_private(context, current)
            return {'saved': False, 'code': 'FORM_UPDATED', 'changedQuestionIds': changed, 'compatibleAnswers': kept, 'questions': current['questions']}
        if current['event']['status'] != 'open':
            raise ApiError('REGISTRATION_CLOSED', '活动已停止报名')
        body = {'eventId': current['event']['id'], 'formVersion': current['formVersion'], 'answers': kept}
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        pending_path = self.root / ('pending-' + activity + '.json')
        pending = read_json(pending_path, {})
        if pending.get('fingerprint') != fingerprint:
            pending = {'fingerprint': fingerprint, 'key': str(uuid.uuid4())}
            write_private(pending_path, pending)
        try:
            result = self.call('/api/events/draft', {**body, 'operationKey': pending['key']})
        except ApiError as exc:
            if exc.code == 'FORM_UPDATED':
                # Keep the previous context: next invocation compares it with the fresh questions.
                return {'saved': False, 'code': exc.code, 'message': '填写期间表单更新，请重新读取题目并核对后保存。'}
            raise
        pending_path.unlink(missing_ok=True)
        write_private(context, current)
        return {'saved': True, 'activityId': activity, 'title': current['event']['title'], 'expiresAt': result['result']['expiresAt'], 'message': '草稿已保存，请回到同一活动核对提交；尚未完成报名。'}

def reconcile(previous, current, answers):
    if not isinstance(answers, dict):
        raise ApiError('INVALID_ANSWERS', '答案文件必须是题目 ID 到答案的 JSON 对象')
    old = {q['id']: q for q in previous}
    now = {q['id']: q for q in current}
    kept, changed = {}, []
    for key, answer in answers.items():
        if key not in now or key not in old or any(old[key].get(p) != now[key].get(p) for p in ('label', 'type', 'options')):
            changed.append(key)
        else:
            kept[key] = answer
    for key, question in now.items():
        if question.get('required') and kept.get(key) in (None, '', []):
            changed.append(key)
    return kept, sorted(set(changed))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('login')
    for name in ('form', 'save', 'upload', 'status'):
        p = sub.add_parser(name)
        p.add_argument('activity_id')
        if name == 'save': p.add_argument('--answers', type=Path, required=True)
        if name == 'upload': p.add_argument('--file', type=Path, required=True)
    args = parser.parse_args()
    client = Client()
    if args.command == 'login':
        phone = input('本人手机号（中国大陆 11 位）: ').strip()
        if not re.fullmatch(r'1\d{10}', phone): raise ApiError('INVALID_PHONE', '手机号格式不正确')
        client.call('/api/auth/sms/send', {'phone': phone, 'cc': '86'}, auth=False)
        code = getpass.getpass('短信验证码（不回显）: ').strip()
        client.save_session(client.call('/api/auth/sms/verify', {'phone': phone, 'cc': '86', 'code': code, 'source': 'skill'}, auth=False))
        result = {'loggedIn': True}
    elif args.command == 'form': result = client.form(args.activity_id)
    elif args.command == 'save': result = client.save(args.activity_id, read_json(args.answers))
    elif args.command == 'status':
        form = client.form(args.activity_id)
        own = client.call('/api/events/mine', {'eventId': form['event']['id']})
        result = {'activityId': args.activity_id, 'registered': bool(own.get('registration')), 'draftSaved': bool(own.get('draft'))}
    else:
        form = client.form(args.activity_id)
        if args.file.stat().st_size > 10 * 1024 * 1024: raise ApiError('FILE_TOO_LARGE', '附件最多 10 MB')
        query = urllib.parse.urlencode({'eventId': form['event']['id'], 'kind': 'attachment', 'filename': args.file.name})
        result = client.call('/api/events/assets/upload?' + query, args.file.read_bytes(), raw=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    try: main()
    except ApiError as exc:
        print(json.dumps({'error': {'code': exc.code, 'message': str(exc)}}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError) as exc:
        print(json.dumps({'error': {'code': 'LOCAL_OR_NETWORK_ERROR', 'message': str(exc)}}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
