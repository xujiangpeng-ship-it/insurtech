---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 22537ee04cb2609a0d4ed75721c7b589_3f0cfe635d0e11f1abc85254006c9bbf
    ReservedCode1: MiUImpyiRkn55SmE69HxtX1al7e9laLyfQWJ+pJEej+Jo8NM1lSXmtJA5Q4RvOiEewWM87Bb5D4aogMBAMOoOx+UKfl5nV805A5LHn6DsYoZ6TwwcxulR3ZUxMv8HJgvobkctMK/md0DPto0TDeZxtjVb2S0R5YHtS1Q7WWW8KZJLO0Q6IGth3JPNEk=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 22537ee04cb2609a0d4ed75721c7b589_3f0cfe635d0e11f1abc85254006c9bbf
    ReservedCode2: MiUImpyiRkn55SmE69HxtX1al7e9laLyfQWJ+pJEej+Jo8NM1lSXmtJA5Q4RvOiEewWM87Bb5D4aogMBAMOoOx+UKfl5nV805A5LHn6DsYoZ6TwwcxulR3ZUxMv8HJgvobkctMK/md0DPto0TDeZxtjVb2S0R5YHtS1Q7WWW8KZJLO0Q6IGth3JPNEk=
---

# Distributed Content Generation Design

**Date**: 2026-06-01  
**Status**: Approved  
**Scope**: Migrate from daily batch (10 articles) to distributed scheduling (2 articles every 2.5 hours)

---

## 1. Motivation

Current system generates all 10 articles in a single daily run at 02:00 UTC. This creates several problems:

- **Content freshness**: 10 articles published simultaneously, then 24h silence. A steady trickle is better for SEO and user engagement.
- **API pressure**: 10 consecutive Mistral API calls in one burst risks rate-limiting.
- **Keyword pool**: Running once daily means users must wait 24h for new content after keyword replenishment.

Distributed generation spreads load evenly, mimics natural publishing cadence, and reduces API contention.

---

## 2. Scheduling

### 2.1 Trigger Model

Remove `push` trigger. Keep only `schedule` (cron) and `workflow_dispatch`.

GitHub Actions cron does not support non-integer-hour intervals (`*/150` minutes). Solution: two complementary cron expressions covering 10 time slots per day:

| UTC | Beijing (UTC+8) |
|-----|-----------------|
| 00:00 | 08:00 |
| 02:30 | 10:30 |
| 05:00 | 13:00 |
| 07:30 | 15:30 |
| 10:00 | 18:00 |
| 12:30 | 20:30 |
| 15:00 | 23:00 |
| 17:30 | 翌 01:30 |
| 20:00 | 04:00 |
| 22:30 | 06:30 |

```yaml
schedule:
  - cron: '0 0,5,10,15,20 * * *'
  - cron: '30 2,7,12,17,22 * * *'
```

### 2.2 Concurrency

```yaml
concurrency: generate
```

Ensures no overlapping runs. 2.5h interval >> 5 min runtime, so queuing will never occur under normal conditions.

### 2.3 Cost

- **Runs/day**: 10
- **Estimated minutes/run**: ~5 (2 articles × ~2 min API + ~1 min render/commit)
- **Monthly**: 10 × 5 × 30 = **1,500 minutes**
- **Free tier budget**: 2,000 minutes — safe with 25% headroom

---

## 3. Article Selection Strategy

### 3.1 Load-Balanced Subdomain Selection

Each run generates 2 articles. Subdomains are selected by "fewest published articles first" to ensure even content distribution across all 6 categories.

**Algorithm**:

1. Scan `content/{subdomain}/` directory structure, count existing articles per subdomain
2. Sort subdomains by article count ascending
3. Pick top 2 subdomains with available keywords
4. For each selected subdomain, randomly pick 1 unused keyword

**Tie-breaking**: if multiple subdomains have equal counts, shuffle randomly.

**Edge cases**:
- Subdomain with exhausted keyword pool → skip, try next in priority order
- Global keyword pool depleted → exit gracefully (0 articles, no error)
- Fewer than 2 subdomains have keywords → generate whatever is available (1 or 0)

### 3.2 Modified `pick_unused_keywords`

```python
def pick_unused_keywords(config, count: int):
    """Pick *count* keywords using load-balanced subdomain selection."""
    subdomains = config["subdomains"]
    
    # Count existing articles per subdomain
    sd_counts = {}
    for sd in subdomains:
        sd_dir = CONTENT_DIR / sd["slug"]
        if sd_dir.is_dir():
            sd_counts[sd["slug"]] = sum(1 for d in sd_dir.iterdir() if d.is_dir())
        else:
            sd_counts[sd["slug"]] = 0
    
    # Load available keywords per subdomain
    sd_keywords = {}
    for sd in subdomains:
        kw_path = KEYWORDS_DIR / f"{sd['slug']}.json"
        if not kw_path.exists():
            continue
        with open(kw_path, encoding="utf-8") as fh:
            keywords = json.load(fh)
        available = [kw for kw in keywords if not kw.get("is_used", False)]
        if available:
            sd_keywords[sd["slug"]] = available
    
    # Sort subdomains by count ascending, pick top N with available keywords
    sorted_sds = sorted(sd_counts.items(), key=lambda x: x[1])
    selected = []
    for sd_slug, _ in sorted_sds:
        if len(selected) >= count:
            break
        if sd_slug in sd_keywords and sd_keywords[sd_slug]:
            kw = random.choice(sd_keywords[sd_slug])
            selected.append(kw)
            sd_keywords[sd_slug].remove(kw)  # prevent double-pick in same run
    
    return selected
```

