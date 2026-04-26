#!/usr/bin/env python3
"""
HEX OS — hex-sync.py
Reads Mac Reminders, Calendar, and Mac Mail then writes everything to
data.json on GitHub. Runs forever with 5-minute intervals.

TOKEN FILE : ~/.openclaw/github_token          (GitHub PAT, repo scope)

USAGE
  python3 hex-sync.py              # run forever (Ctrl+C to stop)
  python3 hex-sync.py --once       # run once and exit
  python3 hex-sync.py --dry-run    # run once, print result, don't write GitHub

FIRST RUN
  1. GitHub token → paste into ~/.openclaw/github_token
  2. When prompted, allow Terminal to access Reminders, Calendar, and Mail in
     System Preferences → Privacy & Security → Automation
"""

import os, sys, json, base64, time, subprocess, signal
import argparse, re, hashlib, traceback
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
import urllib.request, urllib.error

# ── CONFIG ───────────────────────────────────────────────────────
OPENCLAW_DIR   = Path.home() / '.openclaw'
GH_TOKEN_FILE  = OPENCLAW_DIR / 'github_token'

GH_OWNER       = 'Shinnthantsteven'
GH_REPO        = 'hex-os'
GH_FILE        = 'data.json'

SYNC_INTERVAL  = 300           # seconds between syncs (5 minutes)
CAL_DAYS_AHEAD = 7             # how many days of Calendar events to fetch
MAIL_MSGS_PER_INBOX = 50       # last N messages to scan per Mail inbox

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
    r'(?:AED\s*|USD\s*|\$\s*|US\$\s*)(\d{1,4}(?:\.\d{2})?)'
    r'|(\d{1,4}(?:\.\d{2})?)\s*(?:AED|USD)',
    re.IGNORECASE,
)
CURRENCY_RE = re.compile(r'\b(AED|USD|\$)', re.IGNORECASE)

# ── HELPERS ───────────────────────────────────────────────────────

def read_secret(path: Path, env_var: str = '') -> str:
    """Read a secret from a file or env var. Returns '' if neither found."""
    if env_var:
        v = os.environ.get(env_var, '').strip()
        if v:
            return v
    if not os.path.exists(path):
        return ''
    try:
        return path.read_text().strip()
    except OSError:
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

# ── REMINDERS (AppleScript) ──────────────────────────────────────

REMINDERS_AS = '''tell application "Reminders"
    set output to ""
    repeat with aList in every list
        set listName to name of aList
        set theReminders to (every reminder of aList whose completed is false)
        repeat with r in theReminders
            set rName to name of r
            set output to output & listName & "|" & rName & "\n"
        end repeat
    end repeat
    return output
end tell'''

