"""Groundline spike: test X OAuth 2.0 + DM v2 between accounts you control.

Usage (PowerShell, from this folder):
  py spike.py auth <label>                 # log in as one account, save tokens_<label>.json
  py spike.py me <label>                   # who is this token?
  py spike.py refresh <label>              # force a token refresh
  py spike.py users <label> <name> [...]   # DM-reachability fields for usernames
  py spike.py send <label> <name> "<text>" # send one DM via DM v2
  py spike.py events <label> [<name>]      # read DM events (all, or with one person)
Every API response is appended to spike_log.jsonl (token responses redacted).
"""
import base64, hashlib, http.server, json, os, secrets, sys, time
import urllib.error, urllib.parse, urllib.request, webbrowser
from pathlib import Path

CLIENT_ID = os.environ["X_CLIENT_ID"]
CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET", "")
REDIRECT = os.environ.get("X_REDIRECT_URI", "http://127.0.0.1:8765/callback")
SCOPES = "tweet.read users.read follows.read dm.read dm.write offline.access"
API = "https://api.x.com/2"
HERE = Path(__file__).resolve().parent
LOG = HERE / "spike_log.jsonl"
FIELDS = "receives_your_dm,receives_your_chat,connection_status,protected"
EV = "dm_event.fields=created_at,sender_id,dm_conversation_id,text&max_results=20"


def log(entry):
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def call(method, url, token=None, form=None, body=None, basic=False):
    headers, data = {}, None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if basic:
        cred = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        headers["Authorization"] = f"Basic {cred}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, raw, hdrs = r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        status, raw, hdrs = e.code, e.read().decode(), dict(e.headers)
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw
    secret = "oauth2/token" in url
    rate = {k: v for k, v in hdrs.items() if k.lower().startswith("x-rate-limit")}
    log({"method": method, "url": url, "status": status, "rate_limit": rate,
         "body": "<redacted>" if secret else parsed})
    print(status, "(token response saved)" if secret and status == 200
          else json.dumps(parsed, indent=2, ensure_ascii=False))
    return status, parsed


def tok_path(label):
    return HERE / f"tokens_{label}.json"


def save(label, t):
    t["obtained_at"] = int(time.time())
    tok_path(label).write_text(json.dumps(t), encoding="utf-8")
    return t


def refresh(label, t=None):
    t = t or json.loads(tok_path(label).read_text(encoding="utf-8"))
    form = {"grant_type": "refresh_token", "refresh_token": t["refresh_token"],
            "client_id": CLIENT_ID}
    status, new = call("POST", f"{API}/oauth2/token", form=form, basic=bool(CLIENT_SECRET))
    if status != 200:
        sys.exit("refresh failed: run auth again")
    return save(label, new)


def token(label):
    t = json.loads(tok_path(label).read_text(encoding="utf-8"))
    if time.time() > t["obtained_at"] + t.get("expires_in", 7200) - 60:
        t = refresh(label, t)
    return t["access_token"]


def auth(label):
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = "https://x.com/i/oauth2/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT,
        "scope": SCOPES, "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256"})
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab.")

        def log_message(self, *args):
            pass

    p = urllib.parse.urlparse(REDIRECT)
    server = http.server.HTTPServer((p.hostname, p.port), Handler)
    print(f"Log in to X as '{label}' in your browser, then approve:\n{url}\n")
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    server.server_close()
    if "error" in result:
        sys.exit(f"authorization refused: {result}")
    if result.get("state") != state:
        sys.exit("state mismatch: start auth again")
    form = {"grant_type": "authorization_code", "code": result["code"],
            "redirect_uri": REDIRECT, "code_verifier": verifier, "client_id": CLIENT_ID}
    status, t = call("POST", f"{API}/oauth2/token", form=form, basic=bool(CLIENT_SECRET))
    if status != 200:
        sys.exit(f"token exchange failed: {t}")
    save(label, t)
    print("granted scopes:", t.get("scope"))


def me(label):
    return call("GET", f"{API}/users/me", token(label))


def users(label, *names):
    return call("GET", f"{API}/users/by?usernames={','.join(names)}&user.fields={FIELDS}",
                token(label))


def uid(label, name):
    status, d = call("GET", f"{API}/users/by/username/{name}", token(label))
    if status != 200 or "data" not in d:
        sys.exit(f"cannot resolve @{name}")
    return d["data"]["id"]


def send(label, name, text):
    return call("POST", f"{API}/dm_conversations/with/{uid(label, name)}/messages",
                token(label), body={"text": text})


def events(label, name=None):
    path = f"/dm_conversations/with/{uid(label, name)}/dm_events" if name else "/dm_events"
    return call("GET", f"{API}{path}?{EV}", token(label))


COMMANDS = {"auth": auth, "me": me, "refresh": refresh, "users": users,
            "send": send, "events": events}

if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    COMMANDS[sys.argv[1]](*sys.argv[2:])