#!/usr/bin/env python3
"""
HEX OS — hex-sync.py
Reads Mac Reminders, Calendar, and Gmail then writes everything to
data.json on GitHub. Runs forever with 5-minute intervals.

TOKEN FILE : ~/.openclaw/github_token          (GitHub PAT, repo scope)
GMAIL FILE : ~/.openclaw/gmail_app_password    (Gmail 16-char App Password)

USAGE
  python3 hex-sync.py              # run forever (Ctrl+C to stop)
  python3 hex-sync.py --once       # run once and exit
  python3 hex-sync.py --dry-run    # run once, print result, don't write GitHub

FIRST RUN
  1. GitHub token → paste into ~/.openclaw/github_token
  2. Gmail App Password (optional) →
       Gmail Settings → Forwarding/IMAP → Enable IMAP
       myaccount.google.com → Security → App Passwords → Mail/Mac
       Paste the 16-char password into ~/.openclaw/gmail_app_password
  3. When prompted, allow Terminal to access Reminders + Calendar in
     System Preferences → Privacy & Security → Automation
"""

import os, sys, json, base64, time, subprocess, signal, imaplib
import email, argparse, re, hashlib, traceback
from email.header import decode_header as _decode_header
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
import urllib.request, urllib.error

# ── CONFIG ───────────────────────────────────────────────────────
OPENCLAW_DIR   = Path.home() / '.openclaw'
GH_TOKEN_FILE  = OPENCLAW_DIR / 'github_token'
GMAIL_PASS_FILE= OPENCLAW_DIR / 'gmail_app_password'

GH_OWNER       = 'Shinnthantsteven'
GH_REPO        = 'hex-os'
GH_FILE        = 'data.json'
GMAIL_USER     = 'shinnthantsteven@gmail.com'
GMAIL_HOST     = 'imap.gmail.com'

SYNC_INTERVAL  = 300           # seconds between syncs (5 minutes)
CAL_DAYS_AHEAD = 7             # how many days of Calendar events to fetch
GMAIL_DAYS_BACK= 60            # how far back to scan Gmail

VERSION        = '1.0'
API_URL        = f'https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{GH_FILE}'

# ── KNOWN SUBSCRIPTION SENDERS ───────────────────────────────────
KNOWN_SUBS = {
    'netflix.com':       'Netflix',
    'spotify.com':       'Spotify',
    'adobe.com':         'Adobe',
    'anthropic.com':     'Claude',
    'tabby.ai':          'Tabby',
    'apple.com':         'Apple',
    'icloud.com':        'iCloud+',
    'google.com':        'Google One',
    'youtube.com':       'YouTube Premium',
    'amazon.com':        'Amazon Prime',
    'microsoft.com':     'Microsoft 365',
    'github.com':        'GitHub',
    'openai.com':        'OpenAI',
    'dropbox.com':       'Dropbox',
    'notion.so':         'Notion',
    'figma.com':         'Figma',
    'canva.com':         'Canva',
    'grammarly.com':     'Grammarly',
    '1password.com':     '1Password',
    'expressvpn.com':    'ExpressVPN',
    'nordvpn.com':       'NordVPN',
    'proton.me':         'Proton',
    'cloudflare.com':    'Cloudflare',
    'vercel.com':        'Vercel',
    'digitalocean.com':  'DigitalOcean',
    'loom.com':          'Loom',
    'zoom.us':           'Zoom',
    'linear.app':        'Linear',
}

BILLING_KW = re.compile(
    r'receipt|invoice|payment|subscription|renewal|billing|charged|renewed|'
    r'your (bill|plan|membership|order)|payment (confirmation|received)|auto.?renew',
    re.IGNORECASE,
)

MONTH_MAP = {
    'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
    'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12,
    'january':1,'february':2,'march':3,'april':4,'june':6,
    'july':7,'august':8,'september':9,'october':10,'november':11,'december':12,
}

DATE_RES = [
    re.compile(r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
               r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
               r'\.?\s+(\d{1,2}),?\s+(20\d{2})\b', re.IGNORECASE),
    re.compile(r'\b(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
               r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
               r'\.?\s+(20\d{2})\b', re.IGNORECASE),
    re.compile(r'\b(20\d{2})-(0[1-9]|1[0-2])-(\d{2})\b'),
    re.compile(r'\b(\d{1,2})/(\d{1,2})/(20\d{2})\b'),
]
AMOUNT_RE = re.compile(
    r'(?:USD\s*|\$\s*|US\$\s*)(\d{1,4}(?:\.\d{2})?)'
    r'|(\d{1,4}(?:\.\d{2})?)\s*(?:USD|usd)',
    re.IGNORECASE,
)

