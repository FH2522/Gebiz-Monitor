bash


"""
bot.py — GeBIZ Telegram Bot (long-running, runs on your local machine)

Polls Telegram for commands (/summary, /open, /closed, /awarded, /start, /stop)
and replies to senders. Reads gebiz_data.json from the GitHub repo (raw URL)
so it always has fresh data without scraping.

New subscribers are added to local tg_subscribers.json and periodically
pushed to GitHub via git so the next GitHub Actions run picks them up.

Environment variables:
  TG_TOKEN      — Telegram bot token (required)
  TG_CHAT_ID    — Owner's chat ID (first subscriber on fresh install)
  GITHUB_RAW    — Raw URL prefix for the repo, e.g.
                  https://raw.githubusercontent.com/USER/REPO/main/
"""

import sys, io, os, time, json, logging, re, urllib.request, urllib.parse, subprocess
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ── Config ────────────────────────────────────────────────────────────────────
DATA_FILE        = "gebiz_data.json"        # local cached copy
SUBSCRIBERS_FILE = "tg_subscribers.json"
LOG_FILE         = "gebiz_bot.log"

TG_TOKEN     = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT_ID   = os.environ.get("TG_CHAT_ID", "").strip()
GITHUB_RAW   = os.environ.get("GITHUB_RAW", "").strip().rstrip("/") + "/"

POLL_INTERVAL    = 5      # seconds between Telegram getUpdates
DATA_REFRESH_SEC = 60     # how often to re-fetch JSON from GitHub
GIT_PUSH_SEC     = 300    # how often to push subscriber changes to GitHub

# Set working directory to script's folder
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_BASE_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

def load_subscribers():
    if Path(SUBSCRIBERS_FILE).exists():
        with open(SUBSCRIBERS_FILE, encoding='utf-8') as f:
            return set(json.load(f))
    return {TG_CHAT_ID} if TG_CHAT_ID else set()

def save_subscribers(subs):
    with open(SUBSCRIBERS_FILE, "w", encoding='utf-8') as f:
        json.dump(list(subs), f)

_subscribers = load_subscribers()
_subscribers_dirty = False   # set when changed; triggers git push
_tg_last_update_id = 0

def tg_enabled():
    return bool(TG_TOKEN)

def _proj_info(title):
    """Extract short project name and DU count from tender title.
    Returns (name, du) where name is 'Project XX', 'NxxCxx', or the full title if no code found.
    """
    # Try PROJECT XX
    m = re.search(r'PROJECT\s+([A-Z]{2,3})\b', title)
    if m:
        name = f"Project {m.group(1)}"
    else:
        # Try NEIGHBOURHOOD XX CONTRACT XX → NxxCxx
        m2 = re.search(r'NEIGHBOURHOOD\s+(\w+)\s+CONTRACT\s+(\w+)', title, re.IGNORECASE)
        if m2:
            name = f"N{m2.group(1)}C{m2.group(2)}"
        else:
            # No code found — use the full title (trimmed to 60 chars)
            name = title.strip()[:60]
    du_m = re.search(r'TOTAL:?\s*([\d,]+)\s*(?:DWELLING\s*UNITS?|DUs?)\b', title, re.IGNORECASE)
    du   = du_m.group(1) if du_m else "—"
    return name, du

def fmtm(n):
    """Format a dollar amount compactly: S$303.8M, S$1.03B, S$15.6K."""
    if not n:
        return "—"
    if n >= 1_000_000_000:
        return f"S${n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"S${v:.0f}M" if v == int(v) else f"S${v:.1f}M"
    if n >= 1_000:
        v = n / 1_000
        return f"S${v:.0f}K" if v == int(v) else f"S${v:.1f}K"
    return f"S${n:,.0f}"

