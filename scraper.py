
"""
scraper.py — GeBIZ Tender Monitor (single-run version for GitHub Actions)

Runs ONE scrape cycle, updates gebiz_data.json, sends Telegram notifications,
then exits. Designed to be triggered by GitHub Actions cron.

Environment variables:
  TG_TOKEN      — Telegram bot token (required for notifications)
  TG_CHAT_ID    — Owner's chat ID (fallback subscriber if file missing)
"""

import sys, io, os, time, json, logging, re, urllib.request, urllib.parse
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ── Config (same as original) ─────────────────────────────────────────────────
SEARCH_TERM     = "dwelling unit"
TITLE_MUST_HAVE = "building works"
DATA_FILE       = "gebiz_data.json"
NOTIFIED_FILE   = "tg_notified.json"
LOG_FILE        = "gebiz_scraper.log"
GEBIZ_HOME      = "https://www.gebiz.gov.sg/"

DU_SEARCH_TERM = "DUs"
DU_TITLE_RE    = re.compile(r'TOTAL\s*:?\s*[\d,]+\s*DUs?\b', re.IGNORECASE)

LPS_SEARCH_TERM      = "LPS"
LPS_TITLE_MUST_HAVE  = ("large panel slab", "hdb")

# ── Telegram config (from environment, NOT hardcoded) ────────────────────────
TG_TOKEN   = os.environ.get("TG_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

SUBSCRIBERS_FILE = "tg_subscribers.json"

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
    return {"open": [], "closed": [], "last_updated": None, "run_count": 0}

def save_data(data):
    data["last_updated"] = datetime.now().strftime("%d %b %Y, %H:%M:%S")
    # Write to a temp file first, then rename — atomic on Windows too
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)  # atomic rename
    log.info("Saved — open=%d closed=%d", len(data["open"]), len(data["closed"]))

def load_notified():
    """Load set of doc_nos that have already been Telegram-alerted."""
    if Path(NOTIFIED_FILE).exists():
        with open(NOTIFIED_FILE, encoding='utf-8') as f:
            return set(json.load(f))
    return set()

def save_notified(notified):
    with open(NOTIFIED_FILE, "w", encoding='utf-8') as f:
        json.dump(list(notified), f)

