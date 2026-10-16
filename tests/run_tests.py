#!/usr/bin/env python3
"""
Tests for the monitor. Run from the project root:

    python tests/run_tests.py

No network, no email. Everything runs against the saved HTML in tests/fixtures.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import monitor as M  # noqa: E402

PASS, FAIL = 0, 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok    {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}\n          got:  {got!r}\n          want: {want!r}")


def section(title):
    print(f"\n{title}")


# ---------------------------------------------------------------- prices
section("parse_price")
cases = [
    ("€ 129,95", "129.95"),
    ("129,95 EUR", "129.95"),
    ("£51.77", "51.77"),
    ("$1,299.00", "1299.00"),
    ("€ 1.299,00", "1299.00"),
    ("1 299,00 €", "1299.00"),
    ("24,50", "24.50"),
    ("189", "189"),
    ("on request", None),
    ("", None),
    (None, None),
    ("Preis auf Anfrage", None),
    ("ab € 89,90", "89.90"),
    ("1,299", "1299"),                 # single separator, three digits = grouping
    ("1.299", "1299"),
    ("€ 1.234.567,89", "1234567.89"),
    ("$1,234,567.89", "1234567.89"),
    ("129,95 € inkl. MwSt.", "129.95"),
    ("UVP 149,00 / jetzt 129,95", "149.00"),   # first number wins
    ("0,99", "0.99"),
    ("1.299,00 €", "1299.00"),
]
for raw, want in cases:
    check(f"{raw!r}", M.parse_price(raw), want)


# ---------------------------------------------------------------- stock
section("parse_stock")
for raw, want in [
    ("In stock · Berlin", True),
    ("Out of stock", False),
    ("Sold out", False),
    ("Verfügbar", True),
    ("Nicht verfügbar", False),
    ("Auf Lager", True),
    ("Ausverkauft", False),
    ("2 left", None),
    (None, None),
]:
    check(f"{raw!r}", M.parse_stock(raw), want)

# "nicht verfügbar" must win over "verfügbar" - the out-words are checked first
check("negation beats the positive word", M.parse_stock("leider nicht verfügbar"), False)


# ---------------------------------------------------------------- parsing
section("parse_items")
cfg = M.load_config(ROOT / "tests" / "config.test.yaml")
html_v1 = (ROOT / "tests/fixtures/v1/page-1.html").read_text(encoding="utf-8")
items_v1 = M.parse_items(html_v1, cfg)

check("item count", len(items_v1), 5)
by_key = {i.key: i for i in items_v1}
check("drill price", by_key["/p/acme-pro-drill-18v"].price, "129.95")
check("thousands separator", by_key["/p/acme-battery-5ah"].price, "1299.00")
check("battery out of stock", by_key["/p/acme-battery-5ah"].in_stock, False)
check("no price -> None", by_key["/p/acme-case"].price, None)
check("german availability", by_key["/p/acme-case"].in_stock, True)
check("title read from attribute", by_key["/p/acme-impact-driver"].title, "Acme Impact Driver")

html_v3 = (ROOT / "tests/fixtures/v3/page-1.html").read_text(encoding="utf-8")
check("redesigned page yields nothing", len(M.parse_items(html_v3, cfg)), 0)


# ---------------------------------------------------------------- comparison
section("compare")
html_v2 = (ROOT / "tests/fixtures/v2/page-1.html").read_text(encoding="utf-8")
items_v2 = M.parse_items(html_v2, cfg)
previous = {i.key: {"title": i.title, "price": i.price,
                    "in_stock": i.in_stock, "url": i.url} for i in items_v1}

changes = M.compare(previous, items_v2, cfg)
kinds = sorted(c.kind for c in changes)
check("kinds found", kinds, ["new", "price", "removed", "stock"])

price_changes = [c for c in changes if c.kind == "price"]
check("only one price change passes the 0.5% threshold", len(price_changes), 1)
check("it is the drill", price_changes[0].item.key, "/p/acme-pro-drill-18v")
check("old price", price_changes[0].old, "129.95")
check("new price", price_changes[0].new, "119.00")

stock = [c for c in changes if c.kind == "stock"][0]
check("battery came back", (stock.item.key, stock.old, stock.new),
      ("/p/acme-battery-5ah", "out of stock", "in stock"))

new = [c for c in changes if c.kind == "new"][0]
check("sander is new", new.item.key, "/p/acme-sander")

removed = [c for c in changes if c.kind == "removed"][0]
check("case was removed", removed.item.key, "/p/acme-case")

# threshold off -> the 0.04% move shows up too
cfg_zero = json.loads(json.dumps(cfg))
cfg_zero["alerts"]["price_change_min_percent"] = 0.0
check("threshold 0 catches the tiny move",
      len([c for c in M.compare(previous, items_v2, cfg_zero) if c.kind == "price"]), 2)

# same input twice -> nothing
check("no changes against itself", M.compare(previous, items_v1, cfg), [])

# switches off
cfg_off = json.loads(json.dumps(cfg))
cfg_off["alerts"]["notify_new_items"] = False
cfg_off["alerts"]["notify_removed_items"] = False
off_kinds = sorted({c.kind for c in M.compare(previous, items_v2, cfg_off)})
check("new/removed can be switched off", off_kinds, ["price", "stock"])


# ---------------------------------------------------------------- summary line
section("summarise")
check("mixed", M.summarise(changes), "1 price change · 1 stock change · 1 new listing · 1 listing gone")
check("empty", M.summarise([]), "no changes")


# ---------------------------------------------------------------- end to end
section("end to end (three runs through the command line)")
tmp = Path(tempfile.mkdtemp())
state = tmp / "snapshot.json"
history = tmp / "history.csv"


def run(fixture, extra=()):
    return subprocess.run(
        [sys.executable, "monitor.py",
         "--config", "tests/config.test.yaml",
         "--local", f"tests/fixtures/{fixture}",
         "--state", str(state), "--history", str(history),
         "--dry-run", *extra],
        cwd=ROOT, capture_output=True, text=True,
    )

first = run("v1")
check("run 1 exit code", first.returncode, 0)
check("run 1 records a baseline", "first run" in first.stdout, True)
check("run 1 sends nothing", "would send" in first.stdout, False)
check("run 1 wrote a snapshot", state.exists(), True)
check("snapshot holds 5 items", json.loads(state.read_text())["count"], 5)

second = run("v1")
check("run 2 exit code", second.returncode, 0)
check("run 2 reports no change", "nothing changed" in second.stdout, True)
check("run 2 sends nothing", "would send" in second.stdout, False)

third = run("v2")
check("run 3 exit code", third.returncode, 0)
check("run 3 would send an email", "would send" in third.stdout, True)
check("run 3 subject carries the summary",
      "1 price change" in third.stdout and "1 stock change" in third.stdout, True)
check("run 3 names the unchanged remainder", "unchanged" in third.stdout, True)
check("run 3 wrote history", history.exists(), True)
check("history has 4 rows plus a header",
      len(history.read_text().strip().splitlines()), 5)

fourth = run("v3")
check("redesign exits with code 2", fourth.returncode, 2)
check("redesign warns instead of saying all-clear",
      "found nothing" in fourth.stdout, True)
snapshot_after = json.loads(state.read_text())
check("redesign did NOT overwrite the good snapshot", snapshot_after["count"], 5)

shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- result
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
