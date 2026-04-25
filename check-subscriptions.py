#!/usr/bin/env python3
"""
HEX OS — check-subscriptions.py
Reads Gmail for subscription emails, updates data.json subs on GitHub,
and sends a WhatsApp alert if any subscription renews within 7 days.

SETUP
  1. Enable Gmail IMAP:
       Gmail → Settings (gear) → See all settings → Forwarding and POP/IMAP
       → IMAP access: Enable IMAP → Save

  2. Create a Gmail App Password (requires 2-Step Verification to be on):
       myaccount.google.com → Security → App Passwords
       → Select app: Mail  → Select device: Mac → Generate
       Copy the 16-char password.

  3. Set environment variables:
       export HEX_GH_TOKEN=ghp_xxxxxxxxxxxx      # GitHub PAT with repo scope
       export HEX_GMAIL_PASS=xxxx xxxx xxxx xxxx  # Gmail App Password

  4. Optional — WhatsApp webhook (your Hex bot's inbound URL):
       export HEX_WA_WEBHOOK='https://your-bot.com/send?to=%2B1234567890&msg={msg}'

  5. Run:
       python3 check-subscriptions.py
       python3 check-subscriptions.py --dry-run   # print findings, don't write to GitHub

WHAT IT DOES
  - Searches Gmail for emails from known subscription domains in the last 60 days
  - Extracts service name, renewal date, and amount via regex
  - Upserts into data.json subs[] on GitHub (matched by name, deduped)
  - Prints a summary table
  - Sends WhatsApp alert for any sub renewing within RENEWAL_WARN_DAYS days
"""

import os, sys, re, json, base64, time, imaplib, email, argparse
from email.header import decode_header as _decode_header
from datetime import datetime, timezone, timedelta, date
from typing import Optional
import urllib.request, urllib.error, urllib.parse

# ── CONFIG ──────────────────────────────────────────────────────
GH_TOKEN   = os.environ.get('HEX_GH_TOKEN', '')
GH_OWNER   = 'Shinnthantsteven'
GH_REPO    = 'hex-os'
GH_FILE    = 'data.json'

GMAIL_USER = 'shinnthantsteven@gmail.com'
GMAIL_PASS = os.environ.get('HEX_GMAIL_PASS', '')      # Gmail App Password
GMAIL_HOST = 'imap.gmail.com'

# Optional: Hex bot webhook. Include {msg} placeholder.
# e.g. 'https://your-bot.com/send?to=%2B1234567890&msg={msg}'
WA_WEBHOOK = os.environ.get('HEX_WA_WEBHOOK', '')

SEARCH_DAYS       = 60    # look back this many days in Gmail
RENEWAL_WARN_DAYS = 7     # alert if renewal is this soon

API_URL = f'https://api.github.com/repos/{GH_OWNER}/{GH_REPO}/contents/{GH_FILE}'

# ── KNOWN SUBSCRIPTION SENDERS ───────────────────────────────────
# key = domain fragment to match in the From header
# value = display name used in data.json
KNOWN_SUBS = {
    'netflix.com':          'Netflix',
    'spotify.com':          'Spotify',
    'adobe.com':            'Adobe',
    'anthropic.com':        'Claude',
    'tabby.ai':             'Tabby',
    'apple.com':            'Apple',
    'google.com':           'Google One',
    'youtube.com':          'YouTube Premium',
    'microsoft.com':        'Microsoft 365',
    'amazon.com':           'Amazon Prime',
    'github.com':           'GitHub',
    'openai.com':           'OpenAI',
    'dropbox.com':          'Dropbox',
    'notion.so':            'Notion',
    'figma.com':            'Figma',
    'canva.com':            'Canva',
    'grammarly.com':        'Grammarly',
    '1password.com':        '1Password',
    'expressvpn.com':       'ExpressVPN',
    'nordvpn.com':          'NordVPN',
    'proton.me':            'Proton',
    'protonmail.com':       'Proton',
    'icloud.com':           'iCloud+',
    'cloudflare.com':       'Cloudflare',
    'vercel.com':           'Vercel',
    'digitalocean.com':     'DigitalOcean',
    'heroku.com':           'Heroku',
    'linear.app':           'Linear',
    'loom.com':             'Loom',
    'zoom.us':              'Zoom',
    'slack.com':            'Slack',
}

