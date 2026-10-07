"""
ShopMy -> Pinterest auto-pinner.

Opens your PUBLIC ShopMy shop page in a headless browser, collects the
products it shows, and creates a Pinterest pin for every product it hasn't
pinned before. Already-pinned products are remembered in pinned.json.

Usage:
    python shopmy_to_pinterest.py              # normal run
    python shopmy_to_pinterest.py --dry-run    # show what would be pinned, post nothing
    python shopmy_to_pinterest.py --list-boards  # print your Pinterest boards and their IDs
    python shopmy_to_pinterest.py --debug      # save raw page data to debug/ for troubleshooting

First run: every product already on your shop is recorded as "seen" WITHOUT
pinning, so you don't flood Pinterest with your whole back catalog.
Add --pin-existing on the first run if you DO want them all pinned.

Settings come from environment variables (see README.md).
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- settings
SHOPMY_USERNAME = (os.environ.get("SHOPMY_USERNAME") or "jordanrundd").strip().lstrip("@")
PINTEREST_BOARD_ID = os.environ.get("PINTEREST_BOARD_ID", "").strip()
PINTEREST_ACCESS_TOKEN = os.environ.get("PINTEREST_ACCESS_TOKEN", "").strip()
# Optional: lets the script renew the access token itself (tokens expire after ~30 days)
PINTEREST_REFRESH_TOKEN = os.environ.get("PINTEREST_REFRESH_TOKEN", "").strip()
PINTEREST_APP_ID = os.environ.get("PINTEREST_APP_ID", "").strip()
PINTEREST_APP_SECRET = os.environ.get("PINTEREST_APP_SECRET", "").strip()

MAX_PINS_PER_RUN = int(os.environ.get("MAX_PINS_PER_RUN", "5"))
DESCRIPTION_TEMPLATE = os.environ.get(
    "DESCRIPTION_TEMPLATE",
    "{title}\n\nShop it through my ShopMy link. #affiliate",
)

STATE_FILE = Path(os.environ.get("STATE_FILE", "pinned.json"))
PINTEREST_API = "https://api.pinterest.com/v5"

TITLE_KEYS = ("title", "name", "product_title", "productTitle", "display_title")
IMAGE_HINTS = ("image", "img", "photo", "thumbnail")
LINK_KEYS = ("link", "url", "link_url", "linkUrl", "affiliate_link", "short_link",
             "shortLink", "product_url", "productUrl", "go_link")


# ---------------------------------------------------------------- ShopMy side
def _is_http(v):
    return isinstance(v, str) and v.startswith(("http://", "https://"))


def _pick(d, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _pick_image(d):
    for k, v in d.items():
        if any(h in k.lower() for h in IMAGE_HINTS):
            if _is_http(v):
                return v
            if isinstance(v, list) and v and _is_http(v[0]):
                return v[0]
    return None


def _pick_link(d):
    # Prefer a ShopMy affiliate link if one is anywhere in this object
    for v in d.values():
        if _is_http(v) and "shopmy" in v and ("/p-" in v or "go." in v):
            return v
    for k in LINK_KEYS:
        v = d.get(k)
        if _is_http(v):
            return v
    return None


def extract_products(data, found):
    """Walk any JSON blob and collect things that look like products."""
    if isinstance(data, dict):
        title = _pick(data, TITLE_KEYS)
        image = _pick_image(data)
        link = _pick_link(data)
        if title and image and link and "shopmy.us/" + SHOPMY_USERNAME != link.rstrip("/").split("//")[-1]:
            raw_id = data.get("id") or data.get("pin_id") or link
            key = hashlib.sha1(f"{raw_id}|{link}".encode()).hexdigest()[:16]
            found.setdefault(key, {"key": key, "title": title[:100], "image": image, "link": link})
        for v in data.values():
            extract_products(v, found)
    elif isinstance(data, list):
        for v in data:
            extract_products(v, found)


def _find_affiliate_link(obj):
    """Look anywhere inside a product for a ShopMy affiliate link or pin ID."""
    if isinstance(obj, str):
        if _is_http(obj) and ("go.shopmy.us" in obj or "shopmy.us/p-" in obj):
            return obj
        return None
    if isinstance(obj, dict):
        for k in ("Pin_id", "pin_id", "PinId"):
            if obj.get(k):
                return f"https://go.shopmy.us/p-{obj[k]}"
        for k in ("pin", "pins", "Pins"):
            v = obj.get(k)
            if isinstance(v, dict) and v.get("id"):
                return f"https://go.shopmy.us/p-{v['id']}"
            if isinstance(v, list) and v and isinstance(v[0], dict) and v[0].get("id"):
                return f"https://go.shopmy.us/p-{v[0]['id']}"
        for v in obj.values():
            hit = _find_affiliate_link(v)
            if hit:
                return hit
    if isinstance(obj, list):
        for v in obj:
            hit = _find_affiliate_link(v)
            if hit:
                return hit
    return None


_shown_keys = False


def extract_shop_results(blob, found, debug=False):
    """Read ShopMy's own product list: {"results": [{"id", "title", "image", ...}]}."""
    global _shown_keys
    results = blob.get("results") if isinstance(blob, dict) else None
    if not isinstance(results, list):
        return
    for r in results:
        if not isinstance(r, dict) or not r.get("title") or not _is_http(r.get("image")):
            continue
        if debug and not _shown_keys:
            print(f"[debug] product fields: {list(r.keys())}")
            print(f"[debug] CURATORS: {json.dumps(r.get('curators'))[:1200]}")
            print(f"[debug] CIRCLE: {json.dumps(r.get('circleCurators'))[:300]}")
            _shown_keys = True
        link = _find_affiliate_link(r) or f"https://shopmy.us/shop/{SHOPMY_USERNAME}"
        brand = r.get("AllBrand_name") or ""
        title = f"{brand} {r['title']}".strip() if brand and brand.lower() not in r["title"].lower() else r["title"]
        key = f"shopmy-{r.get('id') or hashlib.sha1(r['title'].encode()).hexdigest()[:12]}"
        found.setdefault(key, {"key": key, "title": title[:100], "image": r["image"], "link": link})


