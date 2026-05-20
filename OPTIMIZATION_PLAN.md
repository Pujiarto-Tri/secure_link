# Secure Link — Optimization Plan

Target: `lombokbaratkab.go.id` scanner (`Pujiarto-Tri/secure_link`)
Goals: **make scans dramatically faster** and **cut false positives** without losing real detections (judol, obat aborsi, obat penguat, konten dewasa, penipuan).

This plan was written after reading the current code:
- `detector/scraper.py` — crawler (ThreadPoolExecutor over `requests.Session`)
- `detector/detection.py` — keyword + confidence scoring engine
- `detector/views.py::RunScanView` — scan orchestrator (runs synchronously inside an HTTP request)
- `detector/models.py` — `ScanSession`, `ScrapedPage`, `DetectedContent`, `ScanLog`, `Whitelist`
- `content_guardian/settings.py` — SQLite, default Django

The recommendations are split into **quick wins (days)**, **medium changes (1–2 weeks)** and **bigger refactors (sprint-sized)**, each ranked by impact.

---

## 0. Hardware Constraints — Single Local Machine (i3 12th gen, 4 GB RAM)

The app runs on one local computer with a **Core i3 12th gen (4P + 4E cores, ~8 threads)** and **4 GB RAM**. Every recommendation below has been chosen to fit those limits. To make sure of that, we deliberately:

- **Stay on SQLite.** No PostgreSQL. Instead: enable **WAL mode**, set `synchronous=NORMAL`, `busy_timeout=30000`, and batch all writes via `bulk_create` inside `transaction.atomic()`. With proper batching SQLite handles 100k+ pages comfortably and uses ~tens of MB of RAM.
- **No Celery + Redis.** Background execution runs as either:
  - **(preferred, simplest)** a single Python `threading.Thread` daemon started inside the Django process when the scan is enqueued. The thread reads jobs from a `queue.Queue` and writes progress to the DB. ~0 MB extra RAM, no new service.
  - **(alternative)** `huey` with the SQLite consumer (`huey.SqliteHuey`) if you want a proper out-of-process worker without Redis. ~30–40 MB RAM for the consumer.
- **Cap crawler concurrency at ~10–15** instead of the 30–60 a server-class box could run. Each in-flight HTTP request + parsed DOM peaks around 5–15 MB; 10–15 workers keeps the scanner under ~250 MB even on large pages.
- **Use `selectolax` instead of BeautifulSoup.** `selectolax` (Modest C engine) is 5–10× faster *and* uses less memory than `lxml + bs4` — better on both axes for a 4 GB box.
- **Aho-Corasick is RAM-cheap.** `pyahocorasick` builds a single automaton for all keywords in a few MB and matches in one pass. This is a *win* for low-RAM machines, not a cost.
- **Skip HTTP/2 + async if it gets complicated.** Sticking with threaded `requests.Session` is fine at 10–15 workers on this box. `httpx[http2]` is optional polish.
- **Trim log volume.** `ScanLog` is the table that grows fastest and uses the most disk I/O. Throttle log writes (Sprint 1) and add a retention policy (e.g. keep last 30 days).

**Expected steady-state RAM usage after all sprints: ~300–500 MB** for the worker thread + Django process combined. Fits comfortably in 4 GB alongside Chrome/VS Code.

If you ever outgrow this hardware later (e.g. scanning many `.go.id` sites in parallel), the plan still upgrades cleanly to Postgres + Celery without rewriting the detection or crawl code.

### What to do first (TL;DR)

Execute **Sprint 1 (§5)** in this exact order — it's all code changes, no new packages, no infra. Expected wall-clock improvement on the very first commit: ~5–7×. Order matters because each item makes the next one cheaper to verify:

1. **`detector/detection.py`** — compile keyword regex into one alternation pattern per category (`§3.1`). Also clean up the obviously-noisy keywords (`§4.1`: drop `4d`, `3d`, `2d`, `wd`, `depo`, `forex` dup, `rahim`, …).
2. **`detector/detection.py`** — fix `find_safe_context` to use `\b` instead of plain `in` (`§4.4`).
3. **`detector/views.py::RunScanView`** — build a `keyword_map` dict once per scan; throttle `ScanLog` writes and `scan_session.save` (`§3.2`, `§3.3`, `§3.4`).
4. **`detector/scraper.py`** — `deque + set` for the URL frontier, persistent `ThreadPoolExecutor`, bump workers to ~12, drop the double `BeautifulSoup` parse (`§3.5`, `§3.6`, `§3.7`).
5. **`detector/scraper.py`** — add URL canonicalization (drop `utm_*`, `PHPSESSID`, sort query params) (`§3.10`).

Sprint 2 (background worker thread + SQLite WAL + batched writes + evidence-based scorer) comes next; it's also no-new-infra and brings a further ~2× plus the big false-positive drop.

---

## 1. Diagnosis — Why It's Slow & Why It Has False Positives

### 1.1 Speed bottlenecks (root causes)

| # | Where | Problem | Impact |
|---|-------|---------|--------|
| S1 | `RunScanView.post` | Whole scan runs **synchronously inside the AJAX request**. No Celery/RQ/Dramatiq. A 1000-page scan = a 30+ min HTTP request. If the user reloads, the worker dies. | Critical |
| S2 | `settings.py` | **SQLite is in default (rollback-journal) mode** *and* multi-threaded inserts hammer it. Every page triggers `ScrapedPage.create`, multiple `ScanLog.create`, N × `DetectedContent.create`, plus `Keyword.objects.filter(...).first()` per detection. Without WAL, SQLite serializes all writes globally → "database is locked" + huge wall time. (Solution on a 4 GB box: keep SQLite, switch on WAL + batched writes — see §3.13.) | Critical |
| S3 | `scraper.py::crawl` | `ThreadPoolExecutor` is **created/destroyed per batch** inside the `while` loop. Workers can't start the next URL until the slowest URL in the batch finishes. No streaming pipeline. | High |
| S4 | `scraper.py` | `max_workers=3` (and `=5` default) — far too low for I/O-bound crawling. Government CMS pages are mostly idle waiting for the server. | High |
| S5 | `scraper.py::crawl` | `urls_to_visit` is a plain `list` with `pop(0)` (O(n)) and `link not in urls_to_visit` (O(n)) on every link. Becomes quadratic with >1k discovered URLs. | High |
| S6 | `scraper.py` | **No `sitemap.xml` discovery.** Government CMS sites (typically OpenSID/CMS Balitbang/Wordpress) almost always expose a sitemap. Crawling from scratch via BFS is 5–20× slower than reading the sitemap. | High |
| S7 | `detection.py::detect` | For every page we run **one regex per keyword** (≈150+ patterns) × every page. Each pattern is **recompiled on every call** (`pattern = r'\b' + re.escape(...) + r'\b'`). | High |
| S8 | `views.py` callback | `Keyword.objects.filter(keyword__iexact=..., is_active=True).first()` is run **per detection per page**. Easily 100s of queries per page. | High |
| S9 | `views.py` callback | `ScanLog.objects.create` is called for **every page success, every page failure, every detection, every whitelist skip**. Then `scan_session.save(update_fields=...)` again. On SQLite, this is the dominant cost. | High |
| S10 | `scraper.py::extract_content` | `BeautifulSoup(str(soup), 'lxml')` **re-parses HTML twice** per page (once in `scrape_page`, again here). BeautifulSoup itself is slow vs `selectolax` / direct `lxml`. | Medium |
| S11 | `scraper.py::scrape_page` | Downloads full body before checking `Content-Type`. PDFs/binaries served without extension still hit network + memory. | Medium |
| S12 | `scraper.py` | Connection pool is fixed at 10. No HTTP/2. No DNS cache. No `Accept-Encoding: br`. | Medium |
| S13 | crawler | No URL canonicalization for query strings (`?utm_*`, `?page=1`, session ids, `PHPSESSID`). Same page is fetched many times. | Medium |
| S14 | crawler | No content-hash dedup. Pagination pages and "mirror" URLs get scanned repeatedly. | Medium |
| S15 | crawler | No `robots.txt` / `Retry-After` / 429 / 503 backoff. A WAF block can poison every subsequent request. | Medium |
| S16 | crawler | No incremental scanning. Every run re-downloads everything; no ETag / Last-Modified support. | Medium |

### 1.2 False-positive root causes

| # | Where | Problem |
|---|-------|---------|
| F1 | `DEFAULT_KEYWORDS['judol']` | Includes very generic tokens: **`4d`, `3d`, `2d`, `wd`, `depo`, `cashback`, `referral`, `shio`, `ekor`, `withdraw`, `forex`, `bitcoin mining`, `crypto investment`**. These match legit content ("3D printing", "WD-40", "ekor pesawat", "cashback BPJS", "referral pasien", "withdrawal symptom", financial news). Even `\b` boundaries don't help. |
| F2 | `DEFAULT_KEYWORDS['obat_aborsi']` | Includes single common words like **`rahim`**, **`kandungan`**-adjacent terms. Any kesehatan ibu article triggers. The test `test_safe_context_lowers_score` actually proves this — but it relies on safe-context being present, which isn't reliable. |
| F3 | `DEFAULT_KEYWORDS['obat_penguat']` | **`forex`** is duplicated here from `judol`. `mr p` is a substring of many names. `obat dewasa` is generic. |
| F4 | `detection.py::detect` | Each keyword is matched **independently**. A single isolated match in a 50KB article gets the same base "detected" treatment as a page with 10 unique judol keywords clustered together. Real judol pages have **high keyword density + low diversity of safe context**. |
| F5 | `calculate_confidence_score` | Score adjustments are static & small (−0.1, −0.15…). They don't model **keyword diversity**, **keyword density**, **proximity clustering**, or **outbound link signals**. Real judol injections always come with outbound links to `*.com`/`*.online`/`*.xyz` with `slot|gacor|togel` in the URL — that's a near-perfect signal currently ignored. |
| F6 | `detection.py` | No language detection. English porn keywords (`porn`, `xxx`, `nude`) almost never appear on Indonesian govt pages **unless** the page was injected. But that's the same in both directions — currently the engine can't tell. |
| F7 | `detection.py` | No structural signals: hidden `<div style="display:none">`, suspicious unicode characters / zero-width spaces, cloaked text in CSS, "spam blocks" appended at the bottom of `<body>`. These are the exact patterns used to inject judol into govt sites. |
| F8 | `detection.py` | `find_safe_context` is a plain substring scan (`in`) — matches inside words ("kandungan" inside "berkandungan"). Should also use `\b`. |
| F9 | `views.py` | Detection writes one `DetectedContent` row **per keyword occurrence**. A single judol page with 20 keyword hits creates 20 nearly-identical rows. Should aggregate per (page, category). |

---

## 2. Recommended Architecture (Target State)

```
                       ┌─────────────────────────┐
   user clicks "Scan"  │  Django view            │
   ───────────────────▶│  RunScanView.post       │
                       │  enqueue task & return  │
                       └──────────┬──────────────┘
                                  │  (in-process Thread + queue.Queue,
                                  │   or huey.SqliteHuey)
                                  ▼
                       ┌─────────────────────────┐
                       │  Worker (same box)      │
                       │  ┌───────────────────┐  │
                       │  │ Sitemap fetcher   │  │  ← seed URLs
                       │  └─────────┬─────────┘  │
                       │            ▼            │
                       │  ┌───────────────────┐  │
                       │  │ Threaded crawler  │  │  requests.Session,
                       │  │ (ThreadPoolExec.) │  │  10–15 workers,
                       │  │  bounded queue    │  │  HEAD + size cap
                       │  └─────────┬─────────┘  │
                       │            ▼            │
                       │  ┌───────────────────┐  │
                       │  │ Parser (selectolax│  │
                       │  │  / lxml)          │  │
                       │  └─────────┬─────────┘  │
                       │            ▼            │
                       │  ┌───────────────────┐  │
                       │  │ Detector          │  │  Aho-Corasick
                       │  │ (1-pass match +   │  │  + scorer (density,
                       │  │  scorer)          │  │  diversity, links,
                       │  └─────────┬─────────┘  │  language, hidden CSS)
                       │            ▼            │
                       │  ┌───────────────────┐  │
                       │  │ Batched DB writer │  │  bulk_create every
                       │  │ (transactional)   │  │  25 pages / 2 seconds
                       │  └───────────────────┘  │
                       └─────────────────────────┘
                                  │
                                  ▼
                          SQLite (WAL mode,
                          batched writes,
                          indexes)
```

---

## 3. Speed Optimization — Phased Plan

### Phase 0 — Quick wins (≈ 1–2 days, no infra changes)

These changes touch only `scraper.py`, `detection.py`, `views.py`. Expected combined wall-clock speedup on a 500-page scan: **4–8×.**

1. **Pre-compile keyword regex into one alternation pattern per category**
   In `detection.py`, after loading keywords, build:
   ```python
   self._compiled = {
       category: re.compile(
           r'\b(?:' + '|'.join(re.escape(k) for k in sorted(set(words), key=len, reverse=True)) + r')\b',
           re.IGNORECASE,
       )
       for category, words in self.keywords.items()
   }
   ```
   Then `detect()` runs **one `finditer` per category** (5 passes) instead of one per keyword (~150 passes). With `re.IGNORECASE`, drop the manual `.lower()` step.
   *Expected: 10–30× faster detection per page.*

2. **Build an in-memory `keyword_text → Keyword` map once per scan**
   Replace the per-detection `Keyword.objects.filter(...).first()` with a single dict lookup:
   ```python
   keyword_map = {kw.keyword.lower(): kw for kw in active_keywords}
   ```
   *Expected: removes N×P DB queries; biggest single ORM win.*

3. **Throttle `ScanLog` writes** — only log:
   - first error per URL pattern,
   - one summary log every N pages (e.g. every 25),
   - every detection above `confidence_score ≥ 0.7`.
   Keep the noisy per-page success logs out of the hot path. Optionally batch logs and `bulk_create` them every 1–2 seconds.

4. **Throttle `scan_session.save(update_fields=...)`** — write progress at most once per second (or every N pages), not every page.

5. **Replace `urls_to_visit = list` with `collections.deque` + `seen: set`** — eliminates O(n) `pop(0)` and `in` lookups.

6. **Bump `max_workers` to 10–15 on this box (i3 / 4 GB) and pool sizes accordingly**
   - `HTTPAdapter(pool_connections=max_workers, pool_maxsize=max_workers * 2)`
   - Pre-create one `ThreadPoolExecutor` outside the `while` loop; submit URLs as a pipeline (consumer that re-submits new links immediately when one completes).
   - On a server-class box you could push this to 30–60; here, 10–15 keeps RAM well under control and is already 3–5× the current setting.

7. **Drop the double `BeautifulSoup` parse in `extract_content`** — operate on the original soup, decompose `script/style/noscript/iframe/svg` in-place.

8. **Pre-compile the `re.compile(r'content|main|body', re.I)` once at module level.**

9. **Skip-extension check via `frozenset` + `os.path.splitext`** — O(1) lookup instead of iterating a list.

10. **URL canonicalization** before adding to the queue:
    - lowercase host,
    - strip `#fragment` (already done),
    - drop tracking params (`utm_*`, `gclid`, `fbclid`, `PHPSESSID`, `sid`),
    - sort remaining query params,
    - collapse trailing `/`.
    Cuts visited URL set by typically 20–40 % on a CMS.