def _tg_send_one(chat_id, text):
    """Send to a single chat ID."""
    try:
        url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        body = json.dumps({
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        log.warning("Telegram send error to %s: %s", chat_id, e)
        return False

def tg_send(text, target_id=None):
    """Broadcast to all subscribers, or one chat if target_id given."""
    if not tg_enabled():
        return
    targets = [target_id] if target_id else list(_subscribers)
    for cid in targets:
        if cid:
            _tg_send_one(cid, text)
    if not target_id:
        log.info("Telegram broadcast (%d users): %s", len(targets), text[:50])

def tg_fmt_prices_updated(t):
    """Format a notification when shortlisted bid prices are published for a two-envelope tender."""
    name, du = _proj_info(t.get('title',''))
    du_str = f" · {du} DU" if du != "—" else ""
    resp   = t.get('respondents', [])
    priced = [r for r in resp if not r.get('price_pending')]
    lines = [
        "💰 <b>Shortlisted Bid Prices Published</b>", "",
        f"<b>{name}</b>{du_str}",
        f"<code>{t.get('title','')[:90]}</code>",
        "",
        f"📅 Closed:      {t.get('close_date','—')}",
        f"👥 Shortlisted: {len(priced)} suppliers",
    ]
    if priced:
        lines += ["", "<b>Shortlisted Bids (Option A):</b>"]
        for i, r in enumerate(priced[:6]):
            medal = ["🥇","🥈","🥉"][i] if i < 3 else f"{i+1:2}."
            price = fmtm(r['opt_a']) if r.get('opt_a') else "S$—"
            lines.append(f"<code>{medal} {price}  {r['name'][:30]}</code>")
        if len(priced) > 6:
            lines.append(f"<i>  ...and {len(priced)-6} more</i>")
        if len(priced) > 1:
            lo = priced[0].get('opt_a', 0)
            hi = priced[-1].get('opt_a', 0)
            spread = f"{(hi-lo)/lo*100:.1f}%" if lo else "N/A"
            lines += ["", f"📊 Spread: {spread}  |  Lowest: {fmtm(lo)}"]
    lines += ["", "⏳ <i>Tender still under evaluation — award pending.</i>"]
    if t.get('url'):
        lines += ["", f"🔗 <a href=\"{t['url']}\">View on GeBIZ</a>"]
    return "\n".join(lines)

def tg_fmt_open(t):
    """Format a new open tender alert."""
    name, du = _proj_info(t.get('title',''))
    lps   = t.get('tender_type') == 'lps'
    label = "🔧 <b>New Open Tender — LPS</b>" if lps else "🟢 <b>New Open Tender</b>"
    du_str = f" · {du} DU" if du != "—" else ""
    lines = [
        label, "",
        f"<b>{name}</b>{du_str}",
        "",
        f"📅 Published: {t.get('published','—')}",
        f"🕐 Closes:    {t.get('close_date','—')}",
        f"📄 {t.get('doc_no','—')}",
    ]
    if t.get('url'):
        lines += ["", f"🔗 <a href=\"{t['url']}\">View on GeBIZ</a>"]
    return "\n".join(lines)

def tg_fmt_closed(t):
    """Format a closed tender alert with bid prices."""
    name, du = _proj_info(t.get('title',''))
    lps   = t.get('tender_type') == 'lps'
    label = "🔧 <b>LPS Tender Closed — Bids Received</b>" if lps else "🔴 <b>Tender Closed — Bids Received</b>"
    du_str = f" · {du} DU" if du != "—" else ""
    resp    = t.get('respondents', [])
    priced  = [r for r in resp if not r.get('price_pending')]
    pending = [r for r in resp if r.get('price_pending')]
    lines = [
        label, "",
        f"<b>{name}</b>{du_str}",
        "",
        f"📅 Published:   {t.get('published','—')}",
        f"📅 Closed:      {t.get('close_date','—')}",
        f"👥 Respondents: {len(resp)}",
    ]
    if priced:
        lines += ["", "<b>Top 3 Lowest Bids (Option A):</b>"]
        for i, r in enumerate(priced[:3]):
            medal = ["🥇","🥈","🥉"][i]
            price = fmtm(r['opt_a']) if r.get('opt_a') else "S$—"
            lines.append(f"<code>{medal} {price}  {r['name'][:30]}</code>")
        if len(priced) > 3:
            lines.append(f"<i>  ...and {len(priced)-3} more bidders</i>")
        if len(priced) > 1:
            lo = priced[0].get('opt_a', 0)
            hi = priced[-1].get('opt_a', 0)
            spread = f"{(hi-lo)/lo*100:.1f}%" if lo else "N/A"
            lines += ["", f"📊 Spread: {spread}  |  Lowest: {fmtm(lo)}"]
    if pending:
        lines += ["", f"⏳ {len(pending)} prices not yet published"]
    if t.get('url'):
        lines += ["", f"🔗 <a href=\"{t['url']}\">View on GeBIZ</a>"]
    return "\n".join(lines)

def tg_fmt_awarded(t):
    """Format an awarded (two-envelope) tender alert."""
    name, du = _proj_info(t.get('title',''))
    du_str = f" · {du} DU" if du != "—" else ""
    lines = [
        "✦ <b>Tender Awarded</b>", "",
        f"<b>{name}</b>{du_str}",
        "",
        f"🏆 Awarded to:    <b>{t.get('awarded_to','—')}</b>",
    ]
    if t.get('awarded_value'):
        lines.append(f"💰 Awarded value: <b>{fmtm(t['awarded_value'])}</b>")
    lines += [
        f"📅 Awarded date:  {t.get('awarded_date','—')}",
        f"📅 Closed:        {t.get('close_date','—')}",
    ]
    resp = t.get('respondents', [])
    if resp:
        lines += ["", f"<b>Shortlisted bids ({len(resp)}):</b>"]
        for i, r in enumerate(resp[:6]):
            winner_mark = "🏆" if t.get('awarded_to') and r['name'] == t['awarded_to'] else f"{i+1:2}."
            price = fmtm(r['opt_a']) if r.get('opt_a') else "S$—"
            lines.append(f"<code>{winner_mark} {price}  {r['name'][:30]}</code>")
    if t.get('url'):
        lines += ["", f"🔗 <a href=\"{t['url']}\">View on GeBIZ</a>"]
    return "\n".join(l for l in lines if l is not None)

def tg_summary(data):
    """Format a /summary command response. Returns the overview only — details sent separately."""
    open_t    = data.get('open', [])
    closed_t  = data.get('closed', [])
    awarded_t = [t for t in closed_t if t.get('two_envelope') and not t.get('under_evaluation')]
    normal_t  = [t for t in closed_t if not t.get('two_envelope') or t.get('under_evaluation')]

    lines = [
        "📊 <b>GeBIZ Monitor — Summary</b>",
        f"<i>{data.get('last_updated','—')}</i>",
        "",
        f"🟢 Open:    <b>{len(open_t)}</b>",
        f"🔴 Closed:  <b>{len(normal_t)}</b>",
        f"✦  Awarded: <b>{len(awarded_t)}</b>",
        "",
        "Use /open · /closed · /awarded for details.",
    ]

    if open_t:
        lines += ["", "─── Open Now ───"]
        for t in open_t:
            name, du = _proj_info(t.get('title',''))
            du_str = f" · {du} DU" if du != "—" else ""
            lines.append(f"🟢 <b>{name}</b>{du_str}  closes {t.get('close_date','—')}")

    if awarded_t:
        lines += ["", "─── Recently Awarded ───"]
        for t in awarded_t:
            name, du = _proj_info(t.get('title',''))
            du_str = f" · {du} DU" if du != "—" else ""
            lines.append(f"✦ <b>{name}</b>{du_str}  🏆 {t.get('awarded_to','—')[:30]}")

    return "\n".join(lines)

def load_data():
    if Path(DATA_FILE).exists():
        with open(DATA_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {"open": [], "closed": [], "run_count": 0}

def save_data(data):
    with open(DATA_FILE, "w", encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def _reply_open(open_t, sender=None):
    if not open_t:
        tg_send("No open tenders right now.", target_id=sender); return
    lines = ["🟢 <b>Open Tenders</b>"]
    for t in open_t:
        name, du = _proj_info(t.get('title',''))
        du_str = f" · {du} DU" if du != "—" else ""
        lines += [
            "",
            f"<b>{name}</b>{du_str}",
            f"  📅 Published: {t.get('published','—')}",
            f"  🕐 Closes:    {t.get('close_date','—')}",
            f"  📄 {t.get('doc_no','')}",
        ]
        if t.get('url'):
            lines.append(f"  🔗 <a href=\"{t['url']}\">GeBIZ</a>")
    tg_send("\n".join(lines), target_id=sender)

def _reply_closed(closed_t, sender=None):
    if not closed_t:
        tg_send("No closed tenders with data yet.", target_id=sender); return
    sorted_t = sorted(closed_t, key=lambda x: x.get('close_date',''), reverse=True)
    tg_send(f"🔴 <b>Closed Tenders ({len(sorted_t)})</b>", target_id=sender)
    for t in sorted_t:
        name, du     = _proj_info(t.get('title',''))
        du_str       = f" · {du} DU" if du != "—" else ""
        resp         = t.get('respondents', [])
        priced       = [r for r in resp if not r.get('price_pending')]
        pending      = [r for r in resp if r.get('price_pending')]
        is_two_env   = t.get('two_envelope')
        under_eval   = t.get('under_evaluation')

        # Status tag
        if is_two_env and under_eval:
            status_tag = "  ⏳ <i>Two-Envelope · Under Evaluation (prices pending)</i>"
        elif is_two_env:
            status_tag = "  🔖 <i>Two-Envelope · Pending Award</i>"
        else:
            status_tag = ""

        lines = [
            f"<b>{name}</b>{du_str}",
            f"  📅 {t.get('published','—')} → {t.get('close_date','—')}",
            f"  👥 {len(resp)} respondents",
        ]
        if status_tag:
            lines.append(status_tag)

        if priced:
            lines.append("  <b>Bids (Option A):</b>")
            for i, r in enumerate(priced):
                medal = ["🥇","🥈","🥉"][i] if i < 3 else f"  {i+1}."
                price = fmtm(r['opt_a']) if r.get('opt_a') else "—"
                lines.append(f"  {medal} {price}  {r['name'][:26]}")

        if pending:
            lines.append(f"  ⏳ Prices pending ({len(pending)} respondents):")
            for r in pending:
                lines.append(f"     · {r['name'][:32]}")

        if t.get('url'):
            lines.append(f"  🔗 <a href=\"{t['url']}\">GeBIZ</a>")

        # Safety: Telegram limit is 4096 chars — truncate if needed
        msg = "\n".join(lines)
        if len(msg) > 4000:
            msg = msg[:3900] + "\n  <i>...truncated — view full list on GeBIZ</i>"
        tg_send(msg, target_id=sender)

def _reply_awarded(awarded_t, sender=None):
    if not awarded_t:
        tg_send("No awarded tenders yet.", target_id=sender); return
    tg_send(f"✦ <b>Awarded Tenders ({len(awarded_t)})</b>", target_id=sender)
    for t in awarded_t:
        name, du = _proj_info(t.get('title',''))
        du_str = f" · {du} DU" if du != "—" else ""
        resp = t.get('respondents', [])
        lines = [
            f"<b>{name}</b>{du_str}",
            f"  📅 {t.get('published','—')} → {t.get('close_date','—')}",
            f"  🏆 {t.get('awarded_to','—')}",
        ]
        if t.get('awarded_value'):
            lines.append(f"  💰 {fmtm(t['awarded_value'])} · {t.get('awarded_date','—')}")
        if resp:
            lines.append(f"  👥 {len(resp)} shortlisted bids")
            for i, r in enumerate(resp[:3]):
                medal = ["🥇","🥈","🥉"][i]
                price = fmtm(r['opt_a']) if r.get('opt_a') else "—"
                lines.append(f"  {medal} {price}  {r['name'][:26]}")
        if t.get('url'):
            lines.append(f"  🔗 <a href=\"{t['url']}\">GeBIZ</a>")
        tg_send("\n".join(lines), target_id=sender)

# ── Refresh data from GitHub raw URL ──────────────────────────────────────────
def refresh_data_from_github():
    """Download latest gebiz_data.json from the GitHub repo. Returns True on success."""
    if not GITHUB_RAW:
        log.warning("GITHUB_RAW not set — using local cached gebiz_data.json")
        return False
    url = GITHUB_RAW + "gebiz_data.json"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            content = r.read()
        with open(DATA_FILE, "wb") as f:
            f.write(content)
        log.info("Refreshed data from %s", url)
        return True
    except Exception as e:
        log.warning("Failed to refresh data: %s", e)
        return False

# ── Push subscribers to GitHub via git ────────────────────────────────────────
def push_subscribers_to_github():
    """Commit and push tg_subscribers.json to GitHub. Silent if no changes."""
    global _subscribers_dirty
    if not _subscribers_dirty:
        return
    try:
        # Stage only the subscribers file
        subprocess.run(["git", "add", SUBSCRIBERS_FILE],
                       check=True, capture_output=True)
        # Check if there's anything to commit
        result = subprocess.run(["git", "diff", "--cached", "--quiet"],
                                capture_output=True)
        if result.returncode == 0:
            # Nothing to commit
            _subscribers_dirty = False
            return
        subprocess.run(["git", "commit", "-m", "Update subscribers"],
                       check=True, capture_output=True)
        subprocess.run(["git", "push"], check=True, capture_output=True)
        log.info("Pushed subscriber update to GitHub")
        _subscribers_dirty = False
    except subprocess.CalledProcessError as e:
        log.warning("Git push failed: %s", e.stderr.decode() if e.stderr else e)
    except Exception as e:
        log.warning("Git push error: %s", e)

# ── Telegram command polling loop ─────────────────────────────────────────────
def poll_commands():
    global _tg_last_update_id, _subscribers, _subscribers_dirty
    if not tg_enabled():
        log.error("TG_TOKEN not set. Exiting.")
        sys.exit(1)

    log.info("Bot started. Polling Telegram every %ds.", POLL_INTERVAL)
    log.info("Loaded %d subscribers.", len(_subscribers))
    last_data_refresh = 0
    last_git_push = time.time()

    while True:
        # Periodic refresh of data from GitHub
        if time.time() - last_data_refresh > DATA_REFRESH_SEC:
            refresh_data_from_github()
            last_data_refresh = time.time()

        # Periodic push of subscriber changes
        if time.time() - last_git_push > GIT_PUSH_SEC:
            push_subscribers_to_github()
            last_git_push = time.time()

        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?offset={_tg_last_update_id+1}&timeout=20"
            with urllib.request.urlopen(url, timeout=30) as r:
                updates = json.loads(r.read())
            for u in updates.get("result", []):
                _tg_last_update_id = u["update_id"]
                msg = u.get("message") or u.get("edited_message", {})
                if not msg:
                    continue
                text   = (msg.get("text") or "").strip().lower().split()[0] if msg.get("text") else ""
                sender = str(msg.get("chat", {}).get("id", ""))
                name   = msg.get("chat", {}).get("first_name", "there")
                if not text.startswith("/") or not sender:
                    continue
                log.info("[%s] %s", sender, text)
                data = load_data()

                if text in ("/start", "/help"):
                    if sender not in _subscribers:
                        _subscribers.add(sender)
                        save_subscribers(_subscribers)
                        _subscribers_dirty = True
                        log.info("New subscriber: %s (%s)", name, sender)
                        welcome = f"\U0001f44b Hi {name}! You're now subscribed to GeBIZ alerts.\n\n"
                    else:
                        welcome = f"\U0001f44b Hi {name}!\n\n"
                    _tg_send_one(sender,
                        welcome +
                        "\U0001f916 <b>GeBIZ Monitor Bot</b>\n\n"
                        "Commands:\n"
                        "/summary - overview\n"
                        "/open    - open tenders\n"
                        "/closed  - closed tenders with bids\n"
                        "/awarded - awarded tenders\n"
                        "/stop    - unsubscribe\n"
                        "/help    - show this message"
                    )
                elif text == "/stop":
                    if sender in _subscribers:
                        _subscribers.discard(sender)
                        save_subscribers(_subscribers)
                        _subscribers_dirty = True
                    _tg_send_one(sender, "\U0001f515 Unsubscribed. Send /start anytime to re-subscribe.")
                elif text == "/summary":
                    _tg_send_one(sender, tg_summary(data))
                elif text == "/open":
                    _reply_open(data.get("open", []), sender)
                elif text == "/closed":
                    _reply_closed([t for t in data.get("closed", [])
                                   if not t.get("two_envelope") or t.get("under_evaluation")], sender)
                elif text == "/awarded":
                    _reply_awarded([t for t in data.get("closed", [])
                                    if t.get("two_envelope") and not t.get("under_evaluation")], sender)
                else:
                    _tg_send_one(sender, "Unknown command. Send /help.")
        except Exception as e:
            log.warning("Poll error: %s", e)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("GeBIZ Bot starting")
    log.info("Telegram enabled: %s", tg_enabled())
    log.info("GitHub raw URL: %s", GITHUB_RAW or "(not set)")
    log.info("=" * 60)
    # Initial data fetch from GitHub
    refresh_data_from_github()
    try:
        poll_commands()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        push_subscribers_to_github()
        log.info("Bye.")
