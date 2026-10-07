"""
ShopMy -> Pinterest auto-pinner  (version 7)

Reads the items in your PUBLIC ShopMy collections and creates a Pinterest pin
for every item it hasn't pinned before. Each pin links to that item's ShopMy
affiliate link (go.shopmy.us/p-<id>), so you get credit for sales.
Already-pinned items are remembered in pinned.json.

Usage:
    python shopmy_to_pinterest.py                # normal run
    python shopmy_to_pinterest.py --dry-run      # show what would be pinned, post nothing
    python shopmy_to_pinterest.py --list-boards  # print your Pinterest boards and their IDs
    python shopmy_to_pinterest.py --debug        # print extra detail for troubleshooting

First run: every item already in your collections is recorded as "seen" WITHOUT
pinning, so you don't flood Pinterest with your whole back catalog.
Add --pin-existing on the first run if you DO want them all pinned.
"""

import argparse
import base64
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

MAX_PINS_PER_RUN = int(os.environ.get("MAX_PINS_PER_RUN") or "5")
DESCRIPTION_TEMPLATE = os.environ.get("DESCRIPTION_TEMPLATE") or (
    "{title}\n\nShop it through my ShopMy link. #affiliate"
)

STATE_FILE = Path(os.environ.get("STATE_FILE") or "pinned.json")
PINTEREST_API = "https://api.pinterest.com/v5"
SHOPMY_API = "https://apiv3.shopmy.us/api"


# ---------------------------------------------------------------- ShopMy side
def _is_http(v):
    return isinstance(v, str) and v.startswith(("http://", "https://"))


def pins_in(data, found, collection_name, under_pin_list=False):
    """Collect every ShopMy item ("pin") found anywhere in a collection's data.

    An item is a dict that sits in a list whose key mentions "pin" (e.g. "pins",
    "preview_pins") and has a numeric id, a title and an image.
    """
    if isinstance(data, dict):
        if under_pin_list:
            pid = data.get("id")
            title = data.get("title") or data.get("Product_title")
            image = data.get("image")
            if isinstance(pid, int) and title and _is_http(image):
                brand = (data.get("Product_brand") or "").strip()
                product = (data.get("Product_title") or "").strip()
                if brand and product:
                    nice = f"{brand} {product}"
                else:
                    nice = title.replace(" - ", " ").strip()
                key = f"pin-{pid}"
                found.setdefault(key, {
                    "key": key,
                    "title": nice[:100],
                    "image": image,
                    "link": f"https://go.shopmy.us/p-{pid}",
                    "collection": collection_name,
                })
        for k, v in data.items():
            pins_in(v, found, collection_name, under_pin_list="pin" in k.lower())
    elif isinstance(data, list):
        for v in data:
            pins_in(v, found, collection_name, under_pin_list)


def fetch_shopmy_products(debug=False):
    shop = f"https://shopmy.us/shop/{SHOPMY_USERNAME}"
    print(f"Version 7: checking {shop}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        # Open the shop once so requests below look like they come from the page
        try:
            page.goto(shop, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(5000)
        except Exception as e:
            print(f"Couldn't open {shop}: {e}")

        def get(path):
            res = page.evaluate(FETCH_JS, f"{SHOPMY_API}/{path}")
            if res.get("status") != 200:
                print(f"  {path[:80]} -> status {res.get('status')}")
            return res.get("json")

        # 1. Find all collections (across every section of the shop)
        collections = {}
        first = get(f"Shop/Collections?Curator_username={SHOPMY_USERNAME}&limit=100") or {}
        sections = first.get("sections") or []
        lists = [first] + [
            get(f"Shop/Collections?Curator_username={SHOPMY_USERNAME}&Section_id={s['id']}&limit=100") or {}
            for s in sections if isinstance(s, dict) and s.get("id")
        ]
        for data in lists:
            for c in data.get("collections") or []:
                if isinstance(c, dict) and c.get("id") and not c.get("private") and not c.get("isArchived"):
                    collections[c["id"]] = c
        print(f"Found {len(collections)} collections: "
              + ", ".join(str(c.get("name")) for c in collections.values()))

        # 2. Read the items in each collection
        found = {}
        for cid, c in collections.items():
            name = c.get("name") or str(cid)
            before = len(found)
            detail = get(f"Collections/{cid}?limit=500")
            if detail:
                pins_in(detail, found, name)
            pins_in(c, found, name)  # the preview items, just in case
            print(f"  {name}: {len(found) - before} items")
            if debug and detail and cid == next(iter(collections)):
                print(f"[debug] collection keys: {list(detail.keys())}")
        browser.close()

    return list(found.values())


FETCH_JS = """
async (url) => {
  try {
    const r = await fetch(url, {credentials: 'include'});
    let j = null; try { j = await r.json(); } catch (e) {}
    return {status: r.status, json: j};
  } catch (e) { return {status: 'error ' + e}; }
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
    if not r.ok:
        sys.exit(f"Pinterest error {r.status_code}: {r.text[:300]}")
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

    products = fetch_shopmy_products(debug=args.debug)
    print(f"Found {len(products)} items in your ShopMy collections")
    if not products:
        sys.exit("No items found. Run the test again with --debug and send the output to Claude.")

    state = load_state()
    first_run = state is None
    state = state or {"pinned": {}}

    if first_run and not args.pin_existing:
        for p in products:
            state["pinned"][p["key"]] = {"title": p["title"], "pin_id": None, "seeded": True}
        if not args.dry_run:
            save_state(state)
        print("First run: recorded existing items without pinning. New ones will be pinned from now on.")
        return

    new = [p for p in products if p["key"] not in state["pinned"]]
    print(f"{len(new)} new item(s)")
    if not new:
        return

    if args.dry_run:
        for p in new:
            print(f"Would pin: {p['title']} -> {p['link']}   [{p['collection']}]")
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