def fetch_shopmy_products(debug=False):
    base = f"https://shopmy.us/shop/{SHOPMY_USERNAME}"
    urls = [base, f"{base}?tab=collections"]
    blobs = []
    cards = []
    print(f"Version 6: checking {base}")

    def on_response(resp):
        if "shopmy" not in resp.url:
            return
        if resp.request.resource_type not in ("xhr", "fetch"):
            return
        try:
            blobs.append((resp.url, resp.json()))
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 2000})
        page.on("response", on_response)
        for url in urls:
            # ShopMy keeps making background requests forever, so don't wait
            # for the network to go quiet; load the page, then give it time.
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
            except Exception as e:
                print(f"Couldn't load {url}: {e}")
                continue
            page.wait_for_timeout(8000)
            # Scroll so lazily-loaded products come in (the page and any inner scroll areas)
            for _ in range(10):
                page.mouse.wheel(0, 4000)
                page.evaluate(SCROLL_JS)
                page.wait_for_timeout(1500)
            page.wait_for_timeout(3000)
            print(f"Loaded {url} ({len(blobs)} data responses so far)")
            cards.extend(page.evaluate(CARD_JS))
            if debug:
                Path("debug").mkdir(exist_ok=True)
                page.screenshot(path=f"debug/page_{urls.index(url)}.png", full_page=False)
                text = page.inner_text("body")[:1500].replace("\n", " | ")
                print(f"[debug] page text: {text}")
        # Ask ShopMy's product list directly, the same way the page does
        for api in API_GUESSES:
            api = api.format(u=SHOPMY_USERNAME)
            try:
                res = page.evaluate(FETCH_JS, api)
                if res.get("json") is not None:
                    blobs.append((api, res["json"]))
                print(f"Asked {api.split('/api/')[-1][:90]} -> status {res.get('status')}")
            except Exception as e:
                print(f"Asked {api} -> failed: {e}")
        if debug:
            explore_collections(page, blobs)
        browser.close()

    print("Data the page loaded:")
    for u, _ in blobs:
        print(f"  {u.split('?')[0].split('/api/')[-1]}  ?{u.split('?', 1)[1][:80] if '?' in u else ''}")

    if debug:
        Path("debug").mkdir(exist_ok=True)
        for i, (u, b) in enumerate(blobs):
            Path(f"debug/response_{i:02d}.json").write_text(
                json.dumps({"url": u, "body": b}, indent=2)[:2_000_000])
            if any(w in u.lower() for w in ("product", "pin", "collection", "section")):
                print(f"[debug] response {i}: {u[:150]}")
                print(f"        {json.dumps(b)[:700]}")
        print(f"[debug] {len(cards)} product cards seen on the page")
        for c in cards[:5]:
            print(f"        {c}")

    found = {}
    for u, blob in blobs:
        if "/api/Shop/products" in u and "refinements" not in u:
            extract_shop_results(blob, found, debug)
    if not found:
        for _, blob in blobs:
            extract_products(blob, found)
    if not found:
        # Fallback: use the product cards as shown on the page
        for c in cards:
            if c.get("title") and c.get("image") and c.get("link"):
                key = hashlib.sha1(c["link"].encode()).hexdigest()[:16]
                found.setdefault(key, {"key": key, "title": c["title"][:100],
                                       "image": c["image"], "link": c["link"]})
    return list(found.values())


