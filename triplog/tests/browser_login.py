"""Browser check of the passkey sign-in (Playwright, headless Chromium with a
virtual authenticator). Not part of the pytest suite (no test_ prefix), run
from triplog/:
    python tests/browser_login.py [--port 5097]

Checks what only a browser can show:
  - the sign-in options are fetched ahead, the button opens the dialog with
    them (no request between click and dialog — Safari needs that);
  - a pending autofill request does not block the button;
  - after a refused or cancelled attempt the next one works without a reload;
  - a page open for more than 4 min has fetched a fresh challenge, a page
    open for more than 30 min has loaded itself afresh;
  - a passkey picked in the user name autofill signs in.

Exit 0 = clean; exit 1 = error list on stdout.
"""
import os, sys, time, subprocess, tempfile
from playwright.sync_api import sync_playwright

PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 5097
BASE = f"http://localhost:{PORT}"          # localhost: a secure context without TLS
TRIPLOG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROMIUM = os.environ.get("PW_CHROMIUM")


def start_app(db):
    env = dict(os.environ, SECRET_KEY="smoke", DB_PATH=db, FLASK_DEV="0")
    wrapper = (f"import app; app._check_setup(); from waitress import serve; "
               f"serve(app.app, host='127.0.0.1', port={PORT}, threads=8)")
    p = subprocess.Popen([sys.executable, "-c", wrapper], cwd=TRIPLOG, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import urllib.request
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{BASE}/healthz", timeout=1); return p
        except Exception:
            time.sleep(0.5)
    p.kill(); raise RuntimeError("App did not come up")


def main():
    db = tempfile.mktemp(suffix=".db")
    app = start_app(db)
    errors, begins = [], []

    def check(ok, what):
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            errors.append(what)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**({"executable_path": CHROMIUM} if CHROMIUM else {}))
            ctx = browser.new_context()
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            # console errors and CSP violations; a refused passkey answers 401 on purpose
            page.on("console", lambda m: errors.append(f"console: {m.text}")
                    if m.type == "error" and "status of 401" not in m.text else None)
            page.on("request", lambda r: begins.append(time.time()) if r.url.endswith("/login/passkey/begin") else None)
            cdp = ctx.new_cdp_session(page)
            cdp.send("WebAuthn.enable")
            auth = cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
                "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
                "hasUserVerification": True, "isUserVerified": True,
                "automaticPresenceSimulation": True}})["authenticatorId"]

            def verified(yes):
                cdp.send("WebAuthn.setUserVerified", {"authenticatorId": auth, "isUserVerified": yes})

            def touch(yes):
                # off: the device waits for the finger, so the autofill request stays pending
                cdp.send("WebAuthn.setAutomaticPresenceSimulation", {"authenticatorId": auth, "enabled": yes})

            # The device answers a request only when the finger was already on it
            # as the request began: off while the page loads (the autofill request
            # stays pending, as in a real browser), on just before the dialog.
            ctx.add_init_script("""(() => {
              const get = navigator.credentials.get.bind(navigator.credentials);
              navigator.credentials.get = async o => {
                if (o.mediation !== 'conditional') await new Promise(r => { window.__dialog = r; });
                return get(o);
              };
            })()""")

            def press():
                page.click("#pkBtn")
                for _ in range(50):               # (no wait_for_function: the page's CSP forbids its eval)
                    if page.evaluate("!!window.__dialog"):
                        break
                    page.wait_for_timeout(100)
                touch(True)
                page.evaluate("window.__dialog(); window.__dialog = null")
                touch(False)                      # only this dialog: a later autofill request stays pending

            def login_page():
                ctx.clear_cookies()
                touch(False)
                del begins[:]
                page.goto(f"{BASE}/login")
                page.locator("#pkBtn").wait_for(state="visible")
                page.wait_for_timeout(600)        # the options are fetched ahead

            # an account with a passkey
            page.goto(f"{BASE}/setup")
            page.fill('input[name="username"]', "admin")
            page.fill('input[name="password"]', "Sup3r!secret")
            page.fill('input[name="password_confirm"]', "Sup3r!secret")
            page.click('button[type="submit"]')
            page.wait_for_load_state()
            page.goto(f"{BASE}/profile")
            touch(True)
            added = page.evaluate("IDmatePasskey.register('Test').then(r => r.ok, e => String(e))")
            check(added is True, f"passkey added ({added})")

            # 1. the button uses the options fetched ahead, with the autofill request pending
            login_page()
            check(len(begins) == 1, f"options fetched ahead ({len(begins)} request)")
            press()
            page.wait_for_url(f"{BASE}/")
            check(len(begins) == 1, f"no request between click and dialog ({len(begins)})")

            # 2. refused by the device (no fingerprint): the next attempt works without a reload
            login_page()
            verified(False)
            press()
            page.wait_for_timeout(1500)
            check("/login" in page.url, "refused attempt stays on the login page")
            check(len(begins) == 2, f"fresh options after the refused attempt ({len(begins)})")
            verified(True)
            press()
            page.wait_for_url(f"{BASE}/")
            check(True, "second attempt signs in without a reload")

            # 3. the server refuses (challenge of another tab): message, then it works
            login_page()
            page.evaluate("fetch('/login/passkey/begin', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})")
            page.wait_for_timeout(400)
            press()
            page.locator("#loginError").wait_for(state="visible")
            check(True, "refused by the server: " + page.inner_text("#loginError"))
            page.wait_for_timeout(600)
            press()
            page.wait_for_url(f"{BASE}/")
            check(True, "next attempt signs in")

            # 4. open for 5 min: fresh challenge; open for 31 min: page loaded afresh
            ctx.clear_cookies()
            touch(False)
            page.clock.install()
            del begins[:]
            page.goto(f"{BASE}/login")
            page.locator("#pkBtn").wait_for(state="visible")
            page.wait_for_timeout(600)
            page.clock.fast_forward("05:00")
            page.wait_for_timeout(600)
            check(len(begins) == 2, f"fresh challenge after 5 min ({len(begins)})")
            page.evaluate("window.__same = true")
            page.clock.fast_forward("26:00")
            page.wait_for_timeout(1200)
            check(page.evaluate("window.__same") is None, "page loaded afresh after 31 min")
            page.locator("#pkBtn").wait_for(state="visible")
            page.wait_for_timeout(600)
            press()
            page.wait_for_url(f"{BASE}/")
            check(True, "sign-in on the renewed page")

            # 5. a passkey picked in the user name autofill signs in (the test
            # device answers the autofill request by itself once it is touched)
            ctx.clear_cookies()
            touch(True)
            page.goto(f"{BASE}/login?next=/trips")
            page.wait_for_url(f"{BASE}/trips")
            check(True, "passkey from the autofill signs in and goes on to the page asked for")

            # 6. while someone types, the page is left alone
            ctx.clear_cookies()
            touch(False)
            page.goto(f"{BASE}/login")
            page.wait_for_timeout(400)
            page.evaluate("window.__same = true")
            page.clock.fast_forward("29:30")
            page.fill("#password", "abc")
            page.clock.fast_forward("01:00")
            page.wait_for_timeout(800)
            check(page.evaluate("window.__same") is True, "no reload while typing")
            browser.close()
    finally:
        app.terminate()
        try:
            os.unlink(db)
        except OSError:
            pass
    if errors:
        print("\n".join(["", "ERRORS:"] + errors)); sys.exit(1)
    print("clean")


if __name__ == "__main__":
    main()