# Subject keywords that indicate billing / renewal emails
BILLING_KEYWORDS = re.compile(
    r'receipt|invoice|payment|subscription|renewal|billing|charged|renewed|'
    r'your (bill|plan|membership|order)|thank you for (your )?(purchase|payment)|'
    r'payment confirmation|payment received|auto.?renew',
    re.IGNORECASE,
)

# ── DATE EXTRACTION ───────────────────────────────────────────────
MONTH_MAP = {
    'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,
    'july':7,'august':8,'september':9,'october':10,'november':11,'december':12,
    'jan':1,'feb':2,'mar':3,'apr':4,'jun':6,'jul':7,'aug':8,
    'sep':9,'oct':10,'nov':11,'dec':12,
}

DATE_RES = [
    # April 25, 2026  /  April 25 2026
    re.compile(r'\b(january|february|march|april|may|june|july|august|september|october|november|december|'
               r'jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\.?\s+(\d{1,2}),?\s+(\d{4})\b', re.IGNORECASE),
    # 25 April 2026
    re.compile(r'\b(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december|'
               r'jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\.?\s+(\d{4})\b', re.IGNORECASE),
    # 2026-04-25
    re.compile(r'\b(20\d{2})-(0[1-9]|1[0-2])-(\d{2})\b'),
    # 04/25/2026  or  25/04/2026
    re.compile(r'\b(\d{1,2})/(\d{1,2})/(20\d{2})\b'),
]

AMOUNT_RE = re.compile(
    r'(?:USD\s*|\$\s*|US\$\s*)(\d{1,4}(?:\.\d{2})?)'
    r'|(\d{1,4}(?:\.\d{2})?)\s*(?:USD|usd)',
    re.IGNORECASE,
)

def parse_date(text: str) -> Optional[str]:
    """Try to extract the earliest future (or recent) date from text. Returns 'YYYY-MM-DD' or None."""
    candidates = []
    today = date.today()

    for pattern in DATE_RES:
        for m in pattern.finditer(text):
            groups = m.groups()
            try:
                # Pattern 1: Month Day Year
                if re.match(r'[a-z]', groups[0], re.IGNORECASE):
                    month = MONTH_MAP.get(groups[0].lower()[:3])
                    day, year = int(groups[1]), int(groups[2])
                # Pattern 2: Day Month Year
                elif re.match(r'\d', groups[0]) and re.match(r'[a-z]', groups[1], re.IGNORECASE):
                    day = int(groups[0])
                    month = MONTH_MAP.get(groups[1].lower()[:3])
                    year = int(groups[2])
                # Pattern 3: YYYY-MM-DD
                elif len(groups[0]) == 4:
                    year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
                # Pattern 4: MM/DD/YYYY or DD/MM/YYYY — assume MM/DD if month ≤ 12
                else:
                    a, b, year = int(groups[0]), int(groups[1]), int(groups[2])
                    month, day = (a, b) if a <= 12 else (b, a)

                if month and 1 <= month <= 12 and 1 <= day <= 31 and 2024 <= year <= 2030:
                    d = date(year, month, day)
                    candidates.append(d)
            except (ValueError, TypeError):
                continue

    if not candidates:
        return None
    # Prefer future dates, else the most recent past date
    future = [d for d in candidates if d >= today]
    return str(min(future) if future else max(candidates))

def parse_amount(text: str) -> Optional[float]:
    """Extract first dollar amount from text."""
    m = AMOUNT_RE.search(text)
    if m:
        raw = m.group(1) or m.group(2)
        try:
            return round(float(raw), 2)
        except ValueError:
            pass
    return None

# ── GMAIL IMAP ────────────────────────────────────────────────────

def decode_header_value(val: str) -> str:
    parts = _decode_header(val)
    result = []
    for part, enc in parts:
        if isinstance(part, bytes):
            result.append(part.decode(enc or 'utf-8', errors='replace'))
        else:
            result.append(part)
    return ' '.join(result)

