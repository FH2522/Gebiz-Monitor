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
HDB_AGENCY_RE = re.compile(r"\b(?:housing\s*(?:and|&)\s*development\s*board|hdb)\b", re.I)
PROJECT_RE = re.compile(r"\bproj(?:ect)?\.?\s*[:#-]?\s*([A-Z]{1,5}(?:[-/]\w+)?)\b", re.I)
BUILDING_RE = re.compile(r"\b(?:building|bldg\.?)\s*(?:works?|wk[s]?)\b", re.I)
DU_COUNT_RE = re.compile(r"\bTOTAL\s*:?\s*([\d,]+)\s*(?:DU[S]?|DWELLING\s+UNITS?)\b", re.I)
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
    m = PROJECT_RE.search(title)
    if m:
        name = f"Project {m.group(1).upper()}"
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
    awarded_t = [t for t in closed_t if t.get('status') == 'Awarded' or t.get('awarded_to')]
    normal_t  = [t for t in closed_t if not (t.get('status') == 'Awarded' or t.get('awarded_to'))]

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
    """HDB construction matching: work type + project identifier + DU count."""
    return bool(BUILDING_RE.search(title) and PROJECT_RE.search(title) and DU_COUNT_RE.search(title))

def is_lps(title):
    """Optional LPS discovery retained, but HDB agency verification is mandatory."""
    t = title.lower()
    return 'lps' in t and any(kw in t for kw in LPS_TITLE_MUST_HAVE)