def read_reminders() -> list:
    """
    Read all incomplete reminders from the Mac Reminders app via AppleScript.
    Returns a list of todo dicts: {id, text, priority, done, list, source}.
    """
    try:
        r = subprocess.run(
            ['osascript', '-e', REMINDERS_AS],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            log(f'Reminders AppleScript error: {r.stderr.strip()[:200]}')
            return []
        out = r.stdout.strip()
        if not out:
            return []
    except subprocess.TimeoutExpired:
        log('Reminders AppleScript timed out after 60s')
        return []
    except Exception as e:
        log(f'Reminders read failed: {e}')
        return []

    todos = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split('|', 1)
        if len(parts) != 2:
            continue
        list_name, text = parts[0].strip(), parts[1].strip()
        if not text:
            continue
        # Derive a stable id from list+text so re-runs don't create duplicates
        import hashlib as _hl
        stable_id = 'rem_' + _hl.md5(f'{list_name}|{text}'.encode()).hexdigest()[:12]
        todos.append({
            'id':       stable_id,
            'text':     text,
            'priority': 'mid',   # AppleScript output doesn't include priority; default mid
            'done':     False,
            'list':     list_name,
            'source':   'reminders',
        })
    return todos

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

# ── MAC MAIL (APPLESCRIPT) ────────────────────────────────────────

# Read the last 100 messages from the INBOX of both named accounts only.
MAIL_ACCOUNTS = ['shinnthantsteven@icloud.com', 'shinnthantsteven@gmail.com']

MAIL_AS = '''tell application "Mail"
    set targetAccounts to {"shinnthantsteven@icloud.com", "shinnthantsteven@gmail.com"}
    set output to ""
    repeat with anAccount in every account
        set acctAddr to email addresses of anAccount
        set acctMatch to false
        repeat with addr in acctAddr
            if addr is in targetAccounts then
                set acctMatch to true
                exit repeat
            end if
        end repeat
        if not acctMatch then
        else
            try
                set inboxMB to mailbox "INBOX" of anAccount
                set msgs to messages of inboxMB
                set total to count of msgs
                set startI to total - 99
                if startI < 1 then set startI to 1
                repeat with i from startI to total
                    try
                        set m to message i of inboxMB
                        set mSender to sender of m
                        set mSubject to subject of m
                        set mDate to date received of m
                        set output to output & mSender & "|||" & mSubject & "|||" & (mDate as string) & "\n"
                    end try
                end repeat
            end try
        end if
    end repeat
    return output
end tell'''

# ── Spam/noise filters — skip if subject matches any of these ────
SKIP_RE = re.compile(
    r'\b(unsubscribe|newsletter|weekly\s+digest|daily\s+digest|'
    r'top\s+stories|trending\s+now|here\'s\s+what|you\s+may\s+like|'
    r'recommended\s+for\s+you|new\s+follower|liked\s+your|commented\s+on|'
    r'friend\s+request|people\s+you\s+may\s+know|'
    r'sale|off\s+today|limited\s+offer|exclusive\s+deal|shop\s+now|'
    r'flash\s+sale|% off|promo\s+code|coupon)\b',
    re.IGNORECASE,
)

# ── Transaction keyword triggers ────────────────────────────────
TXN_KW = re.compile(
    r'\b(charged|payment\s+(made|processed|received|confirmation)|'
    r'debit\s+(alert|notification|order)|credit\s+(alert|notification)|'
    r'transfer\s+(sent|received|confirmation)|deposit|withdraw|'
    r'purchase\s+confirm|transaction\s+(alert|notification|complete)|'
    r'apple\s+pay|google\s+pay|spent|amount\s+due|bill\s+paid|'
    r'your\s+(order|purchase|payment|receipt)|receipt\s+for)\b',
    re.IGNORECASE,
)

# ── Subscription keyword triggers ───────────────────────────────
SUB_KW = re.compile(
    r'\b(subscription|renewal|auto.?renew|trial\s+end|'
    r'your\s+(plan|membership)|billing\s+(date|cycle|period)|'
    r'invoice|next\s+charge|will\s+be\s+charged)\b',
    re.IGNORECASE,
)

# ── Important email triggers ─────────────────────────────────────
IMP_RE = re.compile(
    r'\b(flight|booking\s*confirm|e.?ticket|boarding\s*pass|itinerary|'
    r'hotel\s*(booking|confirm|reserv)|check.in|reservation\s*confirm|'
    r'ILOE|insurance\s*(policy|renewal|confirm)|takaful|'
    r'government|ministry|municipality|DEWA|AMAN|SEWA|ICP|GDRFA|DLD|'
    r'RTA\s*fine|traffic\s*fine|'
    r'job\s*(offer|application)|interview|we\s*want\s*to\s*invite|hiring|'
    r'bank\s+statement|account\s+statement|'
    r'urgent|action\s+required|important\s+notice|verify\s+your\s+account)\b',
    re.IGNORECASE,
)

IMP_TYPE_RE = [
    (re.compile(r'\b(flight|boarding\s*pass|e.?ticket|itinerary)\b',                                    re.I), 'flight'),
    (re.compile(r'\b(hotel|check.in|reservation)\b',                                                    re.I), 'hotel'),
    (re.compile(r'\b(ILOE|GDRFA|ICP|DLD|municipality|ministry|government|RTA|DEWA|AMAN|SEWA|traffic\s*fine)\b', re.I), 'government'),
    (re.compile(r'\b(insurance|takaful|policy)\b',                                                      re.I), 'insurance'),
    (re.compile(r'\b(interview|job\s*offer|hiring|we\s*want\s*to\s*invite)\b',                          re.I), 'job'),
    (re.compile(r'\b(bank\s*statement|account\s*statement)\b',                                          re.I), 'statement'),
    (re.compile(r'\b(urgent|action\s*required|important\s*notice|verify)\b',                            re.I), 'urgent'),
]

# ── Subscription service patterns ────────────────────────────────
SUB_KEYWORDS = {
    'netflix':         'Netflix',
    'spotify':         'Spotify',
    'adobe':           'Adobe',
    'icloud':          'iCloud+',
    'apple one':       'Apple One',
    'apple tv':        'Apple TV+',
    'youtube premium': 'YouTube Premium',
    'amazon prime':    'Amazon Prime',
    'microsoft 365':   'Microsoft 365',
    'office 365':      'Microsoft 365',
    'github':          'GitHub',
    'chatgpt':         'OpenAI',
    'claude':          'Claude',
    'dropbox':         'Dropbox',
    'notion':          'Notion',
    'figma':           'Figma',
    'canva':           'Canva',
    'grammarly':       'Grammarly',
    '1password':       '1Password',
    'expressvpn':      'ExpressVPN',
    'nordvpn':         'NordVPN',
    'proton':          'Proton',
    'google one':      'Google One',
    'zoom':            'Zoom',
    'linear':          'Linear',
    'vercel':          'Vercel',
    'digitalocean':    'DigitalOcean',
    'tabby':           'Tabby',
    'tamara':          'Tamara',
    'noon':            'Noon',
}

# ── Transaction auto-category rules (order: specific → generic) ──
TXN_CAT_RULES = [
    # Banks / transfers first — before merchant checks
    (re.compile(r'\b(ENBD|Emirates\s*NBD|Emirates\s*National\s*Bank)\b', re.I), 'Bills',     'ENBD'),
    (re.compile(r'\b(FAB|First\s*Abu\s*Dhabi\s*Bank)\b',                 re.I), 'Bills',     'FAB'),
    (re.compile(r'\b(ADCB|Abu\s*Dhabi\s*Commercial\s*Bank)\b',           re.I), 'Bills',     'ADCB'),
    (re.compile(r'\b(Mashreq|Mashreqbank)\b',                             re.I), 'Bills',     'Mashreq'),
    (re.compile(r'\b(RAKBANK|RAK\s*Bank)\b',                             re.I), 'Bills',     'RAKBANK'),
    (re.compile(r'\b(CBD|Commercial\s*Bank\s*of\s*Dubai)\b',             re.I), 'Bills',     'CBD'),
    # Health
    (re.compile(r'\b(pharmacy|clinic|hospital|doctor|health|medical|dental|vision|DHA|HAAD)\b', re.I), 'Health', None),
    # Food delivery (before generic Careem)
    (re.compile(r'\bTalabat\b',               re.I), 'Food',      'Talabat'),
    (re.compile(r'\bNoon\s*Food\b',           re.I), 'Food',      'Noon Food'),
    (re.compile(r'\bCareem\s*(Food|Eats)\b',  re.I), 'Food',      'Careem Food'),
    # Grocery
    (re.compile(r'\bCarrefour\b',             re.I), 'Shopping',  'Carrefour'),
    (re.compile(r'\bSpinneys\b',              re.I), 'Shopping',  'Spinneys'),
    (re.compile(r'\bLuLu\b',                  re.I), 'Shopping',  'LuLu'),
    (re.compile(r'\bNoon\s*Grocery\b',        re.I), 'Shopping',  'Noon Grocery'),
    # Transport (generic Careem after food)
    (re.compile(r'\bUber\b',                  re.I), 'Transport', 'Uber'),
    (re.compile(r'\bCareem\b',                re.I), 'Transport', 'Careem'),
    (re.compile(r'\bRTA\b',                   re.I), 'Transport', 'RTA'),
    # Fun / entertainment
    (re.compile(r'\b(cinema|movie|ticket|concert|event|VOX|Reel|Novo)\b', re.I), 'Fun', None),
    # BNPL → Bills
    (re.compile(r'\bTabby\b',                 re.I), 'Bills',     'Tabby'),
    (re.compile(r'\bTamara\b',                re.I), 'Bills',     'Tamara'),
    # Apple Pay / generic
    (re.compile(r'\bApple\s*Pay\b',           re.I), 'Shopping',  'Apple Pay'),
]

def _sender_domain(sender: str) -> str:
    m = re.search(r'@([\w.-]+)', sender)
    return m.group(1).lower() if m else ''

def _parse_mail_date(date_str: str) -> str:
    """AppleScript date string → YYYY-MM-DD, '' on failure."""
    for fmt in (
        '%A, %B %d, %Y at %I:%M:%S %p',
        '%A, %d %B %Y at %I:%M:%S %p',
        '%B %d, %Y at %I:%M:%S %p',
        '%d %B %Y at %H:%M:%S',
        '%d %B %Y %H:%M:%S',
        '%Y-%m-%d',
    ):
        try:
            return datetime.strptime(date_str[:40].strip(), fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return ''

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

def _parse_amount_currency(text: str):
    """Returns (amount_float, currency_str) or (None, None)."""
    m = AMOUNT_RE.search(text)
    if not m:
        return None, None
    raw = m.group(1) or m.group(2)
    try:
        amount = round(float(raw), 2)
    except ValueError:
        return None, None
    cm = CURRENCY_RE.search(text[:m.start() + len(m.group()) + 5])
    currency = 'AED'
    if cm:
        sym = cm.group(1).upper()
        currency = 'AED' if sym == 'AED' else 'USD'
    return amount, currency

def read_mail_emails() -> tuple:
    """
    Scans the last 100 emails from iCloud and Gmail INBOX via AppleScript.
    Returns (subs_list, transactions_list, important_emails_list).
      transactions:    [{id, merchant, category, amount, currency, date, subject}]
      subs:            [{id, name, cost, currency, renewal, source}]
      important_emails:[{id, subject, sender, date, type}]  — capped at 10
    """
    try:
        r = subprocess.run(
            ['osascript', '-e', MAIL_AS],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            log(f'Mail AppleScript error: {r.stderr.strip()[:200]}')
            return [], [], []
        out = r.stdout.strip()
        if not out:
            log('Mail: no messages returned (check Mail.app access in Privacy settings)')
            return [], [], []
    except subprocess.TimeoutExpired:
        log('Mail AppleScript timed out after 120s — skipping mail scan')
        return [], [], []
    except Exception as e:
        log(f'Mail read failed: {e}')
        return [], [], []

    subs: dict         = {}   # service_lower → sub dict
    transactions: list = []
    important_emails: list = []
    seen_txn: set = set()
    seen_imp: set = set()

    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split('|||', 2)
        if len(parts) != 3:
            continue
        sender, subject, date_str = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not subject:
            continue

        # Drop promotions / noise
        if SKIP_RE.search(subject):
            continue

        combined      = sender + ' ' + subject
        received_date = _parse_mail_date(date_str)
        amount, currency = _parse_amount_currency(subject)

        # ── IMPORTANT ─────────────────────────────────────────────
        if IMP_RE.search(subject) and len(important_emails) < 10:
            imp_type = 'notice'
            for pat, itype in IMP_TYPE_RE:
                if pat.search(subject):
                    imp_type = itype
                    break
            imp_id = 'imp_' + hashlib.md5(f'{sender}{subject}'.encode()).hexdigest()[:8]
            if imp_id not in seen_imp:
                seen_imp.add(imp_id)
                important_emails.append({
                    'id':      imp_id,
                    'subject': subject[:150],
                    'sender':  sender[:100],
                    'date':    received_date,
                    'type':    imp_type,
                })

        # ── TRANSACTION ───────────────────────────────────────────
        if TXN_KW.search(subject) or (amount is not None and TXN_KW.search(combined)):
            merchant  = None
            category  = 'Other'
            for pat, cat, name in TXN_CAT_RULES:
                if pat.search(combined):
                    category = cat
                    merchant = name
                    break
            # If no specific merchant matched, derive one from sender domain
            if merchant is None:
                domain   = _sender_domain(sender)
                merchant = domain.split('.')[0].title() if domain else 'Unknown'

            txn_id = 'txn_' + hashlib.md5(f'{sender}{subject}{date_str}'.encode()).hexdigest()[:8]
            if txn_id not in seen_txn:
                seen_txn.add(txn_id)
                transactions.append({
                    'id':       txn_id,
                    'merchant': merchant,
                    'category': category,
                    'amount':   amount,
                    'currency': currency or 'AED',
                    'date':     received_date,
                    'subject':  subject[:120],
                })

        # ── SUBSCRIPTION ──────────────────────────────────────────
        if not SUB_KW.search(subject):
            continue

        service = None
        domain  = _sender_domain(sender)
        for d, name in KNOWN_SUBS.items():
            if domain.endswith(d):
                service = name
                break
        if service is None:
            subj_lower = subject.lower()
            for kw, name in SUB_KEYWORDS.items():
                if kw in subj_lower:
                    service = name
                    break
        if service is None:
            continue

        renewal  = _parse_date(subject + ' ' + date_str)
        key      = service.lower()
        existing = subs.get(key, {})
        if key not in subs or (renewal and renewal > existing.get('renewal', '')):
            subs[key] = {
                'id':       'sub_' + hashlib.md5(service.encode()).hexdigest()[:8],
                'name':     service,
                'cost':     str(amount) if amount is not None else existing.get('cost', ''),
                'currency': currency if amount is not None else existing.get('currency', 'AED'),
                'renewal':  renewal or existing.get('renewal', ''),
                'source':   'mail',
            }
        elif amount is not None and not existing.get('cost'):
            subs[key]['cost']     = str(amount)
            subs[key]['currency'] = currency

    log(f'Mail: {len(subs)} sub(s), {len(transactions)} transaction(s), {len(important_emails)} important')
    return list(subs.values()), transactions, important_emails

# ── MERGE LOGIC ───────────────────────────────────────────────────

def merge_subs(existing: list, fresh: list) -> list:
    """Upsert fresh Mail subs into existing list. Preserves manual entries."""
    by_name = {s['name'].lower(): s for s in existing}
    for f in fresh:
        key = f['name'].lower()
        if key in by_name:
            old = by_name[key]
            if f.get('renewal') and f['renewal'] > old.get('renewal', ''):
                old['renewal'] = f['renewal']
            if f.get('cost'):
                old['cost'] = f['cost']
            if f.get('currency'):
                old['currency'] = f['currency']
            old['source'] = 'mail'
        else:
            by_name[key] = f
    return list(by_name.values())

def build_new_state(old: dict, reminders: list, cal_events: dict,
                    subs: list, transactions: list, important_emails: list) -> dict:
    """
    Merge fresh data into the existing state.
    - todos           : replaced from Reminders
    - events          : future events replaced from Calendar; past preserved
    - subs            : upserted from Mail
    - transactions    : replaced from Mail scan
    - important_emails: replaced from Mail scan (last 10)
    - spend           : untouched (manual)
    """
    today_str = date.today().isoformat()

    old_events = old.get('events', {})
    if not isinstance(old_events, dict):
        old_events = {}
    past_events   = {k: v for k, v in old_events.items() if k < today_str}
    merged_events = {**past_events, **cal_events}

    old_subs    = old.get('subs', [])
    if not isinstance(old_subs, list):
        old_subs = []
    merged_subs = merge_subs(old_subs, subs)

    return {
        'todos':           reminders,
        'events':          merged_events,
        'subs':            merged_subs,
        'transactions':    transactions,
        'important_emails': important_emails,
        'spend':           old.get('spend', {}),
        'updated':         now_iso(),
        'sync_info': {
            'source':    'hex-sync.py',
            'version':   VERSION,
            'synced_at': now_iso(),
            'reminders': len(reminders),
            'events':    sum(len(v) for v in cal_events.values()),
            'subs':      len(merged_subs),
            'txns':      len(transactions),
            'important': len(important_emails),
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

    # 3. Mac Mail
    log('Scanning Mac Mail (iCloud + Gmail)…')
    mail_subs, mail_txns, mail_imp = read_mail_emails()

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
    new_data = build_new_state(old_data, reminders, cal_events, mail_subs, mail_txns, mail_imp)

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
                             f'hex-sync: {len(reminders)} todos, {total_evs} events, '
                             f'{len(mail_subs)} subs, {len(mail_txns)} txns, {len(mail_imp)} imp')
            log(f'Done. SHA={new_sha[:8]}…')
            log(f'  todos={len(reminders)} events={total_evs} subs={len(new_data["subs"])} '
                f'txns={len(mail_txns)} imp={len(mail_imp)}')
            return True
        except RuntimeError as e:
            if 'SHA conflict' in str(e) and attempt < retries - 1:
                log('SHA conflict — re-fetching and retrying…')
                try:
                    old_data, sha = gh_get()
                    new_data = build_new_state(old_data, reminders, cal_events,
                                               mail_subs, mail_txns, mail_imp)
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
