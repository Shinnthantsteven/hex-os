#!/usr/bin/env python3
"""
HEX OS — sync-reminders.py
Two-way sync between macOS Reminders app and data.json on GitHub.

MODES
  python3 sync-reminders.py          # bidirectional sync (default)
  python3 sync-reminders.py --push   # Reminders → GitHub only
  python3 sync-reminders.py --pull   # GitHub → Reminders only
  python3 sync-reminders.py --serve  # HTTP server on :7432 (real-time from dashboard)
  python3 sync-reminders.py --list   # print all Reminders and exit

SETUP
  export HEX_GH_TOKEN=ghp_xxxxxxxxxxxx   # GitHub PAT with repo scope
  pip3 install requests                   # only external dep

  For --serve mode (dashboard → Reminders in real-time):
    run this in a terminal: python3 sync-reminders.py --serve
    then add todos from the HEX OS dashboard and they'll appear in Reminders instantly.
"""

import os, sys, json, base64, time, subprocess, argparse, signal
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread

# ── CONFIG ──────────────────────────────────────────────────────
GH_TOKEN        = os.environ.get('HEX_GH_TOKEN', '')
GH_OWNER        = 'Shinnthantsteven'
GH_REPO         = 'hex-os'
GH_FILE         = 'data.json'
REMINDERS_LIST  = 'Reminders'      # Reminders list name to sync with
SERVER_PORT     = 7432
PRIORITY_MAP    = {'high': 9, 'mid': 5, 'low': 1}   # Reminders priority: 9=high, 5=mid, 1=low

API_URL = f'https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{GH_FILE}'

# ── GITHUB API ───────────────────────────────────────────────────

try:
    import urllib.request, urllib.error
    USE_URLLIB = True
except ImportError:
    USE_URLLIB = False

def gh_headers():
    return {
        'Authorization': f'Bearer {GH_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
        'User-Agent': 'hex-os-sync',
    }

def gh_get():
    """Fetch data.json from GitHub. Returns (data_dict, sha) or raises."""
    import urllib.request, urllib.error
    url = API_URL + f'?t={int(time.time())}'
    req = urllib.request.Request(url, headers=gh_headers())
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.loads(r.read())
    sha  = j['sha']
    raw  = json.loads(base64.b64decode(j['content'].replace('\n', '')).decode('utf-8'))
    return raw, sha

def gh_put(data, sha, message='HEX OS sync-reminders'):
    """Write data.json to GitHub. Returns new sha or raises."""
    import urllib.request, urllib.error
    body = json.dumps({
        'message': message,
        'content': base64.b64encode(json.dumps(data, indent=2).encode('utf-8')).decode(),
        'sha': sha,
    }).encode('utf-8')
    req = urllib.request.Request(API_URL, data=body, headers=gh_headers(), method='PUT')
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read())
        return j['content']['sha']
    except urllib.error.HTTPError as e:
        if e.code == 409:
            raise RuntimeError('SHA conflict — retry') from e
        raise

# ── APPLESCRIPT / JXA ────────────────────────────────────────────

def run_as(script):
    """Run AppleScript, return stdout string."""
    r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'AppleScript error: {r.stderr.strip()}')
    return r.stdout.strip()

def run_jxa(script):
    """Run JavaScript for Automation, return stdout string."""
    r = subprocess.run(['osascript', '-l', 'JavaScript', '-e', script],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'JXA error: {r.stderr.strip()}')
    return r.stdout.strip()

def get_reminders_from_app():
    """
    Returns list of dicts: {id, name, priority_str}
    Reads only incomplete reminders from REMINDERS_LIST.
    """
    jxa = f"""
    var app = Application('Reminders');
    var result = [];
    try {{
        var lists = app.lists();
        for (var i = 0; i < lists.length; i++) {{
            if (lists[i].name() === '{REMINDERS_LIST}') {{
                var rems = lists[i].reminders();
                for (var j = 0; j < rems.length; j++) {{
                    var rem = rems[j];
                    if (!rem.completed()) {{
                        var p = rem.priority();
                        var pStr = p >= 9 ? 'high' : (p >= 5 ? 'mid' : 'low');
                        result.push({{id: rem.id(), name: rem.name(), priority: pStr}});
                    }}
                }}
                break;
            }}
        }}
    }} catch(e) {{}}
    JSON.stringify(result);
    """
    out = run_jxa(jxa)
    return json.loads(out) if out else []

