"""
FirstCry Hot Wheels Restock/Price Bot (with auto-discovery)
--------------------------------------------------------------
Two things this script does:

1. AUTO-DISCOVERY: scans FirstCry Hot Wheels category/listing pages,
   finds every product priced at one of your TARGET_PRICES (e.g. 167,
   179, 549) that is CURRENTLY OUT OF STOCK, and starts tracking it
   automatically. You never have to paste in product URLs yourself -
   the bot rebuilds this list on every run.
2. RESTOCK ALERTS: compares each run's out-of-stock list against the
   previous run. Anything that was out of stock last time but isn't
   anymore gets a Telegram alert - that's your restock.

You can ALSO still add specific product URLs by hand to PRODUCTS below
if there's a particular one you want watched regardless of price.

NOTE: FirstCry already has a native "Notify Me" feature on out-of-stock
product pages (enter email + mobile number, they'll email you when it's
back). That still works and is a fine backup alongside this bot.

SETUP
1. pip install requests beautifulsoup4
2. Create a Telegram bot:
     - Message @BotFather on Telegram -> /newbot -> copy the API token
     - Message your new bot once (anything), then visit:
         https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
       and copy your numeric "chat id" from the JSON response.
3. Fill in TELEGRAM_TOKEN and TELEGRAM_CHAT_ID below.
4. Run: python firstcry_restock_bot.py
5. Schedule it (see bottom of file) so it runs every few minutes on its own.

NOTES / LIMITATIONS (read before relying on this)
- Stock status on FirstCry depends on your delivery PINCODE and changes
  constantly - a product can show in stock for one pincode and out for
  another. This script has no pincode set, so it sees FirstCry's default
  view, which may not exactly match what you see when logged in with
  your address. Treat alerts as "worth checking now", not gospel.
- FirstCry's product/listing pages are partly JavaScript-rendered, so a
  plain requests.get() may not always see the same text a browser would.
  If discovery or stock detection seems unreliable, switch FETCH_MODE to
  "selenium" (instructions below) so the page renders fully before it's
  read.
- The auto-discovery parser is a best-effort text scraper (it reads
  visible page text, not fixed CSS classes) because FirstCry's markup
  can change. If FirstCry redesigns their listing pages, you may need
  to adjust discover_target_products() below.
- Be a polite bot: don't set CHECK_INTERVAL_MINUTES below 10-15 minutes,
  and never hammer the site. Repeated aggressive requests can get your
  IP blocked or run afoul of FirstCry's terms of service.
"""

import json
import os
import re
import time
import requests
from bs4 import BeautifulSoup

# ---------------- CONFIG ----------------

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

CHECK_INTERVAL_MINUTES = 15  # how often to check, when run in a loop

# "requests" is faster and simpler; "selenium" renders JS first (more
# reliable on FirstCry's product pages, but needs Chrome + chromedriver
# and `pip install selenium`).
FETCH_MODE = "requests"

# Optional: specific FirstCry product URLs to always watch, regardless
# of price. target_price is optional per entry - set to None to only
# alert on restock for that one.
PRODUCTS = [
    # {
    #     "name": "Hot Wheels Premium Collector Display Set",
    #     "url": "https://www.firstcry.com/hot-wheels/hot-wheels-premium-collector-display-sets-3-cars-and-1-transporter-white/21497090/product-detail",
    #     "target_price": 549,
    # },
]

# Auto-discovery: prices (in Rupees) to watch for across the category
# pages below. A product matches if its listed/club price is within
# PRICE_TOLERANCE of one of these.
TARGET_PRICES = [167, 179, 549]
PRICE_TOLERANCE = 2

