#!/usr/bin/env python3
"""
Price and availability monitor.

Checks the listing pages named in config.yaml, compares the result against the
previous run, and sends one email only when something actually changed.

Run:
    python monitor.py                 # normal run, uses config.yaml
    python monitor.py --config x.yaml # different configuration
    python monitor.py --dry-run       # do everything, print the email, send nothing
    python monitor.py --local DIR     # read saved HTML files from DIR instead of the web

Exit codes:
    0  ran fine (whether or not anything changed)
    1  configuration or input error
    2  the run produced no items - the site probably changed or blocked us
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

# Plain, honest identification. No browser masquerade.
USER_AGENT = "price-monitor/1.0 (small personal project)"
REQUEST_TIMEOUT = 30
POLITE_DELAY_SECONDS = 1.5
MAX_RETRIES = 3            # network errors and 5xx only; a 4xx is never retried


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------

@dataclass
class Item:
    key: str
    title: str
    price: str | None          # kept as string so JSON stays exact
    in_stock: bool | None
    url: str | None

    def price_decimal(self) -> Decimal | None:
        if self.price is None:
            return None
        try:
            return Decimal(self.price)
        except InvalidOperation:
            return None


@dataclass
class Change:
    kind: str                  # price | stock | new | removed
    item: Item
    old: str | None = None
    new: str | None = None


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        die(f"Configuration file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    required = ["site_name", "start_url", "selectors"]
    missing = [k for k in required if k not in cfg]
    if missing:
        die("Missing in config.yaml: " + ", ".join(missing))

    sel = cfg["selectors"]
    for k in ("item", "title"):
        if k not in sel:
            die(f"Missing selector '{k}' in config.yaml")

    cfg.setdefault("max_pages", 1)
    cfg.setdefault("alerts", {})
    cfg["alerts"].setdefault("price_change_min_percent", 0.0)
    cfg["alerts"].setdefault("notify_new_items", True)
    cfg["alerts"].setdefault("notify_removed_items", True)
    cfg.setdefault("email", {})
    return cfg


def die(message: str, code: int = 1):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def page_urls(cfg: dict) -> list[str]:
    """Expand {page} in start_url, or return the single URL."""
    start = cfg["start_url"]
    n = int(cfg.get("max_pages", 1))
    if "{page}" in start:
        return [start.replace("{page}", str(i)) for i in range(1, n + 1)]
    return [start]


def fetch(url: str) -> str:
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "de,en;q=0.8"},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.text
        except Exception as exc:                     # noqa: BLE001
            last = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500:
                # The shop said no (403 block, 404 moved, 429 too fast).
                # Asking again would not be polite, so don't.
                break
            if attempt < MAX_RETRIES:
                wait = attempt * 4
                print(f"  attempt {attempt} failed ({exc}); retrying in {wait}s")
                time.sleep(wait)
    print(f"  giving up on {url}: {last}", file=sys.stderr)
    return ""


def read_local(directory: Path) -> list[str]:
    files = sorted(p for p in directory.iterdir() if p.suffix in (".html", ".htm"))
    if not files:
        die(f"No .html files in {directory}")
    return [p.read_text(encoding="utf-8") for p in files]


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def pick(node, spec: str | None):
    """
    Resolve a selector spec against a node.
    "p.price"        -> text of the first match
    "h3 a@title"     -> value of the title attribute of the first match
    None             -> None
    """
    if not spec:
        return None
    if "@" in spec:
        css, attr = spec.rsplit("@", 1)
        found = node.select_one(css.strip())
        if found is None:
            return None
        return found.get(attr.strip())
    found = node.select_one(spec.strip())
    if found is None:
        return None
    return " ".join(found.get_text(" ", strip=True).split())


# A run of digits that may contain grouping or decimal separators.
PRICE_RE = re.compile(r"\d[\d., \s]*\d|\d")


def parse_price(raw: str | None) -> str | None:
    """
    Turn '£51.77', '1.299,00 EUR', '$1,299.00', '€ 24,50', '1 299,00 €'
    into a plain decimal string. Returns None when there is no number
    (e.g. 'on request').

    The rules, in order:
      both . and ,  -> whichever comes last is the decimal separator
      one separator -> three digits after it means grouping (1.299 = 1299),
                       one or two digits mean decimals (24,50 = 24.50)
    """
    if not raw:
        return None
    match = PRICE_RE.search(raw)
    if not match:
        return None

    text = match.group(0).replace(" ", "").replace(" ", "").strip(".,")
    if not text:
        return None

    has_dot, has_comma = "." in text, "," in text

    if has_dot and has_comma:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_dot or has_comma:
        separator = "," if has_comma else "."
        tail = text.rsplit(separator, 1)[1]
        if len(tail) == 3:
            text = text.replace(separator, "")           # grouping
        elif len(tail) in (1, 2):
            text = text.replace(".", "").replace(",", "")
            text = text[:-len(tail)] + "." + text[-len(tail):]
        else:
            text = text.replace(separator, "")

    try:
        return str(Decimal(text))
    except InvalidOperation:
        return None


OUT_WORDS = ("out of stock", "sold out", "unavailable", "not available",
             "no longer available", "nicht verfügbar", "nicht lieferbar", "ausverkauft",
             "nicht auf lager", "vergriffen")
IN_WORDS = ("in stock", "available", "auf lager", "verfügbar", "lieferbar", "sofort")


def parse_stock(raw: str | None) -> bool | None:
    if raw is None:
        return None
    text = raw.lower()
    for word in OUT_WORDS:
        if word in text:
            return False
    for word in IN_WORDS:
        if word in text:
            return True
    return None


def parse_items(html: str, cfg: dict) -> list[Item]:
    soup = BeautifulSoup(html, "html.parser")
    sel = cfg["selectors"]
    items: list[Item] = []
    for node in soup.select(sel["item"]):
        title = pick(node, sel.get("title"))
        if not title:
            continue
        url = pick(node, sel.get("link"))
        key = (url or title).strip()
        items.append(Item(
            key=key,
            title=title.strip(),
            price=parse_price(pick(node, sel.get("price"))),
            in_stock=parse_stock(pick(node, sel.get("availability"))),
            url=url,
        ))
    return items


# --------------------------------------------------------------------------
# comparison
# --------------------------------------------------------------------------

def compare(previous: dict[str, dict], current: list[Item], cfg: dict) -> list[Change]:
    threshold = Decimal(str(cfg["alerts"]["price_change_min_percent"]))
    changes: list[Change] = []
    seen: set[str] = set()

    for item in current:
        seen.add(item.key)
        before = previous.get(item.key)

        if before is None:
            if cfg["alerts"]["notify_new_items"]:
                changes.append(Change("new", item))
            continue

        old_price, new_price = before.get("price"), item.price
        if old_price != new_price and old_price is not None and new_price is not None:
            try:
                o, n = Decimal(old_price), Decimal(new_price)
                moved = abs(n - o) / o * 100 if o else Decimal(100)
                if moved >= threshold:
                    changes.append(Change("price", item, old_price, new_price))
            except (InvalidOperation, ZeroDivisionError):
                changes.append(Change("price", item, old_price, new_price))
        elif old_price != new_price:
            changes.append(Change("price", item, old_price, new_price))

        old_stock, new_stock = before.get("in_stock"), item.in_stock
        if old_stock != new_stock and new_stock is not None:
            changes.append(Change(
                "stock", item,
                stock_word(old_stock), stock_word(new_stock),
            ))

    if cfg["alerts"]["notify_removed_items"]:
        for key, before in previous.items():
            if key not in seen:
                changes.append(Change("removed", Item(
                    key=key,
                    title=before.get("title", key),
                    price=before.get("price"),
                    in_stock=before.get("in_stock"),
                    url=before.get("url"),
                )))

    order = {"price": 0, "stock": 1, "new": 2, "removed": 3}
    changes.sort(key=lambda c: (order.get(c.kind, 9), c.item.title.lower()))
    return changes


def stock_word(value) -> str:
    if value is True:
        return "in stock"
    if value is False:
        return "out of stock"
    return "unknown"


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def summarise(changes: list[Change]) -> str:
    counts: dict[str, int] = {}
    for change in changes:
        counts[change.kind] = counts.get(change.kind, 0) + 1
    words = {
        "price": ("price change", "price changes"),
        "stock": ("stock change", "stock changes"),
        "new": ("new listing", "new listings"),
        "removed": ("listing gone", "listings gone"),
    }
    parts = []
    for kind in ("price", "stock", "new", "removed"):
        n = counts.get(kind, 0)
        if n:
            singular, plural = words[kind]
            parts.append(f"{n} {singular if n == 1 else plural}")
    return " · ".join(parts) if parts else "no changes"


def render_text(cfg: dict, changes: list[Change], total: int, unchanged: int) -> str:
    lines = [
        f"{cfg['site_name']} — {summarise(changes)}",
        datetime.now(timezone.utc).strftime("checked %Y-%m-%d %H:%M UTC"),
        "",
    ]
    headings = {
        "price": "PRICE CHANGES",
        "stock": "STOCK CHANGES",
        "new": "NEW LISTINGS",
        "removed": "NO LONGER LISTED",
    }
    current_kind = None
    for change in changes:
        if change.kind != current_kind:
            current_kind = change.kind
            lines.append(headings[current_kind])
        if change.kind in ("price", "stock"):
            lines.append(f"  {change.item.title}")
            lines.append(f"      {change.old}  ->  {change.new}")
        elif change.kind == "new":
            price = change.item.price or "no price"
            lines.append(f"  {change.item.title}  ({price})")
        else:
            lines.append(f"  {change.item.title}")
        if change.item.url:
            lines.append(f"      {change.item.url}")
        lines.append("")

    lines.append(f"{total} listings checked. {unchanged} unchanged — not listed here.")
    return "\n".join(lines)


def render_failure(cfg: dict, pages: int) -> str:
    return (
        f"{cfg['site_name']} — the monitor found nothing\n"
        f"{datetime.now(timezone.utc).strftime('checked %Y-%m-%d %H:%M UTC')}\n\n"
        f"{pages} page(s) were requested and zero listings were parsed.\n\n"
        "This is almost always one of three things:\n"
        "  1. the shop changed its page layout, so the selectors no longer match\n"
        "  2. the shop started blocking automated requests\n"
        "  3. the pages moved to different addresses\n\n"
        "The previous data has been left untouched, so nothing is lost.\n"
        "You are getting this email instead of a silent 'no changes',\n"
        "which would have looked like good news.\n"
    )


# --------------------------------------------------------------------------
# email
# --------------------------------------------------------------------------

def env(name: str, default: str | None = None) -> str | None:
    """Environment variable, with an empty string counting as unset.
    GitHub Actions passes a secret that was never created as ''."""
    value = os.environ.get(name, "").strip()
    return value or default


def send_email(cfg: dict, subject: str, body: str) -> None:
    host = env("SMTP_HOST", "smtp.gmail.com")
    user = env("SMTP_USER")
    password = env("SMTP_PASSWORD")
    recipient = (cfg.get("email", {}).get("to") or "").strip() or env("MAIL_TO")

    if not (user and password and recipient):
        print("  SMTP_USER, SMTP_PASSWORD or recipient missing — not sending.")
        print("  (set them as repository secrets; see README)")
        return

    try:
        port = int(env("SMTP_PORT", "587"))
    except ValueError:
        print(f"  SMTP_PORT is not a number ({os.environ.get('SMTP_PORT')!r}) — not sending.")
        return

    message = EmailMessage()
    message["From"] = user
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(host, port, timeout=60) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(message)
    print(f"  email sent to {recipient}")


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def load_snapshot(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("items", {})
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  could not read snapshot ({exc}); treating as first run")
        return {}


def save_snapshot(path: Path, items: list[Item]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(items),
        "items": {item.key: asdict(item) for item in items},
    }
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, sort_keys=True)


def append_history(path: Path, changes: list[Change]) -> None:
    if not changes:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(["checked_at", "kind", "title", "old", "new", "url"])
        for change in changes:
            writer.writerow([
                stamp, change.kind, change.item.title,
                change.old or "", change.new or "", change.item.url or "",
            ])


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Price and availability monitor")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the email instead of sending it")
    parser.add_argument("--local", metavar="DIR",
                        help="read saved .html files from DIR instead of the web")
    parser.add_argument("--state", default="data/snapshot.json")
    parser.add_argument("--history", default="data/history.csv")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    snapshot_path = Path(args.state)
    history_path = Path(args.history)

    print(f"Monitor: {cfg['site_name']}")

    if args.local:
        pages = read_local(Path(args.local))
        print(f"  reading {len(pages)} local file(s) from {args.local}")
    else:
        urls = page_urls(cfg)
        pages = []
        for index, url in enumerate(urls, 1):
            print(f"  fetching page {index}/{len(urls)}")
            pages.append(fetch(url))
            if index < len(urls):
                time.sleep(POLITE_DELAY_SECONDS)

    items: list[Item] = []
    for html in pages:
        if html:
            items.extend(parse_items(html, cfg))

    # de-duplicate on key, first occurrence wins
    unique: dict[str, Item] = {}
    for item in items:
        unique.setdefault(item.key, item)
    items = list(unique.values())

    print(f"  parsed {len(items)} listing(s)")

    previous = load_snapshot(snapshot_path)

    if not items:
        subject = f"{cfg.get('email', {}).get('subject_prefix', '[Monitor]')} " \
                  f"{cfg['site_name']}: found nothing — needs a look"
        body = render_failure(cfg, len(pages))
        if args.dry_run:
            print("\n--- would send ---\n" + subject + "\n\n" + body)
        else:
            send_email(cfg, subject, body)
        return 2

    changes = compare(previous, items, cfg)
    unchanged = len(items) - len([c for c in changes if c.kind != "removed"])

    if previous and changes:
        subject = f"{cfg.get('email', {}).get('subject_prefix', '[Monitor]')} " \
                  f"{cfg['site_name']}: {summarise(changes)}"
        body = render_text(cfg, changes, len(items), unchanged)
        if args.dry_run:
            print("\n--- would send ---\n" + subject + "\n\n" + body)
        else:
            send_email(cfg, subject, body)
        append_history(history_path, changes)
    elif not previous:
        print("  first run — recording the baseline, no email")
    else:
        print("  nothing changed — no email")

    save_snapshot(snapshot_path, items)
    print(f"  snapshot written to {snapshot_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