def matches_hdb(tender, detail_text=''):
    """Do not accept projects from non-HDB agencies or unspecified agencies."""
    agency = tender.get('agency', '')
    if not HDB_AGENCY_RE.search(agency):
        detail_m = re.search(r'\bAgency\s+(Housing\s+(?:and|&)\s+Development\s+Board)\b', detail_text, re.I)
        if detail_m:
            tender['agency'] = detail_m.group(1)
        else:
            return False
    return title_matches(tender.get('title', '')) or is_lps(tender.get('title',''))

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
            if len(title) < 5 or not (title_matches(title) or is_lps(title)):
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
            if not (title_matches(page_title) or is_lps(page_title)):
                log.warning("  Title mismatch for %s — page shows: %s",
                            tender["doc_no"], page_title[:80])
                return False
        else:
            # LPS tenders may not have "PART A" — just check the raw title_matches
            if not (title_matches(tender.get("title", "")) or is_lps(tender.get("title", ""))):
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
            tender["hdb_verified"] = matches_hdb(tender, text)
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
            tender["hdb_verified"] = matches_hdb(tender, text)
            if not tender["hdb_verified"]:
                log.warning("  Not verified HDB project: %s", tender["doc_no"])
                return []

            tender['requires_option_b'] = bool(re.search(r'OPTION\s+B\s*[-–]', text, re.I))

            # Find the Respondents button
            html = self.page.content()
            resp_val_m = re.search(r'value="(Respondents\s*\(\d+\))"', html)
            if not resp_val_m:
                log.warning("  No Respondents tab found for %s", tender["doc_no"])
                self._parse_award(html, text, tender)
                tender['status'] = 'Awarded' if tender.get('award_confirmed') else 'Closed'
                tender['under_evaluation'] = tender['status'] != 'Awarded'
                return []
            resp_val = resp_val_m.group(1)
            tender["respondents_expected"] = int(re.search(r"\((\d+)\)", resp_val).group(1))
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

            if not re.search(r'\bsuppliers?\s+responded\b', text2, re.I):
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

            # Expand respondent accordion entries so item-level details are present.
            try:
                expand = self.page.get_by_text('Expand All', exact=True)
                if expand.count() and expand.first.is_visible():
                    expand.first.click(timeout=6000)
                    self.page.wait_for_timeout(1200)
                    html2 = self.page.content()
                    text2 = clean(html2)
            except Exception as exc:
                log.info('  Expand All not available: %s', exc)

            respondents = self._parse_respondents(html2, text2, tender["doc_no"])
            tender['respondents_complete'] = (len(respondents) == tender['respondents_expected'])
            if not tender['respondents_complete']:
                log.warning('  INCOMPLETE %s: scraped %d / expected %d (or prices missing)',
                            tender['doc_no'], len(respondents), tender['respondents_expected'])
            log.info("  Respondents parsed: %d", len(respondents))

            tender['two_envelope'] = bool(re.search(r'Two Envelope Bidding\s+Yes\b',text,re.I))
            # Only an actual awarded record can justify Awarded classification.
            award_button = re.search(r'value="(Award\s*\(\d+\))"',html2,re.I)
            if award_button:
                tender = self._parse_award(html2,text2,tender)
            tender['status'] = 'Awarded' if tender.get('award_confirmed') or tender.get('awarded_to') else 'Closed'
            tender['under_evaluation'] = tender['status'] != 'Awarded'
            return respondents

        except Exception as e:
            log.warning("  Respondent error %s: %s", tender.get("doc_no","?"), e)
            return []
    def _parse_respondents(self, html, text, doc_no):
        """Read shortlisted and non-shortlisted suppliers without treating absent
        prices as absent respondents. Company-level total and option-level bids
        are deliberately separate fields.
        """
        from bs4 import BeautifulSoup
        from html import unescape
        soup = BeautifulSoup(html, 'html.parser')
        chunks = [re.sub(r'\s+', ' ', unescape(t)).strip() for t in soup.stripped_strings]
        chunks = [c for c in chunks if c]
        start = next((i for i,c in enumerate(chunks) if re.search(r'\b\d+\s+suppliers?\s+responded\b', c, re.I)), None)
        if start is None:
            log.warning('Respondents view absent for %s', doc_no)
            return []
        chunks = chunks[start+1:]
        firm_end = re.compile(r'(?:PTE\.?\s*LTD\.?|PRIVATE LIMITED|LIMITED|LTD\.?|LLP|INC\.?)\s*$', re.I)
        amount = re.compile(r'^([\d,]+\.\d{2})\s*(?:\(SGD\))?$', re.I)
        embedded = re.compile(r'^(.*?)\s+([\d,]+\.\d{2})\s*\(SGD\)$', re.I)
        headings = []
        shortlisted = None
        for i,c in enumerate(chunks):
            if re.match(r'^NOT\s+SHORTLISTED\s+SUPPLIER', c, re.I):
                shortlisted = False
                continue
            if re.match(r'^SHORTLISTED\s+SUPPLIER', c, re.I):
                shortlisted = True
                continue
            if shortlisted is None:
                continue
            match = embedded.match(c)
            if match and firm_end.search(match.group(1)):
                headings.append((i,i+1,match.group(1).strip(),float(match.group(2).replace(',','')),shortlisted))
            elif firm_end.search(c):
                # Only a company heading is followed by a total, an accordion
                # control, or a new section. Do not use names found inside bids.
                following = chunks[i+1] if i+1 < len(chunks) else ''
                m = amount.match(following)
                if m:
                    headings.append((i,i+2,c,float(m.group(1).replace(',','')),shortlisted))
                elif not shortlisted and (not following or re.match(r'^(?:Expand|Collapse|No\. of Companies|Consortium|NOT\s+SHORTLISTED|SHORTLISTED)',following,re.I) or firm_end.search(following)):
                    headings.append((i,i+1,c,None,False))
        # If a not-shortlisted company is the last row, it may have no following
        # text; the explicit NOT SHORTLISTED section remains authoritative.
        result=[]; seen=set()
        for idx,(begin,end,name,total,is_shortlisted) in enumerate(headings):
            key=re.sub(r'\W+','',name).upper()
            if key in seen: continue
            seen.add(key)
            next_begin=headings[idx+1][0] if idx+1 < len(headings) else len(chunks)
            section=' '.join(chunks[end:next_begin])
            items=list(re.finditer(r'\bItem\s+No\.?\s*(\d+)\b',section,re.I))
            prices={}
            for j,it in enumerate(items):
                block=section[it.end():items[j+1].start() if j+1<len(items) else len(section)]
                option=re.search(r'\bOPTION\s+([AB])\b',block,re.I)
                # Some 2-envelope projects use OPTION 2A / 2B.
                if not option:
                    option=re.search(r'\bOPTION\s+\d+\s*([AB])\b',block,re.I)
                base=re.search(r'\bBASE\s+BID\b',block,re.I)
                if not option or not base: continue
                amounts=re.findall(r'([\d,]+\.\d{2,4})\s*\(SGD\)',block[base.end():],re.I)
                if amounts:
                    prices[option.group(1).lower()]=float(amounts[-1].replace(',',''))
            result.append({'name':name,'shortlisted':is_shortlisted,
                'opt_a':prices.get('a'), 'opt_b':prices.get('b'),
                'total_price':total, 'price_pending':not bool(prices)})
        result.sort(key=lambda r: (not r['shortlisted'], r['total_price'] if r['total_price'] is not None else float('inf')))
        log.info('Respondents parsed for %s: %d (%d shortlisted / %d not shortlisted)',
                 doc_no,len(result),sum(r['shortlisted'] for r in result),sum(not r['shortlisted'] for r in result))
        return result

    def _parse_award(self, html, text, tender):
        """Open the official Award tab and capture winner + each awarded item.
        The Award tab's own fields are the source of truth, never bid rankings.
        """
        try:
            m = re.search(r'value="(Award\s*\((\d+)\))"',html,re.I)
            if not m or int(m.group(2)) < 1: return tender
            self.page.evaluate("""() => {
                document.querySelectorAll('[class*="loadingScreen"], [id*="loadingScreen"]').forEach(el => el.remove());
            }""")
            self.page.locator(f'input[value="{m.group(1)}"]').first.click()
            self.page.wait_for_load_state('networkidle')
            time.sleep(2)
            from bs4 import BeautifulSoup
            soup=BeautifulSoup(self.page.content(),'html.parser')
            nodes=[re.sub(r'\s+',' ',v).strip() for v in soup.stripped_strings]
            nodes=[v for v in nodes if v]
            award_text=' '.join(nodes)
            # Verify we remain on the same tender, if reference is visible.
            ref=tender.get('reference_no') or tender.get('ref_no')
            if ref and re.search(r'PCG-TN\d+',award_text,re.I) and ref not in award_text:
                log.warning('Award tab tender mismatch: %s',tender.get('doc_no'))
                return tender
            value_m=re.search(r'\bTotal Awarded Value\s*:?(?:\s+S\$)?\s*([\d,]+\.\d{2})',award_text,re.I)
            date_m=re.search(r'\bAwarded Date\s*:?\s*(\d{1,2}\s+\w+\s+\d{4})',award_text,re.I)
            winner=None
            for i,node in enumerate(nodes):
                if re.fullmatch(r'Awarded to\s*:?',node,re.I) and i+1 < len(nodes):
                    candidate=nodes[i+1].strip()
                    if re.search(r'\b(?:PTE\.?\s*LTD\.?|LIMITED|LLP)\b',candidate,re.I):
                        winner=candidate;break
                wm=re.match(r'^Awarded to\s*:?\s+(.+)$',node,re.I)
                if wm and re.search(r'\b(?:PTE\.?\s*LTD\.?|LIMITED|LLP)\b',wm.group(1),re.I):
                    winner=wm.group(1).strip();break
            # Item prices are scoped by Item No. (a combined tender can have
            # Project DD and Project DO under one award).
            items=[]
            for im in re.finditer(r'\bItem\s+No\.?\s*(\d+)\b',award_text,re.I):
                following=award_text[im.end():]
                next_item=re.search(r'\bItem\s+No\.?\s*\d+\b',following,re.I)
                block=following[:next_item.start()] if next_item else following
                opt=re.search(r'\bOPTION\s+([12]?[AB])\b',block,re.I)
                project=re.search(r'\bPROJECT\s+([A-Z]{1,5})\b',block,re.I)
                v=re.search(r'\bAwarded Value\s*:?\s*([\d,]+\.\d{2})',block,re.I)
                if v:
                    items.append({'item_no':int(im.group(1)),
                        'option':opt.group(1).upper() if opt else None,
                        'project':project.group(1).upper() if project else None,
                        'awarded_value':float(v.group(1).replace(',',''))})
            if value_m: tender['awarded_value']=float(value_m.group(1).replace(',',''))
            if date_m: tender['awarded_date']=date_m.group(1)
            if winner: tender['awarded_to']=winner
            if items: tender['awarded_items']=items
            tender['award_confirmed']=bool(value_m or date_m or winner or items)
            log.info('  Award fields %s: confirmed=%s winner=%s value=%s items=%s',
                tender.get('doc_no'),tender['award_confirmed'],tender.get('awarded_to'),
                tender.get('awarded_value'),items)
        except Exception as exc:
            log.warning('  Award tab error for %s: %s',tender.get('doc_no'),exc)
        return tender

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
            if not t.get("hdb_verified"):
                continue
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
            if not t.get("hdb_verified"):
                continue
            entry = {
                **t,
                "respondents": resp,
                "added": datetime.now().strftime("%d %b %Y, %H:%M")
            }
            data["closed"].append(entry)
            seen_closed.add(doc_no)
            new_closed += 1
            if doc_no not in notified:
                if entry.get("status") == "Awarded" or entry.get("awarded_to"):
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
                    not e.get('respondents_complete', False) or
                    False or
                    # Re-scrape two-envelope tenders that are still pending
                    # (state may have changed: under eval → pending award → awarded)
                    (e.get('status') != 'Awarded') or
                    not e.get('awarded_to') or
                    not e.get('awarded_items')
                )
                if needs_refetch:
                    log.info("RE-FETCHING respondents/state: %s", t["title"][:55])

                    # Snapshot state BEFORE re-fetch to detect what changed
                    was_under_eval  = e.get("under_evaluation", False)
                    was_all_pending = all(r.get("price_pending") for r in e.get("respondents", [{}]))
                    prev_awarded_to = e.get("awarded_to")
                    was_awarded = e.get('status') == 'Awarded'

                    resp = scraper.get_respondents(t)
                    old_resp = e.get('respondents', [])
                    if resp and ((len(resp) > len(old_resp)) or (len(resp) == len(old_resp) and sum(r.get('opt_a') is not None for r in resp) >= sum(r.get('opt_a') is not None for r in old_resp))):
                        e['respondents'] = resp
                    elif resp:
                        log.warning('  Retaining previous respondents (%d vs new %d)', len(old_resp), len(resp))
                    for extra in ('respondents_expected', 'respondents_complete', 'requires_option_b', 'hdb_verified', 'status'):
                        if extra in t: e[extra] = t[extra]

                    # Sync state flags set by get_respondents onto stored entry
                    for flag in ("two_envelope", "under_evaluation", "awarded_to", "awarded_value", "awarded_date", "awarded_items", "award_confirmed"):
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
                    now_awarded     = (e.get('status') == 'Awarded' and not (prev_awarded_to or was_awarded))

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

    # Retain legacy records only if they pass the updated title filter.
    # Existing records without verified agency are kept for review instead of
    # silently discarding history; new entries require HDB verification.
    for row in data['open'] + data['closed']:
        project = PROJECT_RE.search(row.get('title', ''))
        du = DU_COUNT_RE.search(row.get('title', ''))
        if project: row['project_code'] = project.group(1).upper()
        row['project_codes'] = list(dict.fromkeys(m.group(1).upper() for m in PROJECT_RE.finditer(row.get('title',''))))
        if du: row['du_count'] = int(du.group(1).replace(',', ''))
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
