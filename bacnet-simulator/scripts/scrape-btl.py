#!/usr/bin/env python3
"""
Scrapes the BACnet International BTL product database.

Key findings from debug run:
- Manufacturer list: select[name='manu'] on base page (server-rendered)
- jQuery reveals real filter URL: index.php?m=<numeric_id>
- Product pages (index.php?m=<id>) are server-rendered — no JS/Playwright needed
- Structure: <h4> headings name the profile type (B-BC, B-AAC, etc.)
             <table> rows below each h4 contain the product names

Usage:
    pip install requests beautifulsoup4
    python scrape-btl.py [--output path/to/bacnet-vendors.json]
"""

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: pip install requests beautifulsoup4", file=sys.stderr)
    sys.exit(1)

BASE_URL  = "https://www.bacnetinternational.net/btl/"
MFR_URL   = "https://www.bacnetinternational.net/btl/index.php?m={id}"

PROFILE_LABELS = {
    "B-AWS": "Workstation", "B-OWS": "Operator Workstation",
    "B-OD": "Operator Display", "B-LOD": "Lightweight Operator Display",
    "B-BC": "Building Controller", "B-AAC": "Advanced Application Controller",
    "B-ASC": "Application Specific Controller",
    "B-LSC": "Lighting Application Specific Controller",
    "B-SS": "Smart Sensor", "B-SA": "Smart Actuator",
    "B-RTR": "Router", "B-GW": "Gateway", "B-BBMD": "BBMD",
    "B-LD": "Lighting Director", "B-GEN": "Generic", "B-SCHUB": "SC Hub",
}

PROFILE_RE = re.compile(r'\b(B-(?:AWS|OWS|OD|LOD|XAWS|ALSWS|AACWS|BC|AAC|ASC|LSC|LD|SS|SA|RTR|GW|BBMD|GEN|SCHUB))\b')

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
})


def get_manufacturers() -> list[dict]:
    resp = SESSION.get(BASE_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    sel = soup.find("select", {"name": "manu"})
    if not sel:
        raise RuntimeError("Could not find select[name='manu'] on base page")
    return [
        {"id": opt["value"].strip(), "name": opt.text.strip()}
        for opt in sel.find_all("option")
        if opt.get("value", "").strip() and opt.text.strip() != "Filter by Manufacturer"
    ]


def get_products(mfr_id: str) -> list[dict]:
    """
    Fetch index.php?m=<id> and extract all products.
    Page structure: <h4>...profile label (B-XX)...</h4> followed by <table> with product rows.
    """
    url = MFR_URL.format(id=mfr_id)
    resp = SESSION.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    products = []
    current_profile = ""

    # Walk the DOM in order — h4 headings set the current profile, tables collect products
    content = soup.find("div", id="content") or soup.find("main") or soup.body
    if not content:
        return products

    for elem in content.find_all(["h4", "h3", "h2", "table"]):
        tag = elem.name

        if tag in ("h4", "h3", "h2"):
            m = PROFILE_RE.search(elem.get_text())
            if m:
                current_profile = m.group(1)

        elif tag == "table":
            for row in elem.find_all("tr"):
                cells = row.find_all("td")
                if not cells:
                    continue
                name = cells[0].get_text(separator=" ", strip=True)
                # Skip empty, header-like, or obviously non-product rows
                if not name or len(name) < 3 or re.match(r'^(product|model|name|type|version|n/a)$', name, re.I):
                    continue
                entry: dict = {"name": name}
                if current_profile in PROFILE_LABELS:
                    entry["type"] = current_profile
                    entry["typeLabel"] = PROFILE_LABELS[current_profile]
                elif current_profile:
                    entry["type"] = current_profile
                # Also check the row text for a profile code if heading didn't set one
                if not current_profile:
                    row_text = row.get_text()
                    pm = PROFILE_RE.search(row_text)
                    if pm:
                        p = pm.group(1)
                        entry["type"] = p
                        if p in PROFILE_LABELS:
                            entry["typeLabel"] = PROFILE_LABELS[p]
                products.append(entry)

    return products


def scrape() -> dict:
    vendors: dict[str, list[dict]] = {}

    print("Fetching manufacturer list…")
    manufacturers = get_manufacturers()
    print(f"Found {len(manufacturers)} manufacturers")

    for i, mfr in enumerate(manufacturers, 1):
        name = mfr["name"]
        mid  = mfr["id"]
        print(f"  [{i}/{len(manufacturers)}] {name}", end="", flush=True)
        try:
            products = get_products(mid)
            # Deduplicate by name within this manufacturer
            seen: set[str] = set()
            unique = []
            for p in products:
                if p["name"] not in seen:
                    seen.add(p["name"])
                    unique.append(p)
            if unique:
                vendors[name] = unique
                print(f" → {len(unique)} products")
            else:
                print(" → 0")
            time.sleep(0.25)  # polite rate limiting
        except Exception as e:
            print(f" → ERROR: {e}")

    return {
        "updated": str(date.today()),
        "source": "BACnet International BTL Database",
        "url": "https://www.bacnetinternational.net/btl/",
        "vendors": [
            {"name": n, "models": sorted(m, key=lambda x: x["name"])}
            for n, m in sorted(vendors.items()) if m
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    out_path = Path(args.output) if args.output else Path(__file__).parent.parent / "bacnet-vendors.json"
    print(f"Output: {out_path}")

    data = scrape()
    vendor_count  = len(data["vendors"])
    product_count = sum(len(v["models"]) for v in data["vendors"])
    print(f"\nResult: {vendor_count} vendors, {product_count} products")

    if vendor_count == 0:
        print("WARNING: 0 vendors — preserving existing file.", file=sys.stderr)
        sys.exit(1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
