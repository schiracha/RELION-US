"""
Playwright test for password protection (backend/auth.py): unauthenticated
requests get redirected to the login page, a wrong password is rejected in
place, a correct one logs in and reaches the app, and the topbar's Log out
button ends the session again.

Needs a live backend with a password already configured and protection
turned ON (run_tests.sh's make_auth_config does this before starting the
backend for this suite -- see AUTH_TEST_PASSWORD there).

Usage: python3 test_auth.py [base_url]
"""
import os
import sys

from playwright.sync_api import sync_playwright

TEST_PASSWORD = "relion-us-test-password"  # must match run_tests.sh's AUTH_TEST_PASSWORD


def launch_browser(p):
    exe = os.environ.get("RELION_US_CHROMIUM")
    return p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()


BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8434"

errors = []
ok = True


def check(label, cond):
    global ok
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        ok = False


def main():
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        # 401s and the websocket's 403 are the whole point of this test (the
        # gate deliberately refusing an unauthenticated request) -- expected,
        # not a bug, same as test_legacy_project.py's deliberate 409.
        page.on("console", lambda m: errors.append(m.text)
                if m.type == "error" and "401" not in m.text and "403" not in m.text else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        # ---- unauthenticated: every page redirects to the login page ----
        page.goto(BASE_URL + "/", wait_until="networkidle")
        check(f"Visiting / while logged out lands on the login page ({page.url})",
              page.url.rstrip("/").endswith("/login.html"))
        check("Login form is visible", page.locator("#loginForm").is_visible())

        # A direct API call is rejected too, not just the page navigation.
        status = page.evaluate("""async (base) => {
            const r = await fetch(base + '/api/project', {credentials: 'same-origin'});
            return r.status;
        }""", BASE_URL)
        check(f"An API call while logged out gets 401, not data ({status})", status == 401)

        # ---- wrong password: rejected, stays on the login page ----
        page.locator("#password").fill("not the password")
        page.locator("#loginForm button[type=submit]").click()
        page.wait_for_timeout(600)
        check(f"Wrong password shows an error ({page.locator('#loginError').inner_text()!r})",
              "incorrect" in page.locator("#loginError").inner_text().lower())
        check("Still on the login page after a wrong password",
              page.url.rstrip("/").endswith("/login.html"))

        # ---- correct password: logs in and reaches the app ----
        page.locator("#password").fill(TEST_PASSWORD)
        page.locator("#loginForm button[type=submit]").click()
        page.wait_for_selector(".job-item", timeout=5000)
        check(f"Correct password reaches the app ({page.url})",
              not page.url.rstrip("/").endswith("/login.html"))

        # A revisit to /login.html itself, now already authenticated, bounces
        # straight back to the app rather than asking again.
        page.goto(BASE_URL + "/login.html", wait_until="networkidle")
        page.wait_for_timeout(500)
        check(f"Revisiting /login.html while already logged in redirects to the app ({page.url})",
              not page.url.rstrip("/").endswith("/login.html"))

        # ---- the topbar shows Log out, and it actually logs out ----
        logout_btn = page.locator("#logoutBtn")
        check("Log out button is visible once logged in", logout_btn.is_visible())
        logout_btn.click()
        page.wait_for_timeout(500)
        check(f"Clicking Log out lands back on the login page ({page.url})",
              page.url.rstrip("/").endswith("/login.html"))

        status_after = page.evaluate("""async (base) => {
            const r = await fetch(base + '/api/project', {credentials: 'same-origin'});
            return r.status;
        }""", BASE_URL)
        check(f"After logout, the API is 401 again ({status_after})", status_after == 401)

        # ---- the websocket is gated too, not just HTTP routes ----
        ws_rejected = page.evaluate("""(base) => new Promise((resolve) => {
            const wsUrl = base.replace('http', 'ws') + '/ws/runs/whatever';
            const ws = new WebSocket(wsUrl);
            ws.onopen = () => resolve(false);   // connected -- should NOT happen while logged out
            ws.onerror = () => resolve(true);
            ws.onclose = () => resolve(true);
            setTimeout(() => resolve(true), 3000);
        })""", BASE_URL)
        check("The run-output websocket refuses an unauthenticated connection too", ws_rejected)

        browser.close()

    print()
    if errors:
        print("CONSOLE/PAGE ERRORS:")
        for e in errors:
            print(" -", e)
        return False
    print("No console/page errors.")
    return True


clean = main()
print()
print("OVERALL:", "PASS" if (ok and clean) else "FAIL")
sys.exit(0 if (ok and clean) else 1)