---

## 4. Idempotency & Race Condition Prevention

### 4.1 Risk

Two cron runs could theoretically overlap (e.g., GitHub scheduler catch-up after outage). Without safeguards, both runs could pick the same keyword.

### 4.2 Solution: Mark-Before-Generate

Shift from the current "generate → render → mark" order to "mark → generate → render":

1. Pick keywords → immediately `mark_used` with `generated_at: "pending"`
2. Generate article body
3. Render HTML
4. Update `generated_at` to real timestamp

```python
def mark_pending(keyword_entry):
    """Set is_used=True with pending marker — called BEFORE generation."""
    subdomain = keyword_entry["subdomain"]
    kw_path = KEYWORDS_DIR / f"{subdomain}.json"
    with open(kw_path, encoding="utf-8") as fh:
        keywords = json.load(fh)
    for kw in keywords:
        if kw["keyword"] == keyword_entry["keyword"]:
            kw["is_used"] = True
            kw["generated_at"] = "pending"
            break
    with open(kw_path, "w", encoding="utf-8") as fh:
        json.dump(keywords, fh, indent=2, ensure_ascii=False)

def mark_completed(keyword_entry):
    """Update generated_at to real timestamp — called AFTER successful render."""
    subdomain = keyword_entry["subdomain"]
    kw_path = KEYWORDS_DIR / f"{subdomain}.json"
    with open(kw_path, encoding="utf-8") as fh:
        keywords = json.load(fh)
    for kw in keywords:
        if kw["keyword"] == keyword_entry["keyword"]:
            kw["generated_at"] = datetime.now(timezone.utc).isoformat()
            break
    with open(kw_path, "w", encoding="utf-8") as fh:
        json.dump(keywords, fh, indent=2, ensure_ascii=False)
```

If generation fails: keyword remains `is_used=True` with `generated_at: "pending"`. This is acceptable — the keyword is consumed and won't be retried (avoids infinite retry loops on bad keywords). A future cleanup job could reset these.

### 4.3 Git Push Conflict

If two runs somehow overlap, the second run's git push will fail due to non-fast-forward. GitHub Actions `actions/checkout` with default settings handles this gracefully. The failed run logs the conflict and the next scheduled run picks different keywords.

---

## 5. Workflow Changes

### 5.1 `generate.yml` Diff

```yaml
name: Distributed Content Generation

on:
  schedule:
    - cron: '0 0,5,10,15,20 * * *'
    - cron: '30 2,7,12,17,22 * * *'
  workflow_dispatch:

concurrency: generate

jobs:
  generate:
    runs-on: ubuntu-latest
    timeout-minutes: 15           # reduced from 30
    permissions:
      contents: write

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install -r generate/requirements.txt

      - name: Generate content
        env:
          MISTRAL_API_KEY: ${{ secrets.MISTRAL_API_KEY }}
          NVIDIA_API_KEY: ${{ secrets.NVIDIA_API_KEY }}
        run: python generate/main.py --count 2

      - name: Commit and push
        run: |
          git config user.name "content-bot"
          git config user.email "bot@insurtechinsights.com"
          git add content/ keywords/
          git diff --staged --quiet || git commit -m "auto: content batch $(date +%Y-%m-%dT%H:%M:%S)"
          git push
```

Key changes from current:
- Removed `push` trigger
- Two cron expressions replacing single `0 2 * * *`
- `concurrency: generate` added
- `timeout-minutes` reduced from 30 to 15
- `--count` changed from 10 to 2
- Commit message includes timestamp (multiple runs/day)
- `git add` now includes `keywords/` (mark_used modifies keyword JSONs)

### 5.2 `main.py` Changes

| File | Change |
|------|--------|
| `generate/main.py` | Replace `pick_unused_keywords()` with load-balanced version |
| `generate/main.py` | Split `mark_used()` into `mark_pending()` + `mark_completed()` |
| `generate/main.py` | Reorder `main()`: pending → generate → render → completed |
| `config.yaml` | No changes needed (`articles_per_run` unused in new logic) |

---

## 6. No-Change Items

- `config.yaml` site/generation/adsense sections — no changes
- `templates/` — no changes
- `keywords/*.json` format — compatible as-is
- `generate/llm.py` — no changes
- `generate/requirements.txt` — no changes
- Cloudflare Pages deploy — no changes (still triggered by git push)

---

## 7. Rollback Plan

If issues arise (API rate limiting, keyword pool exhaustion too fast):

1. Change `--count 2` back to `--count 1` in workflow
2. Or revert to single daily cron by removing one cron expression and changing the other back to `0 2 * * *`
3. Commit and push — Cloudflare deploys automatically

---

## 8. Open Questions

- **Keyword pool size**: Current pool unknown. At 20 articles/day, a pool of 120 keywords lasts 6 days. Need to verify pool depth and plan replenishment.
- **Pending cleanup**: Keywords stuck in `"pending"` state (generation failed) should eventually be reset. Deferred to future automation or manual review.
*（内容由AI生成，仅供参考）*
