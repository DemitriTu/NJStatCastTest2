from playwright.sync_api import sync_playwright
import re

url = "https://highschoolsports.nj.com/school/oradell-bergen-catholic/boysbasketball/season/2025-2026"
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)
    rows = page.locator("tr:has(a[href*='/game/'])")
    print("rows", rows.count())
    for i in range(min(6, rows.count())):
        row = rows.nth(i)
        tds = row.locator("td")
        cells = [
            tds.nth(j).inner_text().strip().replace("\n", " ")
            for j in range(tds.count())
        ]
        html = row.inner_html()
        print("---", i, cells)
        print(" hints", re.findall(r"vs\.?|@|home|away|fa-", html, re.I)[:12])
        # opponent cell html
        if tds.count() >= 2:
            print(" opp_html", tds.nth(1).inner_html()[:300].replace("\n", " "))
    b.close()