def add_reminder_to_app(text, priority='mid'):
    """Add a single reminder to the REMINDERS_LIST list."""
    p_num = PRIORITY_MAP.get(priority, 5)
    # Escape text for AppleScript
    safe = text.replace('\\', '\\\\').replace('"', '\\"')
    script = f"""
    tell application "Reminders"
        tell list "{REMINDERS_LIST}"
            make new reminder with properties {{name:"{safe}", priority:{p_num}}}
        end tell
    end tell
    """
    run_as(script)

def complete_reminder_in_app(reminder_id):
    """Mark a reminder complete by its Reminders.app ID."""
    script = f"""
    tell application "Reminders"
        set theReminder to first reminder of list "{REMINDERS_LIST}" whose id is "{reminder_id}"
        set completed of theReminder to true
    end tell
    """
    try:
        run_as(script)
    except RuntimeError:
        pass  # already gone

# ── NORMALISE data.json ───────────────────────────────────────────

CATS = ['Food', 'Transport', 'Shopping', 'Bills', 'Health', 'Fun']

def normalise(raw):
    spend = raw.get('spend', {})
    if any(isinstance(spend.get(c), (int, float)) for c in CATS):
        mk = datetime.now(timezone.utc).strftime('%Y-%m')
        prev, spend = spend, {mk: {'entries': []}}
        for c in CATS:
            spend[mk][c] = prev.get(c, 0)
    return {
        'todos':   raw.get('todos', []) if isinstance(raw.get('todos'), list) else [],
        'events':  raw.get('events', {}) if isinstance(raw.get('events'), dict) else {},
        'spend':   spend,
        'subs':    raw.get('subs', []) if isinstance(raw.get('subs'), list) else [],
        'updated': raw.get('updated', ''),
    }

def uid():
    import random, string
    return f'{int(time.time()*1000):x}' + ''.join(random.choices(string.ascii_lowercase, k=4))

# ── SYNC LOGIC ────────────────────────────────────────────────────

def sync_push(data, verbose=True):
    """
    Push Reminders → data.json.
    Adds reminders that exist in the app but not in data.json todos.
    """
    app_reminders = get_reminders_from_app()
    existing_texts = {t['text'].strip().lower() for t in data['todos']}

    added = 0
    for rem in app_reminders:
        text = rem['name'].strip()
        if text.lower() not in existing_texts:
            data['todos'].insert(0, {
                'id':       uid(),
                'text':     text,
                'priority': rem['priority'],
                'done':     False,
                '_remId':   rem['id'],   # store Reminders ID for future dedup
            })
            existing_texts.add(text.lower())
            added += 1
            if verbose:
                print(f'  + Imported from Reminders: [{rem["priority"].upper()}] {text}')

    return data, added

def sync_pull(data, verbose=True):
    """
    Pull data.json → Reminders.
    Adds todos that exist in data.json but not in the Reminders app.
    """
    app_reminders = get_reminders_from_app()
    app_texts = {r['name'].strip().lower() for r in app_reminders}

    added = 0
    for todo in data['todos']:
        if todo.get('done'):
            continue
        text = todo['text'].strip()
        if text.lower() not in app_texts:
            priority = todo.get('priority', 'mid')
            add_reminder_to_app(text, priority)
            app_texts.add(text.lower())
            added += 1
            if verbose:
                print(f'  + Pushed to Reminders: [{priority.upper()}] {text}')

    return added

