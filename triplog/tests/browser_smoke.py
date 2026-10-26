"""Browser smoke harness for IDMate (Playwright, headless Chromium).

Not part of the pytest suite (no test_ prefix): needs `pip install playwright`
+ `playwright install chromium`. Run from triplog/:
    python tests/browser_smoke.py [--port 5098]
Checks all main pages for console/page/CSP errors, clicks through core
interactions, and includes two 7.2 probes: CSP nonce enforcement (a
nonce-less inline script must be blocked) and the CSRF fetch wrapper
(POST without header -> 200).

Starts the app as a subprocess (python app.py, i.e. __main__ as in the
container), creates an admin user and then walks all main pages:
  - collects console errors and pageerrors (JS exceptions)
  - collects CSP violations (securitypolicyviolation events)
  - clicks through core interactions (admin tabs, vehicle picker, modals)

Exit 0 = clean; exit 1 = error list on stdout.
Run: /tmp/idm/bin/python /tmp/browser_smoke.py [--port 5098]
"""
import os, sys, time, subprocess, signal, tempfile
from playwright.sync_api import sync_playwright

PORT = int(sys.argv[sys.argv.index("--port")+1]) if "--port" in sys.argv else 5098
BASE = f"http://127.0.0.1:{PORT}"
TRIPLOG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # triplog/
PY = sys.executable

GPX = (b'<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
       b'<trk><trkseg><trkpt lat="48.1" lon="11.5"><time>2026-06-01T10:00:00Z</time></trkpt>'
       b'<trkpt lat="48.2" lon="11.6"><time>2026-06-01T10:30:00Z</time></trkpt></trkseg></trk></gpx>')

PAGES = ["/", "/trips", "/journeys", "/journeys/1", "/trips/1",
         "/charges", "/analysis", "/admin", "/profile", "/login"]

# Console messages that are not a page bug (missing backends in the smoke setup)
IGNORE = ["favicon", "tile.openstreetmap", "ERR_CONNECTION", "ERR_NAME",
          "Failed to load resource: the server responded with a status of 503",
          "the server responded with a status of 401",
          "Manifest", "apple-touch"]