def get_email_text(msg) -> str:
    """Extract plain text from an email.Message object."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == 'text/plain':
                payload = part.get_payload(decode=True)
                if payload:
                    parts.append(payload.decode(part.get_content_charset() or 'utf-8', errors='replace'))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            parts.append(payload.decode(msg.get_content_charset() or 'utf-8', errors='replace'))
    return '\n'.join(parts)

def fetch_subscription_emails(days=SEARCH_DAYS):
    """
    Returns a list of dicts:
      {service, from, subject, date_received, renewal_date, amount}
    """
    if not GMAIL_PASS:
        print('ERROR: Set HEX_GMAIL_PASS to your Gmail App Password.')
        sys.exit(1)

    print(f'Connecting to Gmail IMAP as {GMAIL_USER}…')
    mail = imaplib.IMAP4_SSL(GMAIL_HOST)
    mail.login(GMAIL_USER, GMAIL_PASS)
    mail.select('inbox')

    since_date = (datetime.now() - timedelta(days=days)).strftime('%d-%b-%Y')

    # Build search query: FROM any known sender in the last N days
    results = []
    seen_ids = set()

    for domain, service_name in KNOWN_SUBS.items():
        # Search by sender domain
        _, data = mail.search(None, f'(SINCE {since_date} FROM "{domain}")')
        ids = data[0].split()
        if not ids:
            continue

        for uid in ids[-20:]:   # cap at 20 most recent per domain
            if uid in seen_ids:
                continue
            seen_ids.add(uid)

            _, raw = mail.fetch(uid, '(RFC822)')
            if not raw or not raw[0]:
                continue

            msg = email.message_from_bytes(raw[0][1])

            subject  = decode_header_value(msg.get('Subject', ''))
            from_hdr = decode_header_value(msg.get('From', ''))
            date_hdr = msg.get('Date', '')

            # Filter: only billing-related subjects
            if not BILLING_KEYWORDS.search(subject):
                continue

            body     = get_email_text(msg)
            combined = subject + '\n' + body

            renewal_date = parse_date(combined)
            amount       = parse_amount(combined)

            results.append({
                'service':      service_name,
                'from':         from_hdr,
                'subject':      subject,
                'date_received': date_hdr,
                'renewal_date': renewal_date,
                'amount':       amount,
            })

    mail.logout()
    return results

# ── GITHUB API ────────────────────────────────────────────────────

def gh_headers_dict():
    return {
        'Authorization': f'Bearer {GH_TOKEN}',
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
        'User-Agent': 'hex-os-check-subscriptions',
    }

def gh_get():
    url = API_URL + f'?t={int(time.time())}'
    req = urllib.request.Request(url, headers=gh_headers_dict())
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.loads(r.read())
    sha  = j['sha']
    data = json.loads(base64.b64decode(j['content'].replace('\n', '')).decode('utf-8'))
    return data, sha

def gh_put(data, sha, message='check-subscriptions update'):
    body = json.dumps({
        'message': message,
        'content': base64.b64encode(json.dumps(data, indent=2).encode('utf-8')).decode(),
        'sha': sha,
    }).encode('utf-8')
    req = urllib.request.Request(API_URL, data=body, headers=gh_headers_dict(), method='PUT')
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read())
        return j['content']['sha']
    except urllib.error.HTTPError as e:
        if e.code == 409:
            raise RuntimeError('SHA conflict — retry') from e
        raise

# ── DATA HELPERS ──────────────────────────────────────────────────

def uid():
    import random, string
    return f'{int(time.time()*1000):x}' + ''.join(random.choices(string.ascii_lowercase, k=4))

def days_until(date_str: str) -> int:
    try:
        target = datetime.strptime(date_str, '%Y-%m-%d').date()
        return (target - date.today()).days
    except ValueError:
        return 999

def upsert_subs(existing_subs: list, findings: list) -> tuple:
    """
    Merge email findings into the subs list.
    Matched by service name (case-insensitive).
    Returns (updated_subs, added_count, updated_count).
    """
    by_name = {s['name'].strip().lower(): s for s in existing_subs}
    added = updated = 0

    for f in findings:
        if not f['renewal_date']:
            continue  # skip if no date could be extracted

        name_key = f['service'].lower()
        if name_key in by_name:
            sub = by_name[name_key]
            old_renewal = sub.get('renewal', '')
            new_renewal = f['renewal_date']
            # Only update if the new date is later (renewal pushed forward)
            if new_renewal > old_renewal:
                sub['renewal'] = new_renewal
                if f['amount']:
                    sub['cost'] = str(f['amount'])
                updated += 1
        else:
            new_sub = {
                'id':      uid(),
                'name':    f['service'],
                'cost':    str(f['amount']) if f['amount'] else '',
                'renewal': f['renewal_date'],
            }
            existing_subs.append(new_sub)
            by_name[name_key] = new_sub
            added += 1

    return existing_subs, added, updated

# ── WHATSAPP NOTIFICATION ─────────────────────────────────────────

def send_wa_alert(message: str):
    if not WA_WEBHOOK:
        return
    try:
        url = WA_WEBHOOK.replace('{msg}', urllib.parse.quote(message))
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f'  WhatsApp alert sent (HTTP {r.status})')
    except Exception as e:
        print(f'  WhatsApp alert failed: {e}')

def build_wa_message(soon: list) -> str:
    lines = ['⚠️ HEX OS — Subscription Renewals']
    for s in soon:
        d = days_until(s['renewal'])
        cost = f"${float(s['cost']):.2f}" if s.get('cost') else ''
        lines.append(f"  • {s['name']} renews in {d}d {cost}")
    return '\n'.join(lines)

# ── MAIN ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='HEX OS Subscription Email Checker')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print findings only, do not write to GitHub')
    parser.add_argument('--days', type=int, default=SEARCH_DAYS,
                        help=f'Days to look back in Gmail (default {SEARCH_DAYS})')
    args = parser.parse_args()

    if not GH_TOKEN and not args.dry_run:
        print('ERROR: Set HEX_GH_TOKEN env var (GitHub PAT with repo scope).')
        sys.exit(1)

    # ── Step 1: fetch emails ──────────────────────────────────────
    print(f'\nScanning Gmail (last {args.days} days) for subscription emails…\n')
    findings = fetch_subscription_emails(args.days)

    if not findings:
        print('No subscription billing emails found.')
    else:
        print(f'Found {len(findings)} billing email(s):\n')
        print(f'  {"SERVICE":<20} {"RENEWAL":<12} {"AMOUNT":<10} SUBJECT')
        print('  ' + '─' * 70)
        for f in findings:
            renewal = f['renewal_date'] or '???'
            amount  = f'${f["amount"]:.2f}' if f['amount'] else '—'
            subj    = f['subject'][:45] + ('…' if len(f['subject']) > 45 else '')
            print(f'  {f["service"]:<20} {renewal:<12} {amount:<10} {subj}')

    # ── Step 2: update data.json ──────────────────────────────────
    if args.dry_run:
        print('\n[dry-run] Not writing to GitHub.')
    elif findings:
        print('\nFetching data.json from GitHub…')
        data, sha = gh_get()
        existing_subs = data.get('subs', [])
        if not isinstance(existing_subs, list):
            existing_subs = []

        updated_subs, added, updated = upsert_subs(existing_subs, findings)
        data['subs']    = updated_subs
        data['updated'] = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

        print(f'Saving to GitHub: {added} added, {updated} updated…')
        gh_put(data, sha)
        print('Saved.\n')

    # ── Step 3: check for upcoming renewals ───────────────────────
    print('\n─── Upcoming Renewals ──────────────────────────────────────────')
    try:
        if args.dry_run:
            all_subs = []  # no GitHub fetch in dry-run
        else:
            data, _ = gh_get()
            all_subs = data.get('subs', [])
    except Exception:
        all_subs = []

    if not all_subs:
        print('  No subscriptions in data.json yet.')
    else:
        soon = []
        print(f'  {"NAME":<20} {"RENEWAL":<12} {"DAYS":<6} {"COST"}')
        print('  ' + '─' * 50)
        for s in sorted(all_subs, key=lambda x: x.get('renewal', '9999')):
            name    = s.get('name', '?')
            renewal = s.get('renewal', '???')
            cost    = f'${float(s["cost"]):.2f}/mo' if s.get('cost') else '—'
            d       = days_until(renewal) if renewal != '???' else 999
            flag    = ' ⚠️' if d <= RENEWAL_WARN_DAYS else ''
            print(f'  {name:<20} {renewal:<12} {str(d)+"d":<6} {cost}{flag}')
            if d <= RENEWAL_WARN_DAYS:
                soon.append(s)

        if soon:
            print(f'\n  {len(soon)} subscription(s) renewing within {RENEWAL_WARN_DAYS} days!')
            msg = build_wa_message(soon)
            print(f'\nWhatsApp alert message:\n{msg}\n')
            if WA_WEBHOOK:
                send_wa_alert(msg)
            else:
                print('  (Set HEX_WA_WEBHOOK to send this as a WhatsApp message.)')

    print('\nDone.')

if __name__ == '__main__':
    main()