def do_sync(mode='both', verbose=True):
    if not GH_TOKEN:
        print('ERROR: Set HEX_GH_TOKEN env var first.')
        sys.exit(1)

    if verbose:
        print(f'[{datetime.now().strftime("%H:%M:%S")}] Fetching data.json from GitHub…')

    raw, sha = gh_get()
    data = normalise(raw)

    push_count = pull_count = 0

    if mode in ('push', 'both'):
        if verbose:
            print('Reading Reminders app…')
        data, push_count = sync_push(data, verbose)

    if mode in ('pull', 'both'):
        if verbose:
            print('Checking todos to push to Reminders…')
        pull_count = sync_pull(data, verbose)

    if push_count > 0:
        data['updated'] = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
        sha = gh_put(data, sha, f'sync-reminders: +{push_count} from Reminders')
        if verbose:
            print(f'Saved {push_count} new todo(s) to GitHub.')

    if verbose:
        print(f'Done. Imported: {push_count}  Pushed: {pull_count}')

# ── HTTP SERVER (for real-time dashboard → Reminders) ─────────────

class ReminderHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f'[server] {fmt % args}')

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path != '/reminder':
            self.send_response(404); self.end_headers(); return
        try:
            length = int(self.headers.get('Content-Length', 0))
            body   = json.loads(self.rfile.read(length))
            text     = body.get('text', '').strip()
            priority = body.get('priority', 'mid')
            if not text:
                self.send_response(400); self._cors(); self.end_headers(); return

            add_reminder_to_app(text, priority)
            print(f'[server] Created reminder: [{priority.upper()}] {text}')

            # Also update data.json on GitHub if token is set
            if GH_TOKEN:
                try:
                    raw, sha = gh_get()
                    data = normalise(raw)
                    existing = {t['text'].strip().lower() for t in data['todos']}
                    if text.lower() not in existing:
                        data['todos'].insert(0, {'id': uid(), 'text': text, 'priority': priority, 'done': False})
                        data['updated'] = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
                        gh_put(data, sha, f'sync-reminders server: {text}')
                except Exception as e:
                    print(f'[server] GitHub update failed: {e}')

            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'ok': True}).encode())
        except Exception as e:
            print(f'[server] Error: {e}')
            self.send_response(500); self._cors(); self.end_headers()

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

def run_server():
    httpd = HTTPServer(('127.0.0.1', SERVER_PORT), ReminderHandler)
    print(f'HEX OS Reminders server running on http://localhost:{SERVER_PORT}')
    print('Dashboard todos will be added to Reminders in real-time.')
    print('Press Ctrl+C to stop.\n')

    # Optional: run a sync on startup
    try:
        print('Running initial sync…')
        do_sync('both', verbose=True)
        print()
    except Exception as e:
        print(f'Initial sync failed: {e}\n')

    # Periodic sync every 5 minutes
    def periodic():
        while True:
            time.sleep(300)
            try:
                do_sync('push', verbose=False)
            except Exception as e:
                print(f'[server] Periodic sync error: {e}')

    t = Thread(target=periodic, daemon=True)
    t.start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')

# ── CLI ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='HEX OS Reminders Sync')
    group  = parser.add_mutually_exclusive_group()
    group.add_argument('--push',  action='store_true', help='Reminders → GitHub only')
    group.add_argument('--pull',  action='store_true', help='GitHub → Reminders only')
    group.add_argument('--serve', action='store_true', help='Start HTTP server on :7432')
    group.add_argument('--list',  action='store_true', help='List all Reminders and exit')
    args = parser.parse_args()

    if args.list:
        rems = get_reminders_from_app()
        if not rems:
            print('No incomplete reminders found.')
        for i, r in enumerate(rems, 1):
            print(f'{i:2}. [{r["priority"].upper():4}] {r["name"]}')
        return

    if args.serve:
        run_server()
        return

    mode = 'push' if args.push else ('pull' if args.pull else 'both')
    do_sync(mode)

if __name__ == '__main__':
    main()
