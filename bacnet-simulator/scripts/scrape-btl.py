#!/usr/bin/env python3
"""
Scrapes the BACnet International BTL product database.

What we learned from the first debug run:
- Manufacturer select: name='manu', values are numeric IDs (e.g. 345 = '75F')
- Profile select: name='profile', values are numeric IDs
- Correct filter URL: ?s=product_listing&filter_product_listing=1&manu=<id>
- Only 17 network requests on load — no separate AJAX data call visible,
  so products are either server-rendered or embedded in a JS data blob

Strategy:
  1. Load base page, extract manu select options (name → numeric id)
  2. For each manufacturer, GET ?...&manu=<id> and extract products
  3. Look for products in: DOM tables, class-based elements, JS script blobs
  4. Fall back to full-page text scan for BTL profile codes

Usage:
    pip install playwright
    playwright install chromium
    python scrape-btl.py [--output path/to/bacnet-vendors.json] [--debug] [--workers N]
"""

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("ERROR: pip install playwright && playwright install chromium", file=sys.stderr)
    sys.exit(1)

BASE_URL = "https://www.bacnetinternational.net/btl/?s=product_listing&filter_product_listing=1"

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


def scrape(headless: bool = True, debug: bool = False) -> dict:
    vendors: dict[str, list[dict]] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )

        # ── Step 1: load base page, get manufacturer list ──────────────────────
        page = ctx.new_page()
        print(f"Loading base page…")
        page.goto(BASE_URL, timeout=60_000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        if debug:
            page.screenshot(path="btl-base.png", full_page=True)
            Path("btl-base.html").write_text(page.content(), encoding="utf-8")
            print("Saved btl-base.png + btl-base.html")

        manufacturers: list[dict] = page.evaluate("""
            () => {
                const sel = document.querySelector('select[name="manu"]');
                if (!sel) return [];
                return Array.from(sel.options)
                    .map(o => ({ id: o.value.trim(), name: o.text.trim() }))
                    .filter(o => o.id && o.id !== '' && o.name !== 'Filter by Manufacturer');
            }
        """)
        print(f"Manufacturers found: {len(manufacturers)}")
        if not manufacturers:
            print("ERROR: Could not read manu select — page structure changed", file=sys.stderr)
            # print page selects for diagnosis
            selects = page.evaluate("""
                () => Array.from(document.querySelectorAll('select')).map(s => ({
                    name: s.name, id: s.id, options: s.options.length,
                    sample: Array.from(s.options).slice(0,3).map(o => o.value + '=' + o.text)
                }))
            """)
            print(json.dumps(selects, indent=2))
            sys.exit(1)

        # ── Step 2: scrape each manufacturer page ──────────────────────────────
        first_product_logged = False  # log the first extracted product for diagnosis

        for i, mfr in enumerate(manufacturers, 1):
            mfr_name = mfr["name"]
            mfr_id   = mfr["id"]
            url = f"{BASE_URL}&manu={mfr_id}"

            print(f"  [{i}/{len(manufacturers)}] {mfr_name} (id={mfr_id})", end="", flush=True)

            try:
                page.goto(url, timeout=45_000, wait_until="domcontentloaded")
                # Wait up to 8s for any product element to appear before scraping
                try:
                    page.wait_for_selector(
                        "table tr td, .product, .listing-item, [class*='product'], [class*='listing']",
                        timeout=8_000
                    )
                except PlaywrightTimeout:
                    pass  # fall through to extraction anyway
                page.wait_for_timeout(1000)

                if debug and i <= 5:
                    page.screenshot(path=f"btl-mfr-{i}.png", full_page=True)
                    Path(f"btl-mfr-{i}.html").write_text(page.content(), encoding="utf-8")

                result = _extract_products_from_page(page)
                products  = result["products"]
                strategy  = result["strategy"]

                # Log the first extraction in detail to verify correctness
                if not first_product_logged and products:
                    first_product_logged = True
                    print(f"\n  [DIAGNOSIS] Strategy '{strategy}' matched on {mfr_name}:")
                    for p in products[:5]:
                        print(f"    {p}")

                if products:
                    print(f" → {len(products)} [{strategy}]")
                    for prod in products:
                        _add(vendors, mfr_name, prod["name"], prod.get("type", ""))
                else:
                    dbg = result.get("debug", {})
                    print(f" → 0 [tables={dbg.get('tables',0)} rows={dbg.get('tableRows',0)}]")
                    if i <= 3:  # print body preview for first few misses
                        print(f"    BODY: {str(dbg.get('bodyPreview',''))[:300]}")

                time.sleep(0.3)

            except PlaywrightTimeout:
                print(" → TIMEOUT")
            except Exception as e:
                print(f" → ERROR: {e}")

        browser.close()

    return _build(vendors)


def _extract_products_from_page(page) -> dict:
    """
    Try several strategies to find product rows on the current page.
    Returns { products: [{name, type}], strategy: str }.
    """
    return page.evaluate(f"""
        () => {{
            const profileRe = /{PROFILE_RE.pattern}/;

            // ── Strategy 1: HTML table rows (2+ columns) ───────────────────────
            const tableRows = [];
            document.querySelectorAll('table tr').forEach(row => {{
                const cells = Array.from(row.querySelectorAll('td')).map(c => c.innerText.trim());
                if (cells.length >= 2 && cells[0].length > 2 && !/^(product|model|name|type|manufacturer)$/i.test(cells[0])) {{
                    const text = cells.join(' ');
                    const m = text.match(profileRe);
                    tableRows.push({{ name: cells[0], type: m ? m[1] : '' }});
                }}
            }});
            if (tableRows.length > 0) return {{ products: tableRows, strategy: 'table-rows' }};

            // ── Strategy 2: single-column table (name only) ────────────────────
            const singleRows = [];
            document.querySelectorAll('table tr td:first-child').forEach(td => {{
                const text = td.innerText.trim();
                if (text.length > 4 && text.length < 200 && !/^(product|model|name|filter)$/i.test(text)) {{
                    const m = td.closest('tr')?.innerText.match(profileRe);
                    singleRows.push({{ name: text, type: m ? m[1] : '' }});
                }}
            }});
            if (singleRows.length > 0) return {{ products: singleRows, strategy: 'single-col-table' }};

            // ── Strategy 3: common CMS list / card classes ─────────────────────
            const candidates = [
                '.product-listing-item', '.btl-listing-item', '.btl-product',
                '.listing-item', '.product-item', '.product-row',
                '.views-row', '.view-row', 'li.product',
                '[class*="listing-item"]', '[class*="product-row"]',
            ];
            for (const sel of candidates) {{
                const items = document.querySelectorAll(sel);
                if (items.length >= 1) {{
                    const found = [];
                    items.forEach(el => {{
                        const text = el.innerText.trim();
                        if (text && text.length > 3 && text.length < 500) {{
                            const m = text.match(profileRe);
                            found.push({{ name: text.split('\\n')[0].trim(), type: m ? m[1] : '' }});
                        }}
                    }});
                    if (found.length) return {{ products: found, strategy: 'css:' + sel }};
                }}
            }}

            // ── Strategy 4: JavaScript data blobs in <script> tags ─────────────
            for (const s of document.querySelectorAll('script:not([src])')) {{
                const src = s.textContent || '';
                const arrayMatch = src.match(/(?:var |let |const |window\\.\\w+\\s*=\\s*)(\[\\{{[\\s\\S]*?\\}}\\])/);
                if (arrayMatch) {{
                    try {{
                        const data = JSON.parse(arrayMatch[1]);
                        if (Array.isArray(data) && data.length >= 1) {{
                            const found = data.map(item => ({{
                                name: String(item.name || item.model || item.product || item.title || '').trim(),
                                type: String(item.profile || item.type || item.category || '').trim(),
                            }})).filter(x => x.name.length > 2);
                            if (found.length) return {{ products: found, strategy: 'js-blob' }};
                        }}
                    }} catch(e) {{}}
                }}
            }}

            // ── Strategy 5: scan all leaf text nodes for profile code patterns ──
            const textNodes = [];
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {{
                const text = node.textContent.trim();
                if (text.length > 3 && text.length < 250) {{
                    const m = text.match(profileRe);
                    if (m) {{
                        const name = text.replace(profileRe, '').trim().replace(/^[-|·\\s]+|[-|·\\s]+$/g, '');
                        if (name && name.length > 3) textNodes.push({{ name, type: m[1] }});
                    }}
                }}
            }}
            if (textNodes.length) return {{ products: textNodes, strategy: 'text-nodes' }};

            // ── Strategy 6: dump page structure for diagnosis ──────────────────
            return {{
                products: [],
                strategy: 'none',
                debug: {{
                    tables: document.querySelectorAll('table').length,
                    tableRows: document.querySelectorAll('table tr').length,
                    bodyPreview: document.body.innerText.slice(0, 800),
                }}
            }};
        }}
    """)


def _add(vendors: dict, vendor: str, name: str, profile: str) -> None:
    name   = name.strip()
    vendor = vendor.strip()
    if not name or len(name) < 3 or not vendor:
        return
    if vendor not in vendors:
        vendors[vendor] = []
    entry: dict = {"name": name}
    if profile in PROFILE_LABELS:
        entry["type"] = profile
        entry["typeLabel"] = PROFILE_LABELS[profile]
    elif profile:
        entry["type"] = profile
    if not any(p["name"] == name for p in vendors[vendor]):
        vendors[vendor].append(entry)


def _build(vendors: dict) -> dict:
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
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--debug", action="store_true", help="Save screenshots/HTML for first 3 manufacturers")
    args = parser.parse_args()

    out_path = Path(args.output) if args.output else Path(__file__).parent.parent / "bacnet-vendors.json"
    print(f"Output: {out_path}")

    data = scrape(headless=not args.no_headless, debug=args.debug)

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