11. **Bound page content for detection** — current code already trims to 50 000 chars, but also trim leading/trailing whitespace and skip detection on empty bodies (HTTP 200 but no extracted text → don't even bother).

### Phase 1 — Background execution + SQLite tuning (≈ 3–5 days, **no new infra**)

All items here are chosen to work on the 4 GB / i3 box without Postgres or Redis.

12. **Move scans out of the HTTP request — in-process worker thread.**
    Add a tiny module `detector/runner.py` with:
    ```python
    import queue, threading
    _q: queue.Queue[int] = queue.Queue()
    _started = False
    _lock = threading.Lock()

    def _worker_loop():
        while True:
            session_pk = _q.get()
            try:
                _run_scan(session_pk)   # current RunScanView body, refactored
            except Exception:
                logger.exception("scan failed")
            finally:
                _q.task_done()

    def enqueue(session_pk: int) -> None:
        global _started
        with _lock:
            if not _started:
                threading.Thread(target=_worker_loop, daemon=True).start()
                _started = True
        _q.put(session_pk)
    ```
    `RunScanView.post` then becomes: mark `pending` → `runner.enqueue(pk)` → return 202.
    The existing run-scan template already polls; keep it. Cancellation still works via `ScanSession.is_cancelled` (the runner calls `should_stop()` in its loop).

    **Alternative if you want a separate process:** use [`huey`](https://huey.readthedocs.io/) with `SqliteHuey` — no Redis required, ~30 MB RAM, runnable as `python manage.py run_huey`.

13. **Tune SQLite for concurrent writers (stay on SQLite).**
    In `settings.py`:
    ```python
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {
                "timeout": 30,           # busy_timeout, ms? -> django passes seconds; use 30
                "init_command": (
                    "PRAGMA journal_mode=WAL;"
                    "PRAGMA synchronous=NORMAL;"
                    "PRAGMA temp_store=MEMORY;"
                    "PRAGMA mmap_size=134217728;"   # 128 MB
                    "PRAGMA cache_size=-20000;"     # ~20 MB page cache
                ),
                "transaction_mode": "IMMEDIATE",   # Django 5.1+
            },
        }
    }
    ```
    WAL alone removes the "database is locked" failures the threaded writer hits today. RAM cost: ~50 MB.

14. **Batch all DB writes inside the worker.**
    Instead of `ScrapedPage.objects.get_or_create` + `DetectedContent.objects.create` + `ScanLog.objects.create` per page, accumulate in lists and flush:
    - every 25 pages, **or**
    - every 2 seconds,
    using `bulk_create` inside `transaction.atomic()`. This is even more important on SQLite than on Postgres — each open transaction takes a write lock.

15. **Add DB indexes** that match the actual queries:
    - `ScanLog (scan_session_id, -created_at)` for the live log poll,
    - `ScrapedPage (scan_session_id, status)`,
    - `DetectedContent (page_id, category)`, `DetectedContent (confidence_score)`.

16. **Aggregate detections per (page, category)** in `DetectedContent`:
    add `match_count INT`, `unique_keywords TEXT` (JSON-encoded list — SQLite has no native array but a JSON column works fine), `sample_contexts TEXT` (JSON) instead of inserting one row per occurrence. Cuts table size 10–30×, makes the UI faster and the DB file smaller.

17. **Bound `ScanLog` growth.** Add a management command `prune_scan_logs --keep-days=30` and run it nightly via `cron` or `apscheduler`. Stops the SQLite file from growing forever on a small disk.

### Phase 2 — Faster crawler (≈ 1 week)

18. **Sitemap-first seeding.**
    On `RunScanView`, fetch:
    - `/sitemap.xml`,
    - `/sitemap_index.xml`,
    - `/robots.txt` (for `Sitemap:` directives),
    - common WordPress / OpenSID extras (`/wp-sitemap.xml`, `/sitemap-posts.xml`, …).
    Push all sitemap URLs into the queue **before** starting BFS. For most govt sites this turns "discover 90 % of pages" from a 30-min crawl into a 30-second download.

19. **Optional: switch to `httpx` / `aiohttp`.**
    On a 4 GB / i3 box this is a *nice-to-have*, not a must — threaded `requests.Session` at 10–15 workers is already CPU/IO-bound on the network, not on Python overhead. If you go async, cap the semaphore at **~15 concurrent requests** and keep one event-loop thread; this still uses less RAM than the current `ThreadPoolExecutor`. Skip HTTP/2 unless the target site clearly supports it.

20. **Streaming parsing with `selectolax` (Modest engine).**
    Drop-in replacement for the parts of BeautifulSoup you actually use (text extraction + link extraction). Typically **5–10× faster** parsing **and lower memory** — a real win on 4 GB.

21. **HEAD probe + size limit.**
    Before downloading unknown URLs, optionally issue a `HEAD`. Skip if:
    - `Content-Type` is not `text/html`,
    - `Content-Length > 2 MB`,
    - `Content-Length` missing **and** the response stream exceeds `MAX_BYTES`.

22. **Adaptive rate limiting + backoff.**
    - Respect `Retry-After`.
    - Treat consecutive 429/503/timeouts as a signal to halve the concurrency until they stop.
    - Per-host token bucket so subdomains don't overwhelm each other.

23. **Content-hash dedup.**
    Compute `sha1(normalized_text)`; if a hash was already seen in this scan, store the URL on the existing page row and skip detection. Saves a lot of work on paginated archives.

24. **Incremental scans.**
    Persist `etag` + `last_modified` per URL across scans. On the next run, send `If-None-Match` / `If-Modified-Since`; if you get 304, skip parsing entirely. Allows daily scans to be 5–20× cheaper than the first scan.

### Phase 3 — Bigger refactors (sprint-sized)

25. **Aho-Corasick automaton for keyword matching.**
    Use `pyahocorasick` (C extension). Build one automaton per scan covering every active keyword across all categories. Matching becomes O(text_length + matches) regardless of how many keywords you have. Easily handles 10 000+ keywords. RAM footprint: a few MB — perfect for a 4 GB box.

26. **Two-stage detection.**
    - **Stage A (cheap):** Aho-Corasick first-pass on the *raw response body* (no parsing). If zero hits → skip parsing entirely. Massive speedup for the bulk of "clean" pages.
    - **Stage B (deep):** only on pages with hits — parse, extract structured content, run the full scorer.

27. **(Future, only if you outgrow this machine)** Job sharding for huge sites: split sitemap URLs into chunks, run N workers in parallel on a beefier box, all writing to the same `ScanSession` (needs the batched/atomic writer from §14).

---

## 4. False-Positive Reduction Plan

The single biggest improvement is to **stop treating "1 keyword match = detection"** and instead score on *evidence strength*. Concrete steps:

### 4.1 Clean up the keyword list (immediate)

- **Move these to "phrase-only" (require multi-word context)** instead of single tokens:
  - `judol`: drop bare `4d`, `3d`, `2d`, `wd`, `depo`, `withdraw`, `cashback`, `referral`, `shio`, `ekor`, `forex`, `crypto investment`, `bitcoin mining`. Replace with phrases like `wd cepat`, `wd lancar`, `depo pulsa`, `depo dana`, `cashback slot`, `cashback turnover`, `referral judi`, `forex bodong`, `crypto judi`.
  - `obat_aborsi`: drop bare `rahim` (extremely common). Replace with phrases: `gugurkan rahim`, `obat rahim aborsi`, `bersihkan rahim`. Keep the strong specific ones (`misoprostol`, `cytotec`, `gastrul`, `obat aborsi`, `obat penggugur`, `klinik aborsi`).
  - `obat_penguat`: dedupe `forex` (it's already in judol; either drop here or move to one canonical category). Tighten `obat dewasa` to `obat kuat dewasa`.

- **Tier keywords by strength** in the DB. Add a column `weight` (or `strength`: `decisive | strong | weak`):
  - `decisive` — single match → high confidence (`misoprostol`, `cytotec`, `slot gacor`, `bandar togel`, `link alternatif slot`, `rtp slot`, `gates of olympus`).
  - `strong` — needs 1 corroborating keyword.
  - `weak` — needs ≥3 corroborating keywords or a decisive one.

### 4.2 Rewrite the scorer to use evidence aggregation

Replace `calculate_confidence_score` with something like:

```python
def score(detections, full_text, title, url, outbound_links):
    by_cat = group_by(detections, "category")
    score_per_cat = {}
    for cat, ds in by_cat.items():
        unique_kws = {d["keyword"] for d in ds}
        strengths = [STRENGTH[d["keyword"]] for d in ds]
        density   = len(ds) / max(len(full_text), 1) * 1000  # hits per 1k chars
        in_title  = any(d["location"] == "title" for d in ds)
        in_meta   = any(d["location"] == "meta"  for d in ds)
        clustered = max_window_count(ds, window=300) >= 3  # 3+ hits within 300 chars
        bad_links = suspicious_link_count(outbound_links, cat)

        s = 0.0
        if "decisive" in strengths:        s += 0.6
        if len(unique_kws) >= 3:           s += 0.25
        if clustered:                      s += 0.15
        if in_title or in_meta:            s += 0.15
        if density > 2.0:                  s += 0.1
        if bad_links > 0:                  s += 0.25

        # safe-context dampening
        safe = find_safe_context(full_text, cat)
        s -= min(len(safe) * 0.05, 0.25)
        if is_health_institution(title, url) and cat in HEALTH_FP_CATS:
            s -= 0.2

        score_per_cat[cat] = clamp(s, 0.0, 1.0)
    return score_per_cat
```

This gives:
- **single isolated weak match in a 50KB article → score < 0.3** (auto-marked `is_false_positive=True`).
- **judol-injected page with 5+ keywords + outbound `slot-gacor-xx.com` link → score ≥ 0.95**.

### 4.3 Add structural signals (high precision)

- **Outbound-link inspection.** Extract every `<a href>` and look for hosts containing `slot`, `togel`, `gacor`, `judi`, `bola`, `casino`, etc., or TLDs heavily used by judol (`.online`, `.xyz`, `.win`, `.bet`, `.live`, plus suspicious numeric subdomains like `slot88.com`, `slot777.net`). Even **one** such outbound link from a `.go.id` page is essentially a definitive signal.

- **Hidden/cloaked content detection.** Before discarding `<style>`, scan for:
  - inline `style="display:none|visibility:hidden|font-size:0|opacity:0"`,
  - `position:absolute; left:-9999px`,
  - text rendered same color as background,
  - zero-width characters (`\u200b`, `\u200c`, `\u200d`, `\ufeff`),
  - large `<noscript>`/`<div>` blocks at the very end of `<body>`.
  Flag these blocks **separately** as `category=cloaking` and **boost** the confidence score of any detections found inside them.

- **Title/URL slug mismatch.** If the URL slug suggests an official page (`/pengumuman/`, `/berita/`, `/profil/`) but the title contains slot/togel/etc., that mismatch is itself evidence of injection.

- **Language signal.** If a page is mostly Indonesian but contains an isolated English porn keyword (`porn`, `xxx`, `nude`), it's very likely injection → boost confidence. Conversely, an English-only news article quoting one of these words should drop confidence.

### 4.4 Refine safe-context handling

- Use **`\b`-bounded matching** for safe-context too — currently it's a plain `in` substring scan that produces its own false positives ("kandungan" matched inside "berkandungan").
- Add a per-category **safe-domain list**: if `host` is `kemenkes.go.id`, `dinkes-*.go.id`, anything under `who.int` → strongly dampen `obat_aborsi` / `obat_penguat`.
- Add a **publication-context** signal: pages whose URL contains `/berita/`, `/artikel/`, `/pengumuman/`, `/siaran-pers/` and which have visible byline / date in the DOM → likely legitimate news → dampen weak/strong matches, never dampen decisive ones.

### 4.5 Feedback loop

You already have the `Whitelist` model. Add:
- **One-click "mark as false positive"** in the detection detail view — it should auto-create a `Whitelist` of type `keyword_url` and re-score future detections.
- **Mined rules:** when N false positives share a common pattern (same domain, same URL prefix, same keyword), surface a prompt to whitelist the pattern.
- **Periodically re-score historical detections** with the new scorer so old data benefits from rule changes.

---

## 5. Roadmap (Suggested Order)

Pick the smallest set first to get the biggest win on day one. Numbers map to sections above.

### Sprint 1 — "10× faster, fewer obviously-wrong detections" (≈ 3 days)
- [ ] §3.1 compile keyword regex per category (single pass)
- [ ] §3.2 in-memory `keyword_map`
- [ ] §3.3 throttle `ScanLog`
- [ ] §3.4 throttle progress saves
- [ ] §3.5 `deque` + `seen` set
- [ ] §3.6 bump workers + pool size, persistent executor
- [ ] §3.7 remove double `BeautifulSoup` parse
- [ ] §3.10 URL canonicalization
- [ ] §4.1 clean up generic keywords (`4d`, `wd`, `rahim`, `forex` dup, …)
- [ ] §4.4 word-boundary safe-context

### Sprint 2 — "Real backend, no more browser timeouts" (≈ 5 days, **no new infra**)
- [ ] §3.12 in-process worker thread (or huey + SQLite)
- [ ] §3.13 SQLite WAL + tuning
- [ ] §3.14 batched `bulk_create` writers
- [ ] §3.15 indexes
- [ ] §3.16 aggregated `DetectedContent` schema
- [ ] §3.17 `ScanLog` retention command
- [ ] §4.2 evidence-based confidence scorer
- [ ] §4.5 one-click "mark FP → whitelist"

### Sprint 3 — "Scale to the whole .go.id ecosystem" (≈ 1–2 weeks)
- [ ] §3.18 sitemap-first seeding
- [ ] §3.19 (optional) async crawler (`httpx`)
- [ ] §3.20 `selectolax` parser
- [ ] §3.21 HEAD probe + size cap
- [ ] §3.22 adaptive backoff
- [ ] §3.23 content-hash dedup
- [ ] §3.24 ETag/Last-Modified incremental scans
- [ ] §4.3 outbound-link + cloaking detectors
- [ ] §3.25 Aho-Corasick
- [ ] §3.26 two-stage detection

---

## 6. Expected Wins (rough estimates, single 500-page scan, i3 12th gen + 4 GB RAM)

| Change | Wall time | Detections | False Positives | Peak RAM |
|--------|-----------|------------|------------------|----------|
| Baseline (today) | ~25–40 min | 100 % | 100 % | ~200 MB |
| Sprint 1 done | ~4–8 min (≈ 5–7×) | ≈ same | ≈ −40 % (cleaner keywords, scorer untouched) | ~250 MB |
| Sprint 2 done | ~3–6 min, scans run in background, no browser timeout | ≈ same | **≈ −70 %** (evidence scorer) | ~300 MB |
| Sprint 3 done | ~1–2 min on 500 pages, ~10–15 min on 5 000 pages | **+15 %** (catches cloaked injections) | **≈ −85 %** (link + cloaking signals) | ~350–500 MB |

These are educated guesses based on the bottlenecks above; measure before/after on a real scan to confirm. The numbers are intentionally conservative for the i3 / 4 GB box — on a faster machine each row would be ~1.5–2× faster again.

---

## 7. Operational Suggestions (orthogonal but worth doing)

- **Add a "test scan" mode** that limits to N pages + skips DB writes, for quickly validating keyword changes.
- **Per-keyword precision metric.** Track how often each keyword's detections get marked false positive vs. confirmed; auto-flag keywords with bad precision for review.
- **Re-run scoring offline.** Keep raw `content` long enough to re-score with a new ruleset without re-crawling.
- **Production hardening:** `DEBUG=False`, set `ALLOWED_HOSTS`, rotate the `SECRET_KEY` out of the repo, add `django-environ` for config.
- **Monitoring:** simple Prometheus counters — pages/sec, detections/sec, queue depth, http error rates per host.

---

## 8. Files That Will Change

Approximate scope of the refactor (Sprint 1 + 2):

- `detector/detection.py` — compiled regex, scorer rewrite, Aho-Corasick (later).
- `detector/scraper.py` — deque/set, persistent executor, sitemap fetcher, canonicalization, dedup.
- `detector/views.py::RunScanView` — enqueue background task instead of doing work inline; build keyword/whitelist maps once; pass them to a single batched writer.
- `detector/runner.py` *(new)* — tiny in-process worker thread + queue (or `huey` consumer).
- `detector/models.py` — aggregate `DetectedContent`; add indexes; add `Keyword.strength`; add `etag`/`last_modified` to `ScrapedPage`.
- `detector/migrations/` — auto-generated.
- `detector/management/commands/prune_scan_logs.py` *(new)* — log retention.
- `content_guardian/settings.py` — SQLite WAL pragmas, env-driven config.
- `requirements.txt` — `selectolax`, `pyahocorasick`, and (optionally) `huey` or `httpx[http2]`. **No Postgres, no Redis, no Celery on this box.**
- `detector/management/commands/` *(optional)* — `rescore_detections`, `import_keywords`, `import_whitelist` for offline ops.

---

*End of plan. Happy to turn any sprint into a concrete PR.*