# FirstCry category/listing pages to scan for Hot Wheels products.
# Add more category URLs here if you want wider coverage (e.g. track
# sets, monster trucks).
CATEGORY_URLS = [
    "https://www.firstcry.com/toy-cars,-trains-and-vehicles/cars-and-jeeps/hotwheels?cid=5&scid=94&sub-type=t1-7973&character-shop=t5-7701",
    "https://www.firstcry.com/hotwheels/toy-cars,-trains-and-vehicles/5/94/113",
]

STATE_FILE = "firstcry_restock_state.json"

# FirstCry-specific wording seen on product pages.
STOCK_KEYWORDS = ["add to cart", "add to bag", "buy now"]
OUT_OF_STOCK_KEYWORDS = ["notify me", "out of stock", "sold out", "currently unavailable"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# ---------------- CORE LOGIC ----------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
    except requests.RequestException as e:
        print(f"[warn] Telegram send failed: {e}")


def extract_price(text: str):
    """Pulls the first rupee amount like '₹549' or 'Rs. 549' out of page text."""
    match = re.search(r"(?:₹|Rs\.?\s?)\s?([\d,]+)", text)
    if match:
        return int(match.group(1).replace(",", ""))
    return None


def fetch_page_html(url: str) -> str:
    """
    Fetches page HTML. FirstCry pages are partly JS-rendered, so plain
    requests sometimes miss stock status that only appears after the
    page's JavaScript runs. If STOCK_KEYWORDS keep looking wrong with
    "requests", switch FETCH_MODE to "selenium" below.
    """
    if FETCH_MODE == "selenium":
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        driver = webdriver.Chrome(options=options)
        try:
            driver.get(url)
            time.sleep(4)  # let the page's JS finish rendering
            return driver.page_source
        finally:
            driver.quit()
    else:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.text


def discover_target_products(category_url: str, target_prices, tolerance: float = 2):
    """
    Scans a FirstCry category/listing page and returns every product
    that is (a) priced at/near one of target_prices and (b) currently
    shown as out of stock ("Out of Stock" / "Notify me" nearby).

    Best-effort text scraper: pairs each product's title link with the
    page text that follows it (up to the next product's title link) and
    looks for a "Club Price: <number>" plus an out-of-stock indicator
    in that chunk. If FirstCry changes their page layout this may need
    adjusting.
    """
    try:
        html = fetch_page_html(category_url)
    except Exception as e:
        print(f"[error] Could not fetch category page {category_url}: {e}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    full_text = soup.get_text(separator=" ")

    skip_texts = {"added to cart", "shortlisted", "already viewed", "add to cart", "shortlist"}
    title_links = []
    seen_hrefs = set()
    for a in soup.find_all("a", href=True):
        if "product-detail" not in a["href"]:
            continue
        text = a.get_text(strip=True)
        if not text or text.lower() in skip_texts or len(text) < 15:
            continue
        href = a["href"]
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        title_links.append((text, href))

    discovered = []
    search_from = 0
    for i, (name, href) in enumerate(title_links):
        idx = full_text.find(name, search_from)
        if idx == -1:
            idx = full_text.find(name)
            if idx == -1:
                continue
        end = len(full_text)
        if i + 1 < len(title_links):
            next_idx = full_text.find(title_links[i + 1][0], idx + len(name))
            if next_idx != -1:
                end = next_idx
        chunk = full_text[idx:end]
        search_from = idx + len(name)

        price_match = re.search(r"Club Price:\s*([\d.]+)", chunk)
        if not price_match:
            price_match = re.search(r"\b(\d{2,5}(?:\.\d+)?)\b", chunk)
        price = float(price_match.group(1)) if price_match else None
        chunk_lower = chunk.lower()
        is_out_of_stock = "out of stock" in chunk_lower or "notify me" in chunk_lower

        if price is not None and is_out_of_stock:
            if any(abs(price - t) <= tolerance for t in target_prices):
                full_url = href if href.startswith("http") else "https://www.firstcry.com" + href
                discovered.append({"name": name, "url": full_url, "price": price})

    return discovered


def check_product(product: dict, state: dict):
    name, url, target_price = product["name"], product["url"], product.get("target_price")

    try:
        html = fetch_page_html(url)
    except Exception as e:
        print(f"[error] Could not fetch {name}: {e}")
        return

    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(separator=" ").lower()

    in_stock = any(k in page_text for k in STOCK_KEYWORDS) and not any(
        k in page_text for k in OUT_OF_STOCK_KEYWORDS
    )
    price = extract_price(page_text)

    prev = state.get(url, {"in_stock": None, "price": None})

    # Restock alert: was out of stock (or unknown), now in stock
    if in_stock and prev["in_stock"] is False:
        send_telegram(f"🔥 RESTOCKED: {name}\n{url}")

    # Price-drop alert: price now at/under target, wasn't before
    if target_price and price and price <= target_price:
        if prev["price"] is None or prev["price"] > target_price:
            send_telegram(f"💰 PRICE DROP: {name} now ₹{price}\n{url}")

    state[url] = {"in_stock": in_stock, "price": price}
    print(f"[ok] {name}: in_stock={in_stock}, price={price}")


def run_once():
    state = load_state()

    # --- Auto-discovery: find out-of-stock items at target prices ---
    prev_oos_urls = set(state.get("_discovered_oos_urls", []))
    current_oos = {}
    for category_url in CATEGORY_URLS:
        for item in discover_target_products(category_url, TARGET_PRICES, PRICE_TOLERANCE):
            current_oos[item["url"]] = item

    # Anything that WAS out of stock last run but is no longer in this
    # run's out-of-stock list has likely been restocked.
    restocked_urls = prev_oos_urls - set(current_oos.keys())
    for url in restocked_urls:
        last_known = state.get("_discovered_names", {}).get(url, url)
        print(f"[discover] Possible restock: {last_known}")
        send_telegram(f"🔥 POSSIBLE RESTOCK (verify before ordering): {last_known}\n{url}")

    # Report newly-discovered out-of-stock items (no alert, just a log,
    # since these are the ones you're now waiting on).
    newly_seen = set(current_oos.keys()) - prev_oos_urls
    for url in newly_seen:
        print(f"[discover] Now tracking (out of stock): {current_oos[url]['name']} - ₹{current_oos[url]['price']}")

    state["_discovered_oos_urls"] = list(current_oos.keys())
    state["_discovered_names"] = {url: item["name"] for url, item in current_oos.items()}

    # --- Manually-listed products (checked individually, more reliable) ---
    for product in PRODUCTS:
        check_product(product, state)

    save_state(state)


if __name__ == "__main__":
    # When run locally/continuously, uncomment the loop below.
    # When run via GitHub Actions (or any external scheduler like cron),
    # leave this as a single run_once() call - the scheduler handles
    # repeating it every 15 minutes, which is more efficient than a
    # script sitting in an infinite loop.
    run_once()

    # --- Uncomment for a standalone always-on loop instead ---
    # while True:
    #     run_once()
    #     time.sleep(CHECK_INTERVAL_MINUTES * 60)

# ---------------- SCHEDULING (pick one) ----------------
#
# A) Cron (Linux/Mac) - run once every 15 min instead of looping:
#      Remove the while-loop above, keep only run_once() at the bottom.
#      Then: crontab -e
#      */15 * * * * /usr/bin/python3 /path/to/firstcry_restock_bot.py
#
# B) Windows Task Scheduler:
#      Create a basic task -> Trigger: repeat every 15 min ->
#      Action: run "python.exe" with argument "C:\path\to\firstcry_restock_bot.py"
#
# C) GitHub Actions (free, runs in the cloud, no computer needed):
#      Put this script in a repo, add a workflow file that runs it on a
#      cron schedule (e.g. "*/15 * * * *"), and store TELEGRAM_TOKEN /
#      TELEGRAM_CHAT_ID as GitHub Secrets instead of hardcoding them.