# ── HELPERS ───────────────────────────────────────────────────────

def read_secret(path: Path, env_var: str = '') -> str:
    """Read a secret from a file or env var. Returns '' if neither found."""
    if env_var:
        v = os.environ.get(env_var, '').strip()
        if v:
            return v
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ''

def uid(prefix=''):
    import random, string
    chars = string.ascii_lowercase + string.digits
    return prefix + ''.join(random.choices(chars, k=10))

def log(msg: str):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'[{ts}] {msg}', flush=True)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

# ── GITHUB API ────────────────────────────────────────────────────

_gh_token = ''

def gh_token() -> str:
    global _gh_token
    if not _gh_token:
        _gh_token = read_secret(GH_TOKEN_FILE, 'HEX_GH_TOKEN')
    return _gh_token

def gh_headers() -> dict:
    return {
        'Authorization': f'Bearer {gh_token()}',
        'Accept':        'application/vnd.github.v3+json',
        'Content-Type':  'application/json',
        'User-Agent':    'hex-sync/1.0',
    }

def gh_get() -> tuple:
    """Returns (data_dict, sha). Raises on error."""
    req = urllib.request.Request(API_URL + f'?t={int(time.time())}', headers=gh_headers())
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read())
    sha  = j['sha']
    data = json.loads(base64.b64decode(j['content'].replace('\n', '')).decode('utf-8'))
    return data, sha

def gh_put(data: dict, sha: str, msg: str = 'hex-sync update') -> str:
    """Write data.json to GitHub. Returns new sha. Raises on error."""
    encoded = base64.b64encode(json.dumps(data, indent=2, ensure_ascii=False).encode('utf-8')).decode()
    body = json.dumps({'message': msg, 'content': encoded, 'sha': sha}).encode('utf-8')
    req  = urllib.request.Request(API_URL, data=body, headers=gh_headers(), method='PUT')
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            j = json.loads(r.read())
        return j['content']['sha']
    except urllib.error.HTTPError as e:
        if e.code == 409:
            raise RuntimeError('SHA conflict') from e
        raise

# ── REMINDERS (JXA) ──────────────────────────────────────────────

REMINDERS_JXA = r"""
var app = Application('Reminders');
var result = [];
try {
    var lists = app.lists();
    for (var i = 0; i < lists.length; i++) {
        try {
            var rems = lists[i].reminders();
            for (var j = 0; j < rems.length; j++) {
                try {
                    var r = rems[j];
                    if (r.completed()) continue;
                    var p = 0;
                    try { p = r.priority(); } catch(e) {}
                    var due = null;
                    try { var d = r.dueDate(); if (d) due = d.toISOString(); } catch(e) {}
                    result.push({
                        id:       'rem_' + r.id(),
                        text:     r.name(),
                        priority: p >= 9 ? 'high' : (p >= 5 ? 'mid' : 'low'),
                        done:     false,
                        dueDate:  due,
                        list:     lists[i].name(),
                        source:   'reminders'
                    });
                } catch(e) {}
            }
        } catch(e) {}
    }
} catch(e) {}
JSON.stringify(result);
"""

