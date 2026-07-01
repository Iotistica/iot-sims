#!/usr/bin/env python3
"""
Scrapes the BACnet International BTL product database and outputs
bacnet-vendors.json to bacnet-simulator/.

Strategy:
  1. Intercept the AJAX/fetch request the page makes when filtering
  2. If that fails, iterate through manufacturer options via the UI
  3. If that fails, fall back to parsing any visible text

Usage:
    pip install playwright beautifulsoup4 requests
    playwright install chromium
    python scrape-btl.py [--output path/to/bacnet-vendors.json] [--debug]
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium", file=sys.stderr)
    sys.exit(1)

BTL_URL = "https://www.bacnetinternational.net/btl/?s=product_listing&filter_product_listing=1"

PROFILE_LABELS = {
    "B-AWS":   "Workstation",
    "B-OWS":   "Operator Workstation",
    "B-OD":    "Operator Display",
    "B-LOD":   "Lightweight Operator Display",
    "B-BC":    "Building Controller",
    "B-AAC":   "Advanced Application Controller",
    "B-ASC":   "Application Specific Controller",
    "B-LSC":   "Lighting Application Specific Controller",
    "B-SS":    "Smart Sensor",
    "B-SA":    "Smart Actuator",
    "B-RTR":   "Router",
    "B-GW":    "Gateway",
    "B-BBMD":  "BBMD",
    "B-LD":    "Lighting Director",
    "B-GEN":   "Generic",
    "B-SCHUB": "SC Hub",
}


def scrape(headless: bool = True, debug: bool = False) -> dict:
    vendors: dict[str, list[dict]] = {}
    captured_requests: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # --- Intercept all network requests to find the data endpoint ---
        def on_request(request):
            url = request.url
            # Only capture interesting requests (skip images, css, fonts)
            if any(ext in url for ext in ['.png', '.jpg', '.css', '.woff', '.gif', '.ico']):
                return
            captured_requests.append({
                "url": url,
                "method": request.method,
                "post_data": request.post_data,
            })

        def on_response(response):
            url = response.url
            ct = response.headers.get("content-type", "")
            # Capture JSON responses that look like product data
            if "json" in ct and any(kw in url.lower() for kw in ["product", "listing", "btl", "ajax", "filter"]):
                try:
                    body = response.body()
                    print(f"  [API] JSON response from: {url}")
                    if debug:
                        print(f"       Preview: {body[:500]}")
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        print(f"Loading: {BTL_URL}")
        try:
            page.goto(BTL_URL, timeout=60_000, wait_until="networkidle")
        except PlaywrightTimeout:
            print("WARNING: networkidle timeout — continuing anyway")
            page.goto(BTL_URL, timeout=60_000, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)

        if debug:
            page.screenshot(path="btl-initial.png", full_page=True)
            with open("btl-initial.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            print("Saved btl-initial.png and btl-initial.html")

        # --- Dump all network requests seen ---
        print(f"\nNetwork requests captured: {len(captured_requests)}")
        ajax_urls = [r for r in captured_requests if "ajax" in r["url"].lower() or "admin-ajax" in r["url"].lower() or "filter" in r["url"].lower()]
        for r in ajax_urls[:10]:
            print(f"  {r['method']} {r['url']}")
            if r.get("post_data"):
                print(f"    POST: {r['post_data'][:200]}")

        # --- Strategy 1: Find all select elements and their options ---
        print("\nInspecting page selects...")
        selects_info = page.evaluate("""
            () => {
                const result = [];
                document.querySelectorAll('select').forEach(sel => {
                    result.push({
                        name: sel.name,
                        id: sel.id,
                        className: sel.className,
                        optionCount: sel.options.length,
                        firstOptions: Array.from(sel.options).slice(0, 5).map(o => ({value: o.value, text: o.text})),
                    });
                });
                return result;
            }
        """)
        for s in selects_info:
            print(f"  select name={s['name']!r} id={s['id']!r} class={s['className']!r} options={s['optionCount']}")
            for opt in s['firstOptions']:
                print(f"    option value={opt['value']!r} text={opt['text']!r}")

        # --- Find the manufacturer select ---
        mfr_select_info = None
        for s in selects_info:
            if s['optionCount'] > 50:  # manufacturer list has 235+ options
                mfr_select_info = s
                print(f"\nUsing select as manufacturer list: name={s['name']!r} id={s['id']!r}")
                break

        if not mfr_select_info:
            print("WARNING: Could not identify manufacturer select element")
        else:
            # Get all manufacturer options
            sel_selector = f"select[name='{mfr_select_info['name']}']" if mfr_select_info['name'] else f"select#{mfr_select_info['id']}" if mfr_select_info['id'] else "select"
            manufacturers = page.evaluate(f"""
                () => {{
                    const sel = document.querySelector({json.dumps(sel_selector)});
                    if (!sel) return [];
                    return Array.from(sel.options)
                        .map(o => ({{ value: o.value.trim(), text: o.text.trim() }}))
                        .filter(o => o.value && o.value !== '' && o.value !== '0');
                }}
            """)
            print(f"Found {len(manufacturers)} manufacturers")
            if manufacturers:
                print(f"First few: {[m['text'] for m in manufacturers[:5]]}")

            # --- Strategy 2: Interact with the select for each manufacturer ---
            for i, mfr in enumerate(manufacturers, 1):
                name = mfr['text'] or mfr['value']
                value = mfr['value']
                print(f"  [{i}/{len(manufacturers)}] {name}", end="", flush=True)

                try:
                    # Select the manufacturer and trigger change
                    page.select_option(sel_selector, value=value)
                    page.wait_for_timeout(2000)

                    # Look for any submit button and click it
                    submit = page.query_selector("input[type=submit], button[type=submit], .filter-submit, #filter-submit")
                    if submit:
                        submit.click()
                        page.wait_for_timeout(2000)

                    if debug and i <= 3:
                        page.screenshot(path=f"btl-mfr-{i}.png", full_page=True)

                    # Extract products from current page state
                    products = _extract_products(page)

                    if products:
                        print(f" → {len(products)} products")
                        for prod in products:
                            _add_product(vendors, name, prod.get("name", ""), prod.get("type", ""))
                    else:
                        # Last resort: scan page text
                        text = page.inner_text("body")
                        found = _parse_text_for_products(text, name)
                        if found:
                            print(f" → {len(found)} (text)")
                            for p in found:
                                _add_product(vendors, name, p["name"], p.get("type", ""))
                        else:
                            print(" → 0")

                    time.sleep(0.2)

                except PlaywrightTimeout:
                    print(" → TIMEOUT")
                except Exception as e:
                    print(f" → ERROR: {e}")

        # --- Strategy 3: Try iterating URL parameters directly ---
        if not vendors:
            print("\nFalling back to URL-based scraping...")
            _scrape_by_url(page, vendors, debug)

        if debug:
            page.screenshot(path="btl-final.png", full_page=True)

        browser.close()

    return _build_output(vendors)


def _extract_products(page) -> list[dict]:
    """Try multiple DOM strategies to find product entries on the current page."""
    return page.evaluate("""
        () => {
            const results = [];
            const profileRe = /\\b(B-[A-Z]+)\\b/;

            // Strategy: table rows
            document.querySelectorAll('table tr').forEach(row => {
                const cells = Array.from(row.querySelectorAll('td')).map(c => c.innerText.trim());
                if (cells.length >= 1 && cells[0].length > 2) {
                    const profileMatch = cells.join(' ').match(profileRe);
                    results.push({ name: cells[0], type: profileMatch ? profileMatch[1] : '' });
                }
            });
            if (results.length) return results;

            // Strategy: list items / divs with product-like classes
            const selectors = [
                '.product', '.product-item', '.listing-item', '.btl-product',
                '[class*="product-row"]', '[class*="listing-row"]',
                '.views-row', '.view-row', '.wpb_wrapper li',
                'article', '.entry', '.post',
            ];
            for (const sel of selectors) {
                const items = document.querySelectorAll(sel);
                if (items.length > 2) {
                    items.forEach(item => {
                        const text = item.innerText.trim();
                        if (text && text.length > 3 && text.length < 300) {
                            const profileMatch = text.match(profileRe);
                            results.push({ name: text.split('\\n')[0].trim(), type: profileMatch ? profileMatch[1] : '' });
                        }
                    });
                    if (results.length) return results;
                }
            }

            // Strategy: look for any element containing a BTL profile code near a product name
            document.querySelectorAll('*').forEach(el => {
                if (el.children.length === 0) {  // leaf nodes only
                    const text = el.innerText?.trim();
                    if (text && profileRe.test(text) && text.length < 200) {
                        results.push({ name: text.replace(profileRe, '').trim(), type: (text.match(profileRe) || [''])[1] });
                    }
                }
            });

            return results;
        }
    """)


def _parse_text_for_products(text: str, vendor: str) -> list[dict]:
    """Scan raw page text for product name + BTL profile code patterns."""
    results = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or len(line) < 4 or len(line) > 250:
            continue
        m = re.search(r'\b(B-[A-Z]+)\b', line)
        if m:
            profile = m.group(1)
            name = line[:m.start()].strip(" -|·\t")
            if name and len(name) > 3 and profile in PROFILE_LABELS:
                results.append({"name": name, "type": profile})
    return results


def _scrape_by_url(page, vendors: dict, debug: bool) -> None:
    """Fallback: iterate URL parameters for each manufacturer from dropdown."""
    # First, get manufacturer list from the page dropdown
    page.goto(BTL_URL, timeout=60_000, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    all_text = page.inner_text("body")

    # Try to get manufacturer select options via URL param approach
    for param_name in ["listing_manufacturer", "manufacturer", "mfr", "vendor"]:
        mfrs = page.evaluate(f"""
            () => {{
                const sel = document.querySelector('select[name="{param_name}"]');
                if (!sel) return null;
                return Array.from(sel.options)
                    .map(o => ({{ value: o.value.trim(), text: o.text.trim() }}))
                    .filter(o => o.value && o.value !== '' && o.value !== '0' && o.value !== 'all');
            }}
        """)
        if mfrs:
            print(f"  Found select[name='{param_name}'] with {len(mfrs)} options")
            for mfr in mfrs:
                url = f"{BTL_URL}&{param_name}={mfr['value'].replace(' ', '+')}"
                _fetch_manufacturer_page(page, url, mfr['text'], vendors)
            return

    print("  No manufacturer select found by name — dumping page structure for diagnosis")
    structure = page.evaluate("""
        () => ({
            title: document.title,
            forms: Array.from(document.forms).map(f => ({
                action: f.action, method: f.method,
                inputs: Array.from(f.elements).map(e => ({ tag: e.tagName, type: e.type, name: e.name, id: e.id }))
            })),
            selectCount: document.querySelectorAll('select').length,
            bodyPreview: document.body.innerText.slice(0, 500),
        })
    """)
    print(json.dumps(structure, indent=2))


def _fetch_manufacturer_page(page, url: str, mfr_name: str, vendors: dict) -> None:
    print(f"  {mfr_name}", end="", flush=True)
    try:
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        products = _extract_products(page)
        if products:
            print(f" → {len(products)}")
            for p in products:
                _add_product(vendors, mfr_name, p["name"], p.get("type", ""))
        else:
            print(" → 0")
    except Exception as e:
        print(f" → {e}")


def _add_product(vendors: dict, vendor: str, name: str, profile: str) -> None:
    if not name or len(name) < 3:
        return
    vendor = vendor.strip()
    if not vendor:
        return
    if vendor not in vendors:
        vendors[vendor] = []
    entry: dict = {"name": name}
    if profile and profile in PROFILE_LABELS:
        entry["type"] = profile
        entry["typeLabel"] = PROFILE_LABELS[profile]
    elif profile:
        entry["type"] = profile
    if not any(p["name"] == name for p in vendors[vendor]):
        vendors[vendor].append(entry)


def _build_output(vendors: dict) -> dict:
    vendor_list = [
        {"name": name, "models": sorted(models, key=lambda m: m["name"])}
        for name, models in sorted(vendors.items())
        if name and models
    ]
    return {
        "updated": str(date.today()),
        "source": "BACnet International BTL Database",
        "url": "https://www.bacnetinternational.net/btl/",
        "vendors": vendor_list,
    }


def main():
    parser = argparse.ArgumentParser(description="Scrape BTL BACnet vendor/product database")
    parser.add_argument("--output", default=None)
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--debug", action="store_true", help="Save screenshots and HTML for diagnosis")
    args = parser.parse_args()

    if args.output:
        out_path = Path(args.output)
    else:
        script_dir = Path(__file__).parent
        out_path = script_dir.parent / "bacnet-vendors.json"

    print(f"Output: {out_path}")
    data = scrape(headless=not args.no_headless, debug=args.debug)

    vendor_count = len(data["vendors"])
    product_count = sum(len(v["models"]) for v in data["vendors"])
    print(f"\nScraped {vendor_count} vendors, {product_count} products")

    if vendor_count == 0:
        print("WARNING: No vendors scraped — BTL page structure may have changed.", file=sys.stderr)
        print("Preserving existing bacnet-vendors.json.", file=sys.stderr)
        sys.exit(1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