def start_app(db):
    env = dict(os.environ, SECRET_KEY="smoke", DB_PATH=db, FLASK_DEV="0")
    # App hard-binds to :5000 in __main__ (occupied on macOS) — the wrapper serves
    # on a free port; the real __main__ boot path is covered by the CI boot smoke.
    wrapper = (f"import app; app._check_setup(); from waitress import serve; "
               f"serve(app.app, host='127.0.0.1', port={PORT}, threads=8)")
    p = subprocess.Popen([PY, "-c", wrapper], cwd=TRIPLOG, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import urllib.request
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{BASE}/healthz", timeout=1); return p
        except Exception:
            time.sleep(0.5)
    p.kill(); raise RuntimeError("App did not come up")


def seed(page):
    """Create setup wizard + test data via the UI/API (using the browser's CSRF)."""
    page.goto(f"{BASE}/setup")
    if "/setup" in page.url:
        page.fill('input[name="username"]', "admin")
        page.fill('input[name="password"]', "Sup3r!secret")
        page.fill('input[name="password_confirm"]', "Sup3r!secret")
        page.click('button[type="submit"]')
        page.wait_for_load_state()
    # Skip 2FA setup if offered
    if "/setup/2fa" in page.url:
        try:
            page.click('button[value="skip"], button:has-text("Überspringen"), button:has-text("Skip")', timeout=3000)
            page.wait_for_load_state()
        except Exception:
            page.goto(BASE)  # skip-button variant unknown -> just continue
    # Vehicle + GPX trip + journey via fetch in the page context (carries the CSRF cookie)
    page.goto(BASE)
    page.evaluate("""async () => {
        const csrf = document.cookie.split('; ').find(r=>r.startsWith('csrf_token='))?.split('=')[1];
        const H = {'X-CSRFToken': csrf};
        await fetch('/api/vehicles', {method:'POST', headers:{...H,'Content-Type':'application/json'},
            body: JSON.stringify({name:'Test', plate:'TEST-1'})});
        const fd = new FormData();
        fd.append('vehicle','TEST-1');
        fd.append('file', new Blob([%r], {type:'application/gpx+xml'}), 't.gpx');
        await fetch('/api/gpx-import', {method:'POST', headers:H, body: fd});
        await fetch('/api/journeys', {method:'POST', headers:{...H,'Content-Type':'application/json'},
            body: JSON.stringify({title:'Trip', date_from:'2026-06-01', date_to:'2026-06-04'})});
    }""" % GPX.decode())


def main():
    db = tempfile.mktemp(suffix=".db")
    app = start_app(db)
    errors = []   # (where, what)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(ignore_https_errors=True)
            page = ctx.new_page()
            current = {"where": "init"}

            def on_console(msg):
                # during the CSP negative probe exactly ONE block message is expected
                if current["where"] == "probe:csp-enforcement" and "violates" in msg.text:
                    return
                if msg.type == "error" and not any(s in msg.text for s in IGNORE):
                    errors.append((current["where"], f"console: {msg.text[:200]}"))
            def on_pageerror(err):
                errors.append((current["where"], f"pageerror: {str(err)[:200]}"))
            page.on("console", on_console)
            page.on("pageerror", on_pageerror)
            # capture CSP violations in the document
            page.add_init_script("""
                window.__cspViolations = [];
                document.addEventListener('securitypolicyviolation', e => {
                    window.__cspViolations.push(e.violatedDirective + ' ' + (e.blockedURI||'') + ' ' + (e.sourceFile||'') + ':' + e.lineNumber);
                });
            """)

            current["where"] = "seed"
            seed(page)

            for path in PAGES:
                current["where"] = path
                page.goto(BASE + path, wait_until="load")
                page.wait_for_timeout(400)
                for v in page.evaluate("window.__cspViolations || []"):
                    errors.append((path, f"CSP: {v[:200]}"))

            # ── Interactions ──
            current["where"] = "interact:/admin tabs"
            page.goto(BASE + "/admin", wait_until="load")
            tabs = page.locator('[role="tab"]')
            for i in range(tabs.count()):
                tabs.nth(i).click(); page.wait_for_timeout(150)

            current["where"] = "interact:veh-picker"
            page.goto(BASE + "/", wait_until="load")
            vp = page.locator(".veh-picker")
            if vp.count():
                vp.first.click(); page.wait_for_timeout(200)
                page.keyboard.press("Escape")

            current["where"] = "interact:/journeys modal"
            page.goto(BASE + "/journeys", wait_until="load")
            btn = page.locator('button:has-text("Neue Reise"), button:has-text("New journey"), .btn-primary')
            if btn.count():
                btn.first.click(); page.wait_for_timeout(250)
                page.keyboard.press("Escape")

            current["where"] = "interact:/trips toolbar"
            page.goto(BASE + "/trips", wait_until="load")
            page.wait_for_timeout(250)


            # ── CSP/CSRF probes (7.2) ──
            current["where"] = "probe:csp-enforcement"
            page.goto(BASE + "/journeys", wait_until="load")
            blocked = page.evaluate("""() => new Promise(res => {
                const before = (window.__cspViolations||[]).length;
                let ran = false;
                window.__cspProbeRan = () => { ran = true; };
                const sc = document.createElement('script');
                sc.textContent = 'window.__cspProbeRan && window.__cspProbeRan();';
                document.body.appendChild(sc);
                setTimeout(() => res({ran, violations:(window.__cspViolations||[]).length - before}), 300);
            })""")
            if blocked["ran"] or blocked["violations"] < 1:
                errors.append(("probe:csp", f"nonce-less inline script NOT blocked (ran={blocked['ran']}, viol={blocked['violations']})"))
            # blocked probe produces the expected violation -> reset the counter
            page.evaluate("window.__cspViolations = []")

            current["where"] = "probe:csrf-wrapper"
            status = page.evaluate("""async () => {
                const r = await fetch('/api/journeys', {method:'POST',
                    headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({title:'CSRF-Probe', date_from:'2026-06-05', date_to:'2026-06-06'})});
                return r.status;
            }""")
            if status != 200:
                errors.append(("probe:csrf", f"POST without manual header -> {status} (fetch wrapper broken?)"))


            # ── Click probe: JS-generated data-onclick buttons (7.2 regression of
            # the formerly single-quoted onclick='…' handlers). The rename button
            # must open the modal — a dead one would stay silent. ──
            current["where"] = "probe:admin-rename-click"
            page.goto(BASE + "/", wait_until="load")
            page.evaluate("""async () => {   // create a value so the tripbook list renders a rename button
                await fetch('/api/trips/1', {method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({purpose:'Smoke purpose'})});
            }""")
            page.goto(BASE + "/admin", wait_until="load")
            page.wait_for_timeout(800)
            btn = page.locator('[data-onclick="openRename"]').first
            if btn.count():
                btn.click(); page.wait_for_timeout(300)
                opened = page.evaluate("""() => !!document.querySelector(
                    '.modal-bg.open, .modal-bg[style*="flex"], dialog[open]')""")
                if not opened:
                    errors.append(("probe:admin-rename", "Rename click did not open a modal (data-onclick dead?)"))
                page.keyboard.press("Escape"); page.wait_for_timeout(150)
            else:
                errors.append(("probe:admin-rename", "no [data-onclick=openRename] button found"))

            current["where"] = "final-csp-sweep"
            for path in ["/admin", "/"]:
                page.goto(BASE + path, wait_until="load")
                for v in page.evaluate("window.__cspViolations || []"):
                    errors.append((path, f"CSP: {v[:200]}"))

            browser.close()
    finally:
        app.send_signal(signal.SIGTERM); app.wait(timeout=5)
        try: os.unlink(db)
        except OSError: pass

    if errors:
        print(f"FAIL — {len(errors)} finding(s):")
        seen = set()
        for where, what in errors:
            key = (where, what)
            if key in seen: continue
            seen.add(key)
            print(f"  [{where}] {what}")
        sys.exit(1)
    print("PASS — all pages + interactions without console/page/CSP errors")


if __name__ == "__main__":
    main()