def read_reminders() -> list:
    """Read all incomplete reminders from the Mac Reminders app."""
    try:
        r = subprocess.run(
            ['osascript', '-l', 'JavaScript', '-e', REMINDERS_JXA],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            log(f'Reminders JXA error: {r.stderr.strip()[:200]}')
            return []
        out = r.stdout.strip()
        if not out:
            return []
        return json.loads(out)
    except subprocess.TimeoutExpired:
        log('Reminders JXA timed out')
        return []
    except Exception as e:
        log(f'Reminders read failed: {e}')
        return []

# ── CALENDAR (JXA) ────────────────────────────────────────────────

def make_calendar_jxa(days_ahead: int) -> str:
    return f"""
var app = Application('Calendar');
var now = new Date();
var end = new Date(now.getTime() + {days_ahead} * 86400000);
var results = [];
try {{
    var cals = app.calendars();
    for (var i = 0; i < cals.length; i++) {{
        try {{
            var evs = cals[i].events();
            for (var j = 0; j < evs.length && results.length < 500; j++) {{
                try {{
                    var sd = evs[j].startDate();
                    if (!sd || sd < now || sd > end) continue;
                    var evId = 'cal_x';
                    try {{ evId = 'cal_' + evs[j].uid(); }} catch(e) {{ evId = 'cal_' + i + '_' + j; }}
                    var allDay = false;
                    try {{ allDay = evs[j].alldayEvent(); }} catch(e) {{}}
                    var timeStr = '';
                    if (!allDay) {{
                        var h = sd.getHours().toString().padStart(2,'0');
                        var m = sd.getMinutes().toString().padStart(2,'0');
                        timeStr = h + ':' + m;
                    }}
                    results.push({{
                        id:       evId,
                        name:     evs[j].summary(),
                        start:    sd.toISOString(),
                        time:     timeStr,
                        allDay:   allDay,
                        calendar: cals[i].name(),
                        source:   'calendar'
                    }});
                }} catch(e) {{}}
            }}
        }} catch(e) {{}}
    }}
}} catch(e) {{}}
results.sort(function(a,b){{ return a.start < b.start ? -1 : 1; }});
JSON.stringify(results);
"""

def read_calendar(days_ahead: int = CAL_DAYS_AHEAD) -> dict:
    """
    Returns events keyed by date: {"YYYY-MM-DD": [{id,name,time,allDay,calendar,source}]}
    """
    jxa = make_calendar_jxa(days_ahead)
    try:
        r = subprocess.run(
            ['osascript', '-l', 'JavaScript', '-e', jxa],
            capture_output=True, text=True, timeout=45,
        )
        if r.returncode != 0:
            log(f'Calendar JXA error: {r.stderr.strip()[:200]}')
            return {}
        out = r.stdout.strip()
        if not out:
            return {}
        flat = json.loads(out)
    except subprocess.TimeoutExpired:
        log('Calendar JXA timed out — skipping calendar sync this round')
        return {}
    except Exception as e:
        log(f'Calendar read failed: {e}')
        return {}

    # Group by date key
    keyed: dict = {}
    for ev in flat:
        try:
            dt   = datetime.fromisoformat(ev['start'].replace('Z', '+00:00'))
            dkey = dt.astimezone().strftime('%Y-%m-%d')
            keyed.setdefault(dkey, []).append(ev)
        except Exception:
            pass
    return keyed

# ── GMAIL (IMAP) ──────────────────────────────────────────────────

def gmail_pass() -> str:
    return read_secret(GMAIL_PASS_FILE, 'HEX_GMAIL_PASS')

def _decode_hdr(val: str) -> str:
    parts = _decode_header(val)
    out = []
    for raw, enc in parts:
        if isinstance(raw, bytes):
            out.append(raw.decode(enc or 'utf-8', errors='replace'))
        else:
            out.append(raw)
    return ' '.join(out)

def _email_text(msg) -> str:
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == 'text/plain':
                p = part.get_payload(decode=True)
                if p:
                    parts.append(p.decode(part.get_content_charset() or 'utf-8', errors='replace'))
    else:
        p = msg.get_payload(decode=True)
        if p:
            parts.append(p.decode(msg.get_content_charset() or 'utf-8', errors='replace'))
    return '\n'.join(parts)

def _parse_date(text: str):
    today = date.today()
    candidates = []
    for pat in DATE_RES:
        for m in pat.finditer(text):
            g = m.groups()
            try:
                if re.match(r'[a-z]', g[0], re.IGNORECASE):
                    month = MONTH_MAP.get(g[0].lower()[:3])
                    day, year = int(g[1]), int(g[2])
                elif re.match(r'[a-z]', g[1], re.IGNORECASE):
                    day   = int(g[0])
                    month = MONTH_MAP.get(g[1].lower()[:3])
                    year  = int(g[2])
                elif len(g[0]) == 4:
                    year, month, day = int(g[0]), int(g[1]), int(g[2])
                else:
                    a, b, year = int(g[0]), int(g[1]), int(g[2])
                    month, day = (a, b) if a <= 12 else (b, a)
                if month and 1 <= month <= 12 and 1 <= day <= 31 and 2024 <= year <= 2030:
                    candidates.append(date(year, month, day))
            except (ValueError, TypeError):
                pass
    if not candidates:
        return None
    future = [d for d in candidates if d >= today]
    return str(min(future) if future else max(candidates))

def _parse_amount(text: str):
    m = AMOUNT_RE.search(text)
    if m:
        raw = m.group(1) or m.group(2)
        try:
            return round(float(raw), 2)
        except ValueError:
            pass
    return None

def read_gmail_subs(days_back: int = GMAIL_DAYS_BACK) -> list:
    """
    Scans Gmail for subscription billing emails.
    Returns [{"id","name","cost","renewal","source"}]
    """
    pw = gmail_pass()
    if not pw:
        log('Gmail: no App Password found — skipping subscription scan')
        log('       Create ~/.openclaw/gmail_app_password with your 16-char App Password')
        return []

    found = {}   # name_lower → sub dict
    try:
        log('Gmail: connecting…')
        mail = imaplib.IMAP4_SSL(GMAIL_HOST)
        mail.login(GMAIL_USER, pw)
        mail.select('inbox')

        since = (datetime.now() - timedelta(days=days_back)).strftime('%d-%b-%Y')
        seen_uids: set = set()

        for domain, service in KNOWN_SUBS.items():
            _, data = mail.search(None, f'(SINCE {since} FROM "{domain}")')
            ids = data[0].split()
            if not ids:
                continue
            for uid_b in ids[-15:]:   # cap per sender
                if uid_b in seen_uids:
                    continue
                seen_uids.add(uid_b)
                _, raw = mail.fetch(uid_b, '(RFC822)')
                if not raw or not raw[0]:
                    continue
                msg     = email.message_from_bytes(raw[0][1])
                subject = _decode_hdr(msg.get('Subject', ''))
                if not BILLING_KW.search(subject):
                    continue
                body     = _email_text(msg)
                combined = subject + '\n' + body
                renewal  = _parse_date(combined)
                amount   = _parse_amount(combined)
                key      = service.lower()
                if key not in found or (renewal and renewal > found[key].get('renewal', '')):
                    found[key] = {
                        'id':      'sub_' + hashlib.md5(service.encode()).hexdigest()[:8],
                        'name':    service,
                        'cost':    str(amount) if amount else found.get(key, {}).get('cost', ''),
                        'renewal': renewal or found.get(key, {}).get('renewal', ''),
                        'source':  'gmail',
                    }

        mail.logout()
        log(f'Gmail: found {len(found)} subscription(s)')
    except imaplib.IMAP4.error as e:
        log(f'Gmail IMAP error: {e}')
        log('       Check your App Password in ~/.openclaw/gmail_app_password')
    except Exception as e:
        log(f'Gmail scan failed: {e}')

    return list(found.values())

# ── MERGE LOGIC ───────────────────────────────────────────────────

def merge_subs(existing: list, fresh: list) -> list:
    """Upsert fresh Gmail subs into existing list. Preserves manual entries."""
    by_name = {s['name'].lower(): s for s in existing}
    for f in fresh:
        key = f['name'].lower()
        if key in by_name:
            old = by_name[key]
            # Only update renewal if fresher date found
            if f.get('renewal') and f['renewal'] > old.get('renewal', ''):
                old['renewal'] = f['renewal']
            if f.get('cost'):
                old['cost'] = f['cost']
            old['source'] = 'gmail'
        else:
            by_name[key] = f
    return list(by_name.values())

def build_new_state(old: dict, reminders: list, cal_events: dict, subs: list) -> dict:
    """
    Merge fresh data into the existing state.
    - todos  : replaced wholesale from Reminders
    - events : future events replaced from Calendar; past events preserved
    - subs   : upserted from Gmail
    - spend  : untouched (manual)
    """
    today_str = date.today().isoformat()

    # Keep past calendar events, replace future ones
    old_events = old.get('events', {})
    if not isinstance(old_events, dict):
        old_events = {}
    past_events = {k: v for k, v in old_events.items() if k < today_str}
    merged_events = {**past_events, **cal_events}

    # Merge subs
    old_subs = old.get('subs', [])
    if not isinstance(old_subs, list):
        old_subs = []
    merged_subs = merge_subs(old_subs, subs)

    return {
        'todos':   reminders,
        'events':  merged_events,
        'subs':    merged_subs,
        'spend':   old.get('spend', {}),
        'updated': now_iso(),
        'sync_info': {
            'source':    'hex-sync.py',
            'version':   VERSION,
            'synced_at': now_iso(),
            'reminders': len(reminders),
            'events':    sum(len(v) for v in cal_events.values()),
            'subs':      len(merged_subs),
        },
    }

# ── SYNC CYCLE ────────────────────────────────────────────────────

def run_sync(dry_run: bool = False) -> bool:
    """Run one full sync cycle. Returns True on success."""
    log('━━━ Starting sync ━━━━━━━━━━━━━━━━━━━━━━━━')

    # 1. Reminders
    log('Reading Reminders…')
    reminders = read_reminders()
    log(f'  {len(reminders)} incomplete reminder(s)')

    # 2. Calendar
    log(f'Reading Calendar (next {CAL_DAYS_AHEAD} days)…')
    cal_events = read_calendar()
    total_evs  = sum(len(v) for v in cal_events.values())
    log(f'  {total_evs} event(s) across {len(cal_events)} day(s)')

    # 3. Gmail
    log('Scanning Gmail for subscriptions…')
    gmail_subs = read_gmail_subs()

    # 4. Fetch current data.json
    token = gh_token()
    if not token:
        log('ERROR: No GitHub token found.')
        log('       Create ~/.openclaw/github_token with your PAT (repo scope).')
        return False

    log('Fetching data.json from GitHub…')
    try:
        old_data, sha = gh_get()
    except Exception as e:
        log(f'GitHub fetch failed: {e}')
        return False

    # 5. Build new state
    new_data = build_new_state(old_data, reminders, cal_events, gmail_subs)

    if dry_run:
        log('DRY RUN — not writing to GitHub')
        print(json.dumps(new_data, indent=2))
        return True

    # 6. Write to GitHub
    log('Writing to GitHub…')
    retries = 2
    for attempt in range(retries):
        try:
            new_sha = gh_put(new_data, sha,
                             f'hex-sync: {len(reminders)} todos, {total_evs} events, {len(gmail_subs)} subs')
            log(f'Done. SHA={new_sha[:8]}…')
            log(f'  todos={len(reminders)} events={total_evs} subs={len(new_data["subs"])}')
            return True
        except RuntimeError as e:
            if 'SHA conflict' in str(e) and attempt < retries - 1:
                log('SHA conflict — re-fetching and retrying…')
                try:
                    old_data, sha = gh_get()
                    new_data = build_new_state(old_data, reminders, cal_events, gmail_subs)
                except Exception as fe:
                    log(f'Re-fetch failed: {fe}')
                    return False
            else:
                log(f'GitHub write failed: {e}')
                return False
        except Exception as e:
            log(f'GitHub write failed: {e}')
            return False

    return False

# ── MAIN LOOP ─────────────────────────────────────────────────────

_stop = False

def _handle_signal(sig, _frame):
    global _stop
    print()
    log('Received stop signal — finishing current cycle then exiting…')
    _stop = True

def main():
    parser = argparse.ArgumentParser(description='HEX OS sync daemon')
    parser.add_argument('--once',    action='store_true', help='Run once and exit')
    parser.add_argument('--dry-run', action='store_true', help='Print data, do not write GitHub')
    args = parser.parse_args()

    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Ensure ~/.openclaw/ exists
    OPENCLAW_DIR.mkdir(parents=True, exist_ok=True)

    if not gh_token():
        log(f'ERROR: GitHub token not found.')
        log(f'       Create {GH_TOKEN_FILE} with a PAT that has repo scope.')
        sys.exit(1)

    if args.once or args.dry_run:
        ok = run_sync(dry_run=args.dry_run)
        sys.exit(0 if ok else 1)

    log(f'HEX OS sync daemon started (interval={SYNC_INTERVAL}s)')
    log(f'Token file : {GH_TOKEN_FILE}')
    log(f'Gmail file : {GMAIL_PASS_FILE}')
    log('Press Ctrl+C to stop.\n')

    while not _stop:
        try:
            run_sync()
        except Exception:
            log('Unexpected error in sync cycle:')
            traceback.print_exc()

        if _stop or args.once:
            break

        next_run = datetime.now() + timedelta(seconds=SYNC_INTERVAL)
        log(f'Next sync at {next_run.strftime("%H:%M:%S")}  (sleeping {SYNC_INTERVAL}s)\n')

        # Sleep in 5s chunks so Ctrl+C is responsive
        slept = 0
        while slept < SYNC_INTERVAL and not _stop:
            time.sleep(5)
            slept += 5

    log('hex-sync.py stopped.')

if __name__ == '__main__':
    main()
