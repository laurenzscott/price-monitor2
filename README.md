# Price monitor

Checks the listing pages you choose once a day, compares the result against the
previous run, and emails you **only when something actually changed**. On a day
when nothing moved, no email arrives.

It runs inside your own GitHub account on the free tier. There is no server in
between and no monthly fee. If you never speak to the person who built it again,
it keeps running.

---

## The live copy in this repository

This repository is not a demo. It watches a real shop every morning at
05:04 UTC: **BerryBase, category Raspberry Pi boards** (18 products), one
request per run, identified as `price-monitor/1.0 (small personal project)`.

- [`data/snapshot.json`](data/snapshot.json) - the state after the last run
- [`data/history.csv`](data/history.csv) - every change it has detected
- **Actions** tab - the log of every run

The first changes it caught, on 18 October 2026:

```
price  Raspberry Pi Zero v1.3     12.50 -> 13.50
stock  Raspberry Pi 3 Modell A+   out of stock -> in stock
stock  Raspberry Pi 3 Modell A+   in stock -> out of stock   (same day, evening)
```

On 16 and 17 October nothing moved, so no e-mail was sent.

## What a client gets

- this monitor, set up for the shop pages they choose
- an e-mail only when a price, a stock state or a listing changes, and a
  warning instead of silence when the shop redesigns its page
- the full code and a plain-language setup document
- for the first 30 days I run it; after that it moves into the client's own
  GitHub account on the free tier, or I keep running it as a small monthly order

---

## What it watches

| | |
|---|---|
| **Price** | any movement, or only beyond a percentage you set |
| **Stock** | out of stock, back in stock |
| **New listings** | products that appeared since the last run |
| **Removed listings** | products that disappeared |

It also notices when it gets **nothing at all** — which usually means the shop
changed its layout — and emails you about that instead of quietly reporting
"no changes". A monitor that goes silent when it breaks is worse than no monitor.

---

## Setting it up (about ten minutes, once)

### 1. Put these files in a new GitHub repository

Private is fine. The scheduled run works on free accounts.

### 2. Tell it what to watch

Everything lives in `config.yaml`. Nothing else needs editing.

```yaml
site_name: "Example Shop"
start_url: "https://example-shop.com/products?page={page}"
max_pages: 3

selectors:
  item: "article.product"      # the box around one product
  title: "h3 a@title"          # "css@attribute" reads an attribute
  price: "p.price"             # "css" alone reads the text
  availability: "p.stock"
  link: "h3 a@href"

alerts:
  price_change_min_percent: 0.5   # ignore movements smaller than this
  notify_new_items: true
  notify_removed_items: true

email:
  to: ""                          # leave empty: the MAIL_TO secret is used
  subject_prefix: "[Price monitor]"
```

`{page}` in the URL is replaced with 1, 2, 3 … up to `max_pages`. A single page
without `{page}` works too.

Leave `email.to` empty when you run it on GitHub. An address written here takes
precedence over the `MAIL_TO` secret, which is only useful when you run the
monitor on your own machine and do not want to set environment variables.

**How to find the selectors:** open the shop, right-click a product, Inspect.
The element that wraps one whole product is your `item`. Everything else is
searched inside it.

### 3. Add the email credentials

In the repository: **Settings → Secrets and variables → Actions → New
repository secret**. Add:

| Secret | Value |
|---|---|
| `SMTP_USER` | the address that sends the mail |
| `SMTP_PASSWORD` | an **app password**, not your normal password |
| `MAIL_TO` | where the alerts should arrive |
| `SMTP_HOST` | optional, defaults to `smtp.gmail.com` |
| `SMTP_PORT` | optional, defaults to `587` |

For Gmail you need 2-factor authentication switched on, then create an app
password under Google Account → Security → App passwords. A normal Gmail
password will not work and should never be put here.

### 4. Run it once by hand

**Actions → Price monitor → Run workflow.**

The first run records a baseline and sends nothing — there is nothing to compare
against yet. From the second run onwards you only hear from it when something
moved.

---

## Running it on your own machine

```bash
pip install -r requirements.txt

python monitor.py                      # normal run
python monitor.py --dry-run            # print the email instead of sending it
python monitor.py --local tests/fixtures/v1   # use saved HTML, no network
```

---

## What is in the repository

```
monitor.py                    the whole program, one file
config.yaml                   what to watch — the only file you edit
requirements.txt              three libraries
.github/workflows/monitor.yml the daily schedule
data/snapshot.json            the last state. Do not edit; it is how the
                              comparison works
data/history.csv              every change ever detected, appended
tests/run_tests.py            69 checks, no network needed
tests/fixtures/               saved HTML used by the tests
```

`data/` is committed back to the repository after each run. That is deliberate:
it means the history lives with the code, and you can read it, chart it or hand
it to someone else without asking anybody for an export.

---

## Changing the schedule

In `.github/workflows/monitor.yml`:

```yaml
- cron: "4 5 * * *"      # every day at 05:04 UTC
```

GitHub runs cron in UTC and does not follow summer time. Useful values:

| | |
|---|---|
| `"4 5 * * *"` | once a day, early morning |
| `"4 */6 * * *"` | every six hours |
| `"4 5 * * 1-5"` | weekdays only |

Scheduled runs on GitHub can be delayed by a few minutes under load. For a daily
price check that does not matter.

---

## When it stops working

Shops redesign their pages a few times a year. When that happens, the selectors
in `config.yaml` no longer match and the monitor finds zero products.

It will email you: **"found nothing — needs a look"**, and it will *not*
overwrite the good snapshot, so no history is lost.

To fix it: open the shop, inspect a product again, and correct the selectors in
`config.yaml`. That is usually a two-minute change. Then run it once by hand to
confirm it finds products again.

---

## Deliberate limits

- **Public pages only.** Nothing behind a login, nothing that circumvents a
  protection. If the data you need sits behind a sign-in, this is the wrong tool.
- **Some shops block automated access.** That is their right. If a target starts
  blocking, the monitor will report that it found nothing rather than pretend.
- **It does not change your prices.** It reports what moved. The decision stays
  with you, which is also why it needs no access to your shop system.
- **One request per page per run**, 1.5 seconds apart, with a normal timeout.
  Network errors are retried up to three times; a refusal from the shop (any
  4xx such as 403 or 429) is never retried. It is deliberately slow enough
  not to be a burden on the shop being checked.
- **It says who it is.** Every request carries the User-Agent
  `price-monitor/1.0 (small personal project)`, nothing pretending to be a
  browser.

---

## Tests

```bash
python tests/run_tests.py
```

69 checks covering price formats (German, English, grouped thousands), stock
wording in two languages, the comparison logic, the percentage threshold, and
four full runs through the command line: first run, unchanged run, changed run
and a simulated redesign.

No network and no email are involved, so the tests work anywhere and always give
the same result.