def explore_collections(page, blobs):
    """Debug only: show what a collection and its items look like."""
    api = f"https://apiv3.shopmy.us/api/Shop/Collections?Curator_username={SHOPMY_USERNAME}&limit=24"
    res = page.evaluate(FETCH_JS, api)
    print(f"[debug] COLLECTIONS: {json.dumps(res.get('json'))[:2500]}")
    # Open the first collection the way a shopper would, and record what loads
    start = len(blobs)
    try:
        page.goto(f"https://shopmy.us/shop/{SHOPMY_USERNAME}", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(8000)
        page.get_by_text("View Full Collection").first.click(timeout=15000)
        page.wait_for_timeout(10000)
        print(f"[debug] opened collection page: {page.url}")
    except Exception as e:
        print(f"[debug] couldn't open a collection: {e}")
    for u, b in blobs[start:]:
        if "pa.shopmy" in u or "rudder" in u or "Events" in u:
            continue
        print(f"[debug] COLLECTION DATA {u.split('/api/')[-1][:120]}")
        print(f"        {json.dumps(b)[:1500]}")
    links = page.evaluate("""() => [...document.querySelectorAll('a[href]')].map(a => a.href)
        .filter(h => /go\\.shopmy|shopmy\\.us\\/p-|\\/p\\//.test(h)).slice(0, 5)""")
    print(f"[debug] affiliate-looking links on page: {links}")


SCROLL_JS = """
() => { for (const el of document.querySelectorAll('*')) {
  if (el.scrollHeight > el.clientHeight + 50 && /(auto|scroll)/.test(getComputedStyle(el).overflowY)) el.scrollTop += 4000; } }
"""

FETCH_JS = """
async (url) => {
  try {
    const r = await fetch(url, {credentials: 'include'});
    let j = null; try { j = await r.json(); } catch (e) {}
    return {status: r.status, json: j};
  } catch (e) { return {status: 'error ' + e}; }
}
"""

API_GUESSES = [
    "https://apiv3.shopmy.us/api/Shop/products?Curator_username={u}",
    "https://apiv3.shopmy.us/api/Shop/products?Curator_username={u}&page=0&limit=100",
    "https://apiv3.shopmy.us/api/Shop/products?Curator_username={u}&skipRefinements=true",
]


# Finds every link on the page that contains a product image.
CARD_JS = """
() => {
  const out = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    const img = a.querySelector('img') || a.closest('div')?.querySelector('img');
    if (!img) continue;
    const href = a.href;
    if (!href.startsWith('http') || seen.has(href)) continue;
    // skip links that just go to other pages of the shop itself
    if (/shopmy\\.us\\/(shop|collections?)\\b/.test(href) && !/\\/p-|go\\./.test(href)) continue;
    const src = img.currentSrc || img.src || '';
    if (!src.startsWith('http') || (img.naturalWidth && img.naturalWidth < 80)) continue;
    const card = a.closest('[class*="product" i], [class*="pin" i], [class*="item" i]') || a;
    const title = (img.alt || card.innerText || a.title || '').trim().split('\\n')[0];
    if (!title) continue;
    seen.add(href);
    out.push({title, image: src, link: href});
  }
  return out;
}
"""


# ---------------------------------------------------------------- Pinterest side
def refresh_access_token():
    if not (PINTEREST_REFRESH_TOKEN and PINTEREST_APP_ID and PINTEREST_APP_SECRET):
        return PINTEREST_ACCESS_TOKEN
    basic = base64.b64encode(f"{PINTEREST_APP_ID}:{PINTEREST_APP_SECRET}".encode()).decode()
    r = requests.post(
        f"{PINTEREST_API}/oauth/token",
        headers={"Authorization": f"Basic {basic}"},
        data={"grant_type": "refresh_token", "refresh_token": PINTEREST_REFRESH_TOKEN},
        timeout=30,
    )
    if r.ok:
        return r.json()["access_token"]
    print(f"Warning: token refresh failed ({r.status_code}): {r.text[:200]}")
    return PINTEREST_ACCESS_TOKEN


def list_boards(token):
    r = requests.get(f"{PINTEREST_API}/boards", headers={"Authorization": f"Bearer {token}"},
                     params={"page_size": 100}, timeout=30)
    r.raise_for_status()
    for b in r.json().get("items", []):
        print(f"{b['id']}  {b['name']}")


def create_pin(token, product):
    body = {
        "board_id": PINTEREST_BOARD_ID,
        "title": product["title"],
        "description": DESCRIPTION_TEMPLATE.format(title=product["title"])[:500],
        "link": product["link"],
        "alt_text": product["title"][:500],
        "media_source": {"source_type": "image_url", "url": product["image"]},
    }
    r = requests.post(f"{PINTEREST_API}/pins", json=body,
                      headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Pinterest error {r.status_code}: {r.text[:300]}")
    return r.json().get("id")


# ---------------------------------------------------------------- main
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list-boards", action="store_true")
    ap.add_argument("--pin-existing", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    if args.list_boards:
        if not PINTEREST_ACCESS_TOKEN:
            sys.exit("Add the PINTEREST_ACCESS_TOKEN secret first.")
        list_boards(refresh_access_token())
        return

    if not SHOPMY_USERNAME:
        sys.exit("Set SHOPMY_USERNAME (the part after shopmy.us/).")

    products = fetch_shopmy_products(debug=args.debug)
    print(f"Found {len(products)} products on shopmy.us/shop/{SHOPMY_USERNAME}")
    if not products:
        sys.exit("No products found. Run with --debug and check the debug/ folder.")

    state = load_state()
    first_run = state is None
    state = state or {"pinned": {}}

    if first_run and not args.pin_existing:
        for p in products:
            state["pinned"][p["key"]] = {"title": p["title"], "pin_id": None, "seeded": True}
        if not args.dry_run:
            save_state(state)
        print("First run: recorded existing products without pinning. New ones will be pinned from now on.")
        return

    new = [p for p in products if p["key"] not in state["pinned"]]
    print(f"{len(new)} new product(s)")
    if not new:
        return

    if args.dry_run:
        for p in new:
            print(f"Would pin: {p['title']} -> {p['link']}")
        return

    if not (PINTEREST_ACCESS_TOKEN and PINTEREST_BOARD_ID):
        print("Pinterest isn't set up yet (missing PINTEREST_ACCESS_TOKEN or PINTEREST_BOARD_ID), so nothing was pinned.")
        return
    token = refresh_access_token()

    for p in new[:MAX_PINS_PER_RUN]:
        try:
            pin_id = create_pin(token, p)
            state["pinned"][p["key"]] = {"title": p["title"], "pin_id": pin_id}
            save_state(state)
            print(f"Pinned: {p['title']} (pin {pin_id})")
            time.sleep(5)
        except Exception as e:
            print(f"Failed on {p['title']}: {e}")

    if len(new) > MAX_PINS_PER_RUN:
        print(f"{len(new) - MAX_PINS_PER_RUN} more will be pinned on later runs.")


if __name__ == "__main__":
    main()
