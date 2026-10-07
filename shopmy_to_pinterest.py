"""
ShopMy -> Pinterest auto-pinner  (version 8)

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
import re
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
# Easiest option: a Make.com webhook that creates the pin for you (see README)
MAKE_WEBHOOK_URL = os.environ.get("MAKE_WEBHOOK_URL", "").strip()

MAX_PINS_PER_RUN = int(os.environ.get("MAX_PINS_PER_RUN") or "5")
DESCRIPTION_TEMPLATE = os.environ.get("DESCRIPTION_TEMPLATE") or (
    "{title}\n\nShop it through my ShopMy link. #affiliate"
)

STATE_FILE = Path(os.environ.get("STATE_FILE") or "pinned.json")
PINTEREST_API = "https://api.pinterest.com/v5"
SHOPMY_API = "https://apiv3.shopmy.us/api"
TEST_SAMPLE_KEY = "pin-90373467"  # the Wilfred sweater, already pinned during setup


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
                nice = " ".join(nice.split())  # flatten line breaks and extra spaces
                key = f"pin-{pid}"
                found.setdefault(key, {
                    "key": key,
                    "title": nice[:100],
                    "image": image,
                    "link": f"https://go.shopmy.us/p-{pid}",
                    "collection": collection_name,
                    "product_id": data.get("Product_id"),
                    "category": data.get("Product_category") or "",
                })
        for k, v in data.items():
            pins_in(v, found, collection_name, under_pin_list="pin" in k.lower())
    elif isinstance(data, list):
        for v in data:
            pins_in(v, found, collection_name, under_pin_list)


def fetch_shopmy_products(debug=False):
    shop = f"https://shopmy.us/shop/{SHOPMY_USERNAME}"
    print(f"Version 9: checking {shop}")

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

        # 3. Look up each product's ShopMy department (Footwear, Haircare, Makeup...)
        departments = {}
        for n in range(10):
            data = get(f"Shop/products?Curator_username={SHOPMY_USERNAME}&page={n}&limit=100") or {}
            added = 0
            for r in data.get("results") or []:
                pid = r.get("Product_id") or r.get("id")
                if pid and pid not in departments:
                    departments[pid] = (r.get("Department_name") or "", r.get("Category_name") or "")
                    added += 1
            if not added:
                break
        browser.close()

    items = list(found.values())
    for it in items:
        dept, cat = departments.get(it.get("product_id"), ("", ""))
        it["department"] = dept
        it["category"] = it.get("category") or cat
        it["board"] = choose_board(it)
        it["board_id"] = BOARDS[it["board"]]
    counts = {}
    for it in items:
        counts[it["board"]] = counts.get(it["board"], 0) + 1
    print("Boards: " + ", ".join(f"{b}: {n}" for b, n in counts.items()))
    return items


# ---------------------------------------------------------------- which board
BOARDS = {
    "All my Favs": "1138003468282937193",
    "Shoes!!": "1138003468282937317",
    "Activewear & Accessories": "1138003468282937316",
    "Makeup & Skincare": "1138003468282937314",
    "Hair Favs": "1138003468282937313",
}
DEFAULT_BOARD = "All my Favs"

# ShopMy department -> board
DEPARTMENT_BOARDS = {
    "footwear": "Shoes!!",
    "shoes": "Shoes!!",
    "haircare": "Hair Favs",
    "hair care": "Hair Favs",
    "hair tools": "Hair Favs",
    "makeup": "Makeup & Skincare",
    "skincare": "Makeup & Skincare",
    "fragrance": "Makeup & Skincare",
    "bath & body": "Makeup & Skincare",
    "nails": "Makeup & Skincare",
    "beauty": "Makeup & Skincare",
    "activewear": "Activewear & Accessories",
    "fitness equipment": "Activewear & Accessories",
    "bags & purses": "Activewear & Accessories",
    "jewelry": "Activewear & Accessories",
    "accessories": "Activewear & Accessories",
}

CLOTHING_DEPARTMENTS = {"apparel", "coats & outerwear", "swimwear", "sleep & loungewear",
                        "dresses", "tops", "bottoms", "denim", "intimates", "clothing"}

# Words in the product's category or name -> board (checked in this order)
KEYWORD_BOARDS = [
    ("Shoes!!", r"shoes?|boots?|booties?|sneakers?|heels?|sandals?|loafers?|flats|slippers?|mules?|clogs?|pumps"),
    ("Hair Favs", r"hair|shampoo|conditioner|texturi[sz]ing|volumi[sz]er|dry shampoo|curl\w*|blowout|scalp"),
    ("Makeup & Skincare", r"makeup|mascara|lash\w*|eyelash|lip\w*|blush|bronzer|foundation|concealer|"
                          r"highlighter|eyeliner|eyeshadow|brow|serum|moisturi[sz]er|cleanser|sunscreen|spf|"
                          r"skincare|skin care|toner|perfume|fragrance|nail\w*|cream"),
    ("Activewear & Accessories", r"bags?|purses?|totes?|clutch|backpack|jewelry|necklaces?|earrings?|bracelets?|"
                                 r"sunglasses|hats?|caps?|belts?|scarf|scarves|wallet|watch|"
                                 r"leggings?|sports bra|workout|activewear|athletic|yoga|gym"),
]

# Collection names that hint at a board, used when the product itself isn't clear
COLLECTION_BOARDS = [
    ("Hair Favs", r"hair"),
    ("Makeup & Skincare", r"makeup|skin|beauty"),
    ("Activewear & Accessories", r"workout|wellness|active|gym"),
    ("Shoes!!", r"shoe"),
]


def _match(rules, text):
    for board, pattern in rules:
        if re.search(rf"\b(?:{pattern})\b", text, re.I):
            return board
    return None


def choose_board(item):
    dept = (item.get("department") or "").strip().lower()
    if dept in DEPARTMENT_BOARDS:
        return DEPARTMENT_BOARDS[dept]
    if dept in CLOTHING_DEPARTMENTS:  # clothes go to the main board unless the category says otherwise
        hit = _match(KEYWORD_BOARDS, item.get("category") or "")
        return hit or DEFAULT_BOARD
    return (_match(KEYWORD_BOARDS, item.get("category") or "")
            or _match(COLLECTION_BOARDS, item.get("collection") or "")
            or _match(KEYWORD_BOARDS, re.split(r"\s+in\s+", item.get("title") or "")[0])  # skip "in Cream" etc.
            or DEFAULT_BOARD)


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


def pin_fields(product):
    return {
        "title": product["title"],
        "description": DESCRIPTION_TEMPLATE.format(title=product["title"])[:500],
        "link": product["link"],
        "image": product["image"],
        "alt_text": product["title"][:500],
        "collection": product.get("collection", ""),
        "board": product.get("board", DEFAULT_BOARD),
        "board_id": product.get("board_id", BOARDS[DEFAULT_BOARD]),
    }


def send_to_make(product):
    r = requests.post(MAKE_WEBHOOK_URL, json=pin_fields(product), timeout=60)
    if not r.ok:
        raise RuntimeError(f"Make error {r.status_code}: {r.text[:300]}")
    return "sent to Make"


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
    ap.add_argument("--start-backlog", action="store_true",
                    help="also pin items that were already in your collections, a few per run")
    ap.add_argument("--send-sample", action="store_true",
                    help="send one item to Make so it can learn the fields (nothing is recorded)")
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

    if args.send_sample:
        if not MAKE_WEBHOOK_URL:
            sys.exit("Add the MAKE_WEBHOOK_URL secret first.")
        print(f"Sending sample to Make: {products[0]['title']}")
        send_to_make(products[0])
        print("Sent. Go back to Make; it should say it determined the data structure.")
        return

    state = load_state()
    first_run = state is None
    state = state or {"pinned": {}}

    if args.start_backlog:
        # Remember everything currently there, then switch on backlog mode so
        # every run pins a few of the older items as well as anything new.
        for p in products:
            state["pinned"].setdefault(p["key"], {"title": p["title"], "pin_id": None, "seeded": True})
        # The test sample was already pinned by hand, so don't pin it twice
        if TEST_SAMPLE_KEY in state["pinned"] and state["pinned"][TEST_SAMPLE_KEY].get("seeded"):
            state["pinned"][TEST_SAMPLE_KEY] = {"title": "test sample", "pin_id": "sent as sample"}
        state["backlog"] = True
        save_state(state)
        waiting = sum(1 for v in state["pinned"].values() if v.get("seeded"))
        print(f"Backlog mode on: {waiting} existing items will be pinned, {MAX_PINS_PER_RUN} per run.")
        first_run = False

    if first_run and not args.pin_existing:
        for p in products:
            state["pinned"][p["key"]] = {"title": p["title"], "pin_id": None, "seeded": True}
        if not args.dry_run:
            save_state(state)
        print("First run: recorded existing items without pinning. New ones will be pinned from now on.")
        return

    new = [p for p in products if p["key"] not in state["pinned"]]
    if state.get("backlog"):
        # Older items go after anything brand new
        new += [p for p in products if state["pinned"].get(p["key"], {}).get("seeded")]
    print(f"{len(new)} item(s) waiting to be pinned")
    if not new:
        return

    if args.dry_run:
        for p in new:
            print(f"Would pin: {p['title']} -> {p['board']}   ({p['link']})")
        return

    if MAKE_WEBHOOK_URL:
        post = send_to_make
    elif PINTEREST_ACCESS_TOKEN and PINTEREST_BOARD_ID:
        token = refresh_access_token()
        post = lambda prod: create_pin(token, prod)
    else:
        print("Pinterest isn't connected yet (add the MAKE_WEBHOOK_URL secret), so nothing was pinned.")
        return

    for p in new[:MAX_PINS_PER_RUN]:
        try:
            pin_id = post(p)
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