def clean(s):
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'&amp;', '&', s)
    s = re.sub(r'&nbsp;', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

def title_matches(title):
    """Returns True if title is a building works, valid DU short-form, or valid LPS tender."""
    t = title.lower()
    if TITLE_MUST_HAVE in t:
        return True
    # DU/DUs short form: must have "TOTAL <number> DU/DUs" pattern (not just any mention of DU)
    if DU_TITLE_RE.search(title):
        return True
    # LPS: must also contain "large panel slab" or "hdb"
    if 'lps' in t and any(kw in t for kw in LPS_TITLE_MUST_HAVE):
        return True
    return False

def is_lps(title):
    """True if this tender is an LPS type (not building works)."""
    t = title.lower()
    return 'lps' in t and any(kw in t for kw in LPS_TITLE_MUST_HAVE)

# ── Scraper ───────────────────────────────────────────────────────────────────

class GeBIZScraper:
    def __init__(self):
        self.pw = self.browser = self.page = None

    def start(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=True)
        self.page = self.browser.new_page()
        self.page.set_default_timeout(30000)
        log.info("Browser started")

    def stop(self):
        try: self.browser.close(); self.pw.stop()
        except: pass

    def search(self):
        log.info("Searching: '%s'", SEARCH_TERM)
        self.page.goto(GEBIZ_HOME, wait_until="networkidle")
        time.sleep(2)
        self.page.wait_for_selector('input[id$="searchBar_INPUT-SEARCH"]', timeout=10000)
        self.page.fill('input[id$="searchBar_INPUT-SEARCH"]', SEARCH_TERM)
        time.sleep(0.3)
        self.page.click('input[value="Go"]')
        self.page.wait_for_load_state("networkidle")
        time.sleep(2)
        html = self.page.content()
        open_m   = re.search(r'value="Open\s*\((\d+)\)"', html)
        closed_m = re.search(r'value="Closed\s*\((\d+)\)"', html)
        log.info("Results — Open:%s Closed:%s",
                 open_m.group(1) if open_m else "?",
                 closed_m.group(1) if closed_m else "?")

    def search_lps(self):
        """Run a separate search for LPS tenders."""
        log.info("Searching LPS: '%s'", LPS_SEARCH_TERM)
        self.page.goto(GEBIZ_HOME, wait_until="networkidle")
        time.sleep(2)
        self.page.wait_for_selector('input[id$="searchBar_INPUT-SEARCH"]', timeout=10000)
        self.page.fill('input[id$="searchBar_INPUT-SEARCH"]', LPS_SEARCH_TERM)
        time.sleep(0.3)
        self.page.click('input[value="Go"]')
        self.page.wait_for_load_state("networkidle")
        time.sleep(2)
        html = self.page.content()
        open_m   = re.search(r'value="Open\s*\((\d+)\)"', html)
        closed_m = re.search(r'value="Closed\s*\((\d+)\)"', html)
        log.info("LPS Results — Open:%s Closed:%s",
                 open_m.group(1) if open_m else "?",
                 closed_m.group(1) if closed_m else "?")

    def search_du(self):
        """Run a separate search for DUs short-form tenders."""
        log.info("Searching DU short-form: '%s'", DU_SEARCH_TERM)
        self.page.goto(GEBIZ_HOME, wait_until="networkidle")
        time.sleep(2)
        self.page.wait_for_selector('input[id$="searchBar_INPUT-SEARCH"]', timeout=10000)
        self.page.fill('input[id$="searchBar_INPUT-SEARCH"]', DU_SEARCH_TERM)
        time.sleep(0.3)
        self.page.click('input[value="Go"]')
        self.page.wait_for_load_state("networkidle")
        time.sleep(2)
        html = self.page.content()
        open_m   = re.search(r'value="Open\s*\((\d+)\)"', html)
        closed_m = re.search(r'value="Closed\s*\((\d+)\)"', html)
        log.info("DU Results — Open:%s Closed:%s",
                 open_m.group(1) if open_m else "?",
                 closed_m.group(1) if closed_m else "?")

    def scrape_open(self):
        log.info("Scraping Open tab...")
        html = self.page.content()
        btn = re.search(r'<input[^>]*value="Open\s*\(\d+\)"[^>]*>', html)
        if btn and 'disabled' not in btn.group(0):
            try:
                self.page.click('input[value^="Open ("]')
                self.page.wait_for_load_state("networkidle")
                time.sleep(2)
            except: pass
        return self._scrape_all_pages("Open")

    def scrape_closed(self):
        log.info("Scraping Closed tab...")
        try:
            self.page.click('input[value^="Closed ("]')
            self.page.wait_for_load_state("networkidle")
            time.sleep(2)
        except Exception as e:
            log.warning("Could not click Closed tab: %s", e)
            return {}
        return self._scrape_all_pages("Closed")

    def _scrape_all_pages(self, status):
        all_results = {}
        page_n = 1
        while True:
            html = self.page.content()
            items = self._parse_page(html, status)
            all_results.update(items)
            log.info("  %s page %d: %d matched (total %d)", status, page_n, len(items), len(all_results))
            next_ids = [i for i in re.findall(r'id="(contentForm:[^"]*_Next_[^"]*)"', html)
                        if 'loadingScreen' not in i]
            clicked = False
            for nid in next_ids:
                try:
                    btn = self.page.locator(f'input[id="{nid}"]')
                    if btn.is_visible() and not btn.is_disabled():
                        btn.click()
                        self.page.wait_for_load_state("networkidle")
                        time.sleep(1.5)
                        page_n += 1
                        clicked = True
                        break
                except: pass
            if not clicked or page_n > 50:
                break
        return all_results

    def _parse_page(self, html, status):
        results = {}
        for m in re.finditer(
            r'href="(/ptn/opportunity/directlink\.xhtml\?docCode=([^"]+))"[^>]*'
            r'class="commandLink_TITLE-BLUE">(.*?)</a>',
            html, re.DOTALL
        ):
            href, doc_code, title_html = m.group(1), m.group(2), m.group(3)
            title = clean(title_html)
            if len(title) < 5 or not title_matches(title):
                continue
            doc_idx  = html.find('docCode=' + doc_code)
            block    = clean(html[max(0, doc_idx-200): doc_idx+2500])
            agency_m = re.search(r'Agency\s+(.+?)\s+(?:Published|Procurement)', block)
            tender_type = "lps" if is_lps(title) else "building_works"
            results[doc_code] = {
                "title":        title,
                "doc_no":       doc_code,
                "close_date":   "",
                "published":    "",
                "status":       status,
                "agency":       agency_m.group(1).strip() if agency_m else "",
                "url":          "https://www.gebiz.gov.sg" + href,
                "tender_type":  tender_type,
            }
            log.info("  [%s][%s] %s", status, tender_type.upper(), title[:65])
        return results

    # ── Verify page title matches expected tender ──────────────────────────────
    def _verify_title(self, text, tender):
        """
        After navigating to a detail page, confirm it's the right tender.
        GeBIZ can return a companion/related tender with wrong docCode from listing.
        Returns True if page title matches our filter, False if mismatch.
        Also works for LPS tenders which may not start with "PART A".
        """
        page_title_m = re.search(r'PART\s+A\s*:\s*([A-Z\s\(\),;:\.\-\d]+?)(?:PART\s+B|layout_)', text)
        if page_title_m:
            page_title = page_title_m.group(0).strip()
            if not title_matches(page_title):
                log.warning("  Title mismatch for %s — page shows: %s",
                            tender["doc_no"], page_title[:80])
                return False
        else:
            # LPS tenders may not have "PART A" — just check the raw title_matches
            if not title_matches(tender.get("title", "")):
                return False
        return True

    # ── Parse dates from any detail page ──────────────────────────────────────
    def _parse_dates(self, text, tender):
        """
        From debug: Published = "28 Nov 2025 03:05PM"
                    Closed    = "Closed 03 Feb 2026 04:00PM (updated)"
                    Open      = "Closing on 09 Mar 2026 04:00PM (updated)"
        """
        # Published date
        pub_m = re.search(
            r'Published\s+(\d{1,2}\s+\w+\s+\d{4}\s+\d{1,2}:\d{2}\s*(?:AM|PM)?)',
            text, re.IGNORECASE
        )
        if pub_m:
            tender["published"] = pub_m.group(1).strip()

        # Closing/Closed date — can be "Closed DD Mon YYYY HH:MMam" or "Closing on DD Mon..."
        close_m = re.search(
            r'(?:Closed|Closing on)\s+(\d{1,2}\s+\w+\s+\d{4})\s+(\d{1,2}:\d{2}\s*(?:AM|PM)?)',
            text, re.IGNORECASE
        )
        if close_m:
            tender["close_date"] = close_m.group(1).strip() + " " + close_m.group(2).strip()

        return tender

    # ── Open tender: visit detail for Published + Closing ─────────────────────
    def visit_detail(self, tender):
        url = tender.get("url", "")
        if not url:
            return tender
        try:
            self.page.goto(url, wait_until="networkidle")
            time.sleep(2)
            text = clean(self.page.content())
            if not self._verify_title(text, tender):
                log.warning("  Skipping detail for %s — page title mismatch", tender["doc_no"])
                return tender
            tender = self._parse_dates(text, tender)
            log.info("  %s: published=%s closing=%s",
                     tender["doc_no"], tender.get("published","?"), tender.get("close_date","?"))
        except Exception as e:
            log.warning("  Detail error %s: %s", tender.get("doc_no","?"), e)
        return tender

    # ── Closed tender: get respondents by removing overlay then clicking ────────
    def get_respondents(self, tender):
        """
        Navigate to tender, remove the loadingScreen overlay that blocks headless
        clicks, then do a real Playwright click on the Respondents tab button.
        form.submit() was unreliable — it submitted without proper JSF session
        state and GeBIZ returned a random cached page.
        """
        url = tender.get("url", "")
        if not url:
            return []
        try:
            self.page.goto(url, wait_until="networkidle")
            time.sleep(2)

            # Verify this is actually a building works tender
            text = clean(self.page.content())
            if not self._verify_title(text, tender):
                log.warning("  Skipping %s — not a building works tender", tender["doc_no"])
                return []

            # Grab dates from overview
            tender = self._parse_dates(text, tender)

            # Find the Respondents button
            html = self.page.content()
            resp_val_m = re.search(r'value="(Respondents\s*\(\d+\))"', html)
            if not resp_val_m:
                log.warning("  No Respondents tab found for %s", tender["doc_no"])
                return []
            resp_val = resp_val_m.group(1)
            log.info("  Clicking tab: '%s'", resp_val)

            # Remove ALL loadingScreen overlay elements — these intercept clicks
            # in headless mode, causing the click to be swallowed by the overlay
            self.page.evaluate("""() => {
                document.querySelectorAll('[class*="loadingScreen"]').forEach(el => el.remove());
                document.querySelectorAll('[id*="loadingScreen"]').forEach(el => el.remove());
            }""")

            # Now do a real click — the overlay is gone so it hits the button directly
            btn = self.page.locator(f'input[value="{resp_val}"]').first
            btn.click()
            self.page.wait_for_load_state("networkidle")
            time.sleep(3)

            html2 = self.page.content()
            text2 = clean(html2)

            # Verify active tab switched to Respondents
            active_m = re.search(r'value="([^"]*)"[^>]*BUTTON-ACTIVE', html2)
            active_tab = active_m.group(1) if active_m else "unknown"
            log.info("  Active tab: %s", active_tab)

            if 'suppliers responded' not in html2:
                log.warning("  Respondents content not loaded for %s", tender["doc_no"])
                return []

            # CRITICAL: verify the respondents page is still for the correct tender.
            # Clicking the tab can navigate to a wrong cached tender in headless mode.
            # Check the page title contains the expected project identifier.
            expected_proj = re.search(r'PROJECT\s+([A-Z]+)', tender.get('title',''), re.IGNORECASE)
            if expected_proj:
                proj_code = expected_proj.group(1)
                if proj_code not in text2:
                    log.warning("  Post-click page mismatch for %s — expected PROJECT %s not found",
                                tender["doc_no"], proj_code)
                    return []
                log.info("  Project %s confirmed in respondents page", proj_code)

            respondents = self._parse_respondents(html2, text2, tender["doc_no"])
            log.info("  Respondents parsed: %d", len(respondents))

            # For two-envelope tenders, tag and scrape award tab only if actually awarded.
            # Reliable rule: Award tab button ONLY appears after the tender is awarded.
            # "under evaluation" AND "pending award" both have NO Award tab — treat same.
            if 'two envelope tender' in html2.lower():
                tender["two_envelope"] = True
                has_award_tab = bool(re.search(r'value="Award\s*\(\d+\)"', html2))
                if has_award_tab:
                    # Fully awarded — scrape winner + value from Award tab
                    tender = self._parse_award(html2, text2, tender)
                    tender.pop("under_evaluation", None)
                    log.info("  Two-envelope: Award tab found — scraping award details")
                else:
                    # Still pending (under evaluation or pending award) — names only
                    tender["under_evaluation"] = True
                    log.info("  Two-envelope: no Award tab — pending (evaluation/pending award)")

            return respondents

        except Exception as e:
            log.warning("  Respondent error %s: %s", tender.get("doc_no","?"), e)
            return []
    def _parse_respondents(self, html, text, doc_no):
        """
        Handles two GeBIZ tender structures:

        1. STANDARD tender: all respondents shown with full Option A / Option B prices.
           Each accordion block: "NNN,NNN,NNN.00 (SGD) COMPANY NAME ... OPTION A - ... OPTION B - ..."

        2. TWO-ENVELOPE AWARDED tender:
           - Shows notice: "This is a two envelope tender..."
           - "SHORTLISTED SUPPLIER(S)" section with prices (accordion, same format)
           - Non-shortlisted suppliers listed but no prices shown
           - Award tab: winner + awarded value
           For this type we store:
             awarded_to, awarded_value, awarded_date  on the tender
             respondents = shortlisted suppliers with their prices (like standard)
        """
        respondents = []

        # ── Detect two-envelope awarded tender ────────────────────────────────
        is_two_envelope  = 'two envelope tender' in html.lower()
        under_evaluation = is_two_envelope and 'under evaluation' in html.lower()
        if is_two_envelope:
            log.info("  Two-envelope tender detected for %s (under_eval=%s)", doc_no, under_evaluation)

        # ── Find respondent section ────────────────────────────────────────────
        idx = text.find('suppliers responded')
        if idx < 0:
            log.warning("  \'suppliers responded\' not found for %s", doc_no)
            return []
        section = text[idx:]

        # For two-envelope: restrict to SHORTLISTED section only
        if is_two_envelope:
            sl_idx = section.find('SHORTLISTED SUPPLIER')
            if sl_idx >= 0:
                section = section[sl_idx:]
                log.info("  Using shortlisted section only")

        # ── Split into per-respondent blocks ───────────────────────────────────
        # (?<![,\d]) prevents splitting inside a larger number:
        #   "1,037,952,000" would otherwise also split at "037,952,000"
        #   because "," is not a digit and (?<!\d) alone doesn't catch it.
        # Negative lookahead excludes keywords that follow an amount inside a block.
        blocks = re.split(
            r'(?<![,\d])(?=\d{1,3}(?:,\d{3}){2,}\.\d{2}\s*\(SGD\)\s+'
            r'(?!Option|OPTION|Item|Unit|BASE|LOT|Mandatory|Required|Provision|PROVISION|ALTERNATE|See)[A-Z])',
            section
        )

        # ── Also try to extract names-only when prices not yet published ────────
        # GeBIZ sometimes shows company names in accordion before prices are released.
        # Pattern: company name appears after "inputText_DisableEnterKey" markers.
        # We use this as fallback when block splitting finds nothing.
        names_only_pattern = re.compile(
            r'(?:inputText_DisableEnterKey\([^)]+\)\s*){2,}'  # two consecutive inputText markers
            r'.*?'
            r'No\.\s+of\s+Companies.*?'
            r'(?:Consortium\s+(?:Yes|No)\s+\d+\s+)?'
            r'(?!Item|Mandatory|Option)',
            re.DOTALL
        )

        for block in blocks[1:]:  # skip preamble before first respondent
            header_m = re.match(
                r'([\d,]+\.[\d]{2})\s*\(SGD\)\s+([A-Z][^\n]+?)(?:\s+inputText|\s+No\.\s+of)',
                block
            )
            if not header_m:
                continue

            company   = header_m.group(2).strip()
            total_amt = float(header_m.group(1).replace(',', ''))

            if len(company) < 5:
                continue
            if company.startswith(('PART A', 'PART B', 'Document', 'CONTINGENCY', 'Option', 'Item')):
                continue

            # Option A unit price (4-decimal SGD)
            opt_a_m = re.search(
                r'OPTION\s+A\s*[-:\u2013].*?(\d{1,3}(?:,\d{3})*\.\d{4})\s*\(SGD\)',
                block, re.DOTALL | re.IGNORECASE
            )
            opt_a = float(opt_a_m.group(1).replace(',', '')) if opt_a_m else None

            # Option B unit price
            opt_b_m = re.search(
                r'OPTION\s+B\s*[-:\u2013].*?(\d{1,3}(?:,\d{3})*\.\d{4})\s*\(SGD\)',
                block, re.DOTALL | re.IGNORECASE
            )
            opt_b = float(opt_b_m.group(1).replace(',', '')) if opt_b_m else None

            # For two-envelope: no Option A/B labels — use the header total directly
            if opt_a is None:
                opt_a = total_amt if total_amt > 0 else None
            # opt_a stays None if total is 0 (prices not yet published)

            respondents.append({
                "name":        company,
                "opt_a":       opt_a,
                "opt_b":       opt_b,
                "price_pending": opt_a is None,
                "shortlisted": True if is_two_envelope else None,
            })

        # ── For two-envelope under evaluation: names shown but NO amounts ──────
        # GeBIZ message: "prices would only be shown after the award"
        # Structure: COMPANY_NAME inputText... inputText... No. of Companies N layout_RepaintAllLayouts(); NEXT_COMPANY...
        # Split on layout_RepaintAllLayouts() calls — each chunk starts with the next company name.
        if under_evaluation and not respondents:
            log.info("  Under evaluation — extracting names via layout splitter")
            chunks = re.split(r'layout_RepaintAllLayouts\(\);\s*', section)
            for chunk in chunks:
                chunk = chunk.strip()
                # First data chunk may start with "Collapse All Expand All COMPANY..."
                chunk = re.sub(r'^(?:Collapse All\s+|Expand All\s+)+', '', chunk)
                # Company name = everything before first inputText_DisableEnterKey
                m = re.match(
                    r'^([A-Z][A-Z0-9\s&\(\)\.\-,\'/]+?)\s+inputText_DisableEnterKey',
                    chunk
                )
                if not m:
                    continue
                name = m.group(1).strip()
                if len(name) < 5:
                    continue
                if name.startswith(('No.', 'Consortium', 'Item', 'Mandatory', 'Expand', 'Collapse')):
                    continue
                respondents.append({
                    "name":          name,
                    "opt_a":         None,
                    "opt_b":         None,
                    "price_pending": True,
                    "shortlisted":   None,
                })
            log.info("  Under-evaluation names extracted: %d", len(respondents))

        # ── Fallback: extract names only if no blocks found (prices not released) ─
        if not respondents:
            # Try simpler pattern: find company names near inputText markers
            name_matches = re.findall(
                r'([\d,]+\.00)\s*\(SGD\)\s+([A-Z][A-Z\s&\(\)\.\-,\']+?)(?:\s+inputText)',
                section
            )
            for amt_str, company in name_matches:
                company = company.strip()
                if len(company) < 5:
                    continue
                if company.startswith(('PART A', 'PART B', 'Document', 'CONTINGENCY')):
                    continue
                amt = float(amt_str.replace(',', ''))
                respondents.append({
                    "name":          company,
                    "opt_a":         amt if amt > 0 else None,
                    "opt_b":         None,
                    "price_pending": amt == 0,
                    "shortlisted":   True if is_two_envelope else None,
                })
            if respondents:
                log.info("  Fallback name-only parse: %d companies (prices pending)", len(respondents))

        # Sort: priced entries by opt_a ascending, price_pending entries at the end
        respondents.sort(key=lambda r: (r.get("price_pending") or False, r["opt_a"] or float("inf")))
        log.info("  Parsed %d respondents for %s", len(respondents), doc_no)
        return respondents

    def _parse_award(self, html, text, tender):
        """
        For awarded two-envelope tenders: click Award tab and extract
        winner name, awarded value, awarded date.
        """
        try:
            # Find and click the Award tab
            award_btn_m = re.search(r'value="(Award\s*\(\d+\))"', html)
            if not award_btn_m:
                return tender
            award_val = award_btn_m.group(1)

            self.page.evaluate("""() => {
                document.querySelectorAll('[class*="loadingScreen"]').forEach(el => el.remove());
                document.querySelectorAll('[id*="loadingScreen"]').forEach(el => el.remove());
            }""")
            btn = self.page.locator(f'input[value="{award_val}"]').first
            btn.click()
            self.page.wait_for_load_state("networkidle")
            time.sleep(2)

            award_html = self.page.content()
            award_text = clean(award_html)

            # Awarded to (company name follows "Awarded to" section)
            awarded_to_m = re.search(r'Awarded to\s+([A-Z][A-Z0-9&\(\)\.\-,\s]+?)\ +(?:No[\s\.]|Address|Consortium\b|Awarded\b)', award_text)
            awarded_val_m = re.search(r'Total Awarded Value\s+([\d,]+\.\d{2})\s*\(SGD\)', award_text)
            awarded_date_m = re.search(r'Awarded Date\s+(\d{1,2}\s+\w+\s+\d{4})', award_text)

            if awarded_to_m:
                tender["awarded_to"] = awarded_to_m.group(1).strip()
            if awarded_val_m:
                tender["awarded_value"] = float(awarded_val_m.group(1).replace(',', ''))
            if awarded_date_m:
                tender["awarded_date"] = awarded_date_m.group(1).strip()

            log.info("  Award: %s — $%s",
                     tender.get("awarded_to","?"),
                     f'{tender.get("awarded_value",0):,.0f}')
        except Exception as e:
            log.warning("  Award tab error for %s: %s", tender.get("doc_no","?"), e)
        return tender



# ── Main check ────────────────────────────────────────────────────────────────

def run_check():
    log.info("=" * 55)
    log.info("Running GeBIZ check...")
    data = load_data()
    data["run_count"] = data.get("run_count", 0) + 1
    notified = load_notified()   # doc_nos already alerted — skip on restart

    seen_open   = {t["doc_no"] for t in data["open"]}
    seen_closed = {t["doc_no"] for t in data["closed"]}
    prev_total  = len(seen_open) + len(seen_closed)

    # On first run after restart: mark ALL existing data as already notified
    # so we never re-alert old tenders. Only truly NEW ones (not in seen_*) get alerted.
    existing_all = seen_open | seen_closed
    if existing_all - notified:
        log.info("Pre-marking %d existing tenders as notified (restart protection)",
                 len(existing_all - notified))
        notified |= existing_all
        save_notified(notified)

    scraper = GeBIZScraper()
    open_items = closed_items = {}

    try:
        scraper.start()
        # ── Search 1: Building works (dwelling unit) ──
        scraper.search()
        open_items   = scraper.scrape_open()
        closed_items = scraper.scrape_closed()
        # ── Search 2: DUs short-form ──
        scraper.search_du()
        du_open   = scraper.scrape_open()
        du_closed = scraper.scrape_closed()
        open_items.update(du_open)
        closed_items.update(du_closed)
        # ── Search 3: LPS ──
        scraper.search_lps()
        lps_open   = scraper.scrape_open()
        lps_closed = scraper.scrape_closed()
        open_items.update(lps_open)
        closed_items.update(lps_closed)
        log.info("Combined: %d open, %d closed (incl. DU + LPS)", len(open_items), len(closed_items))
    except Exception as e:
        log.error("Scrape error: %s", e)
        scraper.stop()
        save_data(data)
        return

    total_found = len(open_items) + len(closed_items)
    if total_found == 0 and prev_total > 0:
        log.warning("0 results — keeping existing data")
        save_data(data)
        scraper.stop()
        return

    # Process open tenders
    new_open   = 0
    new_closed = 0  # declared here so auto-close block can increment it
    for doc_no, t in open_items.items():
        if doc_no not in seen_open:
            log.info("NEW OPEN: %s", t["title"][:65])
            t = scraper.visit_detail(t)
            entry = {**t, "added": datetime.now().strftime("%d %b %Y, %H:%M")}
            data["open"].append(entry)
            seen_open.add(doc_no)
            new_open += 1
            if doc_no not in notified:
                tg_send(tg_fmt_open(entry))   # 🔔 alert
                notified.add(doc_no)
                save_notified(notified)
        else:
            # Refresh dates each run
            t = scraper.visit_detail(t)
            for e in data["open"]:
                if e["doc_no"] == doc_no:
                    if t["close_date"]: e["close_date"] = t["close_date"]
                    if t["published"]:  e["published"]  = t["published"]

    # Process closed tenders from GeBIZ search results
    for doc_no, t in closed_items.items():
        if doc_no not in seen_closed:
            # Brand new closed tender
            log.info("NEW CLOSED: %s", t["title"][:60])
            resp = scraper.get_respondents(t)
            entry = {
                **t,
                "respondents": resp,
                "added": datetime.now().strftime("%d %b %Y, %H:%M")
            }
            data["closed"].append(entry)
            seen_closed.add(doc_no)
            new_closed += 1
            if doc_no not in notified:
                if entry.get("two_envelope") and not entry.get("under_evaluation"):
                    tg_send(tg_fmt_awarded(entry))  # 🔔 awarded alert
                else:
                    tg_send(tg_fmt_closed(entry))   # 🔔 closed alert
                notified.add(doc_no)
                save_notified(notified)
        else:
            # Already known — re-fetch if:
            # 1. Respondents missing (handles failed previous runs)
            # 2. Two-envelope tender that doesn't yet have under_evaluation set correctly
            #    (old JSON may have two_envelope=True but no under_evaluation — re-check state)
            for e in data["closed"]:
                if e["doc_no"] != doc_no:
                    continue
                needs_refetch = (
                    not e.get("respondents") or
                    # Re-scrape two-envelope tenders that are still pending
                    # (state may have changed: under eval → pending award → awarded)
                    (e.get("two_envelope") and not e.get("awarded_to"))
                )
                if needs_refetch:
                    log.info("RE-FETCHING respondents/state: %s", t["title"][:55])

                    # Snapshot state BEFORE re-fetch to detect what changed
                    was_under_eval  = e.get("under_evaluation", False)
                    was_all_pending = all(r.get("price_pending") for r in e.get("respondents", [{}]))
                    prev_awarded_to = e.get("awarded_to")

                    resp = scraper.get_respondents(t)
                    e["respondents"] = resp if resp else e.get("respondents", [])

                    # Sync state flags set by get_respondents onto stored entry
                    for flag in ("two_envelope", "under_evaluation", "awarded_to", "awarded_value", "awarded_date"):
                        if flag in t:
                            e[flag] = t[flag]
                        elif flag == "under_evaluation" and flag not in t and e.get(flag):
                            del e[flag]  # clear stale under_evaluation if award is now present
                    if t.get("close_date") and not e.get("close_date"):
                        e["close_date"] = t["close_date"]
                    if t.get("published") and not e.get("published"):
                        e["published"] = t["published"]
                    log.info("  Re-fetched: %d respondents, under_eval=%s, awarded=%s",
                             len(e.get("respondents",[])), e.get("under_evaluation"), e.get("awarded_to"))

                    # ── Detect state changes and send appropriate notifications ──
                    now_has_prices  = any(not r.get("price_pending") for r in e.get("respondents", []))
                    now_awarded     = e.get("awarded_to") and not prev_awarded_to

                    notify_key_prices  = doc_no + "_prices"
                    notify_key_awarded = doc_no + "_awarded"

                    if now_awarded and notify_key_awarded not in notified:
                        # Tender just got awarded — send award notification
                        tg_send(tg_fmt_awarded(e))
                        notified.add(notify_key_awarded)
                        save_notified(notified)
                        log.info("  🔔 Award notification sent for %s", doc_no)

                    elif now_has_prices and (was_under_eval or was_all_pending) and notify_key_prices not in notified:
                        # Prices just published for shortlisted suppliers — send price update
                        tg_send(tg_fmt_prices_updated(e))
                        notified.add(notify_key_prices)
                        save_notified(notified)
                        log.info("  🔔 Price update notification sent for %s", doc_no)

        for e in data["open"]:
            if e["doc_no"] == doc_no:
                e["status"] = "Closed"
    # Remove any open entries that are now in closed (so they don't show in both tabs)
    closed_doc_nos = {t["doc_no"] for t in data["closed"]}
    data["open"] = [t for t in data["open"] if t["doc_no"] not in closed_doc_nos]

    save_data(data)
    scraper.stop()
    log.info("Done. +%d open +%d closed. Total: %d open %d closed.",
             new_open, new_closed, len(data["open"]), len(data["closed"]))


# ── Telegram command polling thread (runs every 15s, independent of scraper) ──

# ── Main entry: single run ────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("GeBIZ Scraper — single run (GitHub Actions mode)")
    log.info("Time: %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("Telegram enabled: %s", tg_enabled())
    log.info("Subscribers: %d", len(_subscribers))
    log.info("=" * 60)
    run_check()
    log.info("Done. Exiting.")
