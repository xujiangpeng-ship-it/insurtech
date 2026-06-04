"""
Insurtech Insights — Content Generation Pipeline
Usage: python generate/main.py [--count N]
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

from llm import generate_text

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("main")

ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = ROOT / "content"

CATEGORY_HERO = {
    "ai-claims": {
        "badge": "Claims Automation",
        "heading": "AI Claims<br>Intelligence",
        "subtitle": "From FNOL to settlement — how machine learning is rewriting the claims playbook. Real implementations, real results."
    },
    "ai-underwriting": {
        "badge": "Underwriting Innovation",
        "heading": "AI Underwriting<br>Insights",
        "subtitle": "Risk assessment at machine speed. In-depth analysis of automated underwriting engines and their impact on loss ratios."
    },
    "ai-fraud-detection": {
        "badge": "Fraud Prevention",
        "heading": "AI Fraud Detection<br>Deep Dives",
        "subtitle": "Catching what humans miss. Technical breakdowns of anomaly detection, network analysis, and predictive fraud scoring."
    },
    "embedded-insurance": {
        "badge": "Embedded Insurance",
        "heading": "Embedded Insurance<br>Frontier",
        "subtitle": "Insurance where customers already are — inside platforms, checkout flows, and digital ecosystems. The architecture and economics."
    },
    "ai-policy-cx": {
        "badge": "Policy & CX",
        "heading": "AI-Powered Policy<br>& Customer Experience",
        "subtitle": "From chatbots to hyper-personalization — how AI is transforming policy administration and customer retention."
    },
    "decision-intelligence": {
        "badge": "Decision Science",
        "heading": "Decision Intelligence<br>for Insurance",
        "subtitle": "Data strategy, analytics maturity, and the organizational transformation behind AI adoption in insurance."
    }
}
KEYWORDS_DIR = ROOT / "keywords"
TEMPLATES_DIR = ROOT / "templates"


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Keyword Picker
# ---------------------------------------------------------------------------

def pick_unused_keywords(config, count: int):
    """Pick *count* unused keywords using load-balanced subdomain selection.
    
    Prioritizes subdomains with the fewest published articles to ensure even
    content distribution across all categories.
    """
    subdomains = config["subdomains"]

    # Count existing articles per subdomain
    sd_counts = {}
    for sd in subdomains:
        sd_dir = CONTENT_DIR / sd["slug"]
        if sd_dir.is_dir():
            sd_counts[sd["slug"]] = sum(1 for d in sd_dir.iterdir() if d.is_dir())
        else:
            sd_counts[sd["slug"]] = 0

    # Load available (unused) keywords per subdomain
    sd_keywords = {}
    for sd in subdomains:
        kw_path = KEYWORDS_DIR / f"{sd['slug']}.json"
        if not kw_path.exists():
            logger.warning("Keyword file not found: %s", kw_path)
            continue
        with open(kw_path, encoding="utf-8") as fh:
            keywords = json.load(fh)
        available = [kw for kw in keywords if not kw.get("is_used", False)]
        if available:
            sd_keywords[sd["slug"]] = available

    if not sd_keywords:
        logger.error("No unused keywords available in any subdomain.")
        return []

    # Sort subdomains by article count ascending (fewest first)
    sorted_sds = sorted(sd_counts.items(), key=lambda x: x[1])
    selected = []
    for sd_slug, _ in sorted_sds:
        if len(selected) >= count:
            break
        if sd_slug in sd_keywords and sd_keywords[sd_slug]:
            kw = random.choice(sd_keywords[sd_slug])
            selected.append(kw)
            sd_keywords[sd_slug].remove(kw)  # prevent double-pick within same run

    if len(selected) < count:
        logger.warning(
            "Only %d keywords available across all subdomains (requested %d).",
            len(selected), count
        )

    return selected


def mark_pending(keyword_entry):
    """Set is_used=True with pending marker — called BEFORE generation to
    prevent concurrent runs from picking the same keyword."""
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


# ---------------------------------------------------------------------------
# Content Generation
# ---------------------------------------------------------------------------

CONTENT_LENGTH_RULES = {
    "tutorial":       (2000, 4000),
    "tool-review":    (1200, 2000),
    "news":           (1200, 1800),
    "comparison":     (1500, 2500),
    "explainer":      (1500, 3000),
    "case-study":     (1200, 2500),
    "how-to":         (1200, 2500),
}

SYSTEM_PROMPT = textwrap.dedent("""\
You are a senior insurance technology analyst writing for "Insurtech Insights" — a Gartner/Forrester-caliber publication covering AI in insurance. Your tone: confident, direct, data-driven, skeptical where warranted.

ABSOLUTE RULES:
- Write as a human domain expert. Never use AI clichés: no "In today's rapidly evolving landscape", "In today's digital age", "in the world of", "delve into", "game-changer", "game changer", "revolutionary", "unlock the power of", "harness the power of", "harness the potential", "As we all know", "it is important to note that", "in conclusion", "cutting-edge", "game-changing", "paradigm shift".
- No adjective stacking. One adjective per noun, two max if both are precise and necessary.
- Short sentences. Active voice. First-person perspective where natural ("I've seen claims teams...", "I've reviewed dozens of..."). Avoid passive constructions like "it can be observed that" or "it has been found that".
- Include at least one real trade-off, limitation, or risk per major section. No puff pieces.
- Use industry jargon naturally: loss ratio, combined ratio, TPA, MGA, bordereaux, parametric trigger, STP, UW, FNOL, LR, COR. Don't define basic terms — your readers are insurance professionals.
- Take a stance. Don't hedge. If something is overhyped, say so explicitly. Say "this vendor's claims are inflated by 40%" not "some may question the accuracy."
- No summary/conclusion paragraph at the end. End on a specific forward-looking observation, hard question, or actionable next step — not a "key takeaways" recap.

OPENING REQUIREMENT:
- Every article MUST start with one of: a specific dollar figure or percentage, a named company's specific result, a regulatory event with date, or a contrarian claim that challenges conventional wisdom.
- Never open with a rhetorical question, a broad industry observation, or "In the world of insurance...".

DATA & STRUCTURE REQUIREMENTS:
- Cite at least 2 specific data sources per article with the organization name and year (e.g., "McKinsey's 2024 Global Insurance Report", "NAIC 2023 market conduct data", "Swiss Re sigma 02/2024"). When possible, cite the exact report or study name.
- Include at least 1 comparison table (<table>) with minimum 4 rows and 4 columns. Tables must compare specific vendors, frameworks, metrics, or approaches — not generic pros/cons.
- Mix paragraph lengths: some 1-2 sentence paragraphs for impact, some 4-5 sentence paragraphs for depth.
- Use <h2> for major sections (4-6 per article), <h3> for sub-sections within each H2.

FORMAT:
- Output in raw HTML suitable for direct insertion into a Jinja2 {{ content }} block.
- Use <h2>, <h3>, <p>, <ul>/<li>, <table> as needed. Tables should use <thead>/<tbody>/<th>/<td>.
- Do NOT include <!DOCTYPE>, <html>, <head>, <body> tags.
- Do NOT wrap in ```html or any code fence.
- Word count: adhere strictly to the range specified. Minimum 1200 words for all content types.""")

TYPE_INSTRUCTIONS = {
    "tutorial": "Write a step-by-step implementation guide. Include numbered steps, code snippets or config examples where relevant, and a realistic resource estimate. Target: practitioner who will actually build this.",
    "tool-review": "Write a hands-on tool review. Cover: what it does, pricing, setup experience, what it does well, where it falls short. No star ratings — qualitative only.",
    "news": "Write a news analysis piece. Lead with the event, then provide context, market reaction, and a contrarian take. Short, punchy.",
    "comparison": "Build a comparison table (<table>) of 4-6 options, then analyze trade-offs. Explicitly recommend which to pick for which scenario.",
    "explainer": "Explain a complex concept to a mid-level insurance professional. Assume domain knowledge — skip the basics. Lead with a provocative question or stat.",
    "case-study": "Profile a real company's implementation. Structure: Background → Challenge → Solution → Results → Lessons Learned. Use actual numbers.",
    "how-to": "Practical, actionable guide. Start with the end result, then show exactly how to get there. Include pitfalls and shortcuts.",
}


def build_user_prompt(keyword: str, content_type: str) -> str:
    min_words, max_words = CONTENT_LENGTH_RULES.get(content_type, (1200, 2500))
    type_instruction = TYPE_INSTRUCTIONS.get(content_type, TYPE_INSTRUCTIONS["explainer"])

    return textwrap.dedent(f"""\
Content Type: {content_type}
Target Keyword: {keyword}
Word Count Range: {min_words}–{max_words} words

{type_instruction}

Generate the article now. Start directly with the HTML content — no preamble, no meta-commentary, no code fences.""")


def generate_article(keyword_entry, config) -> str:
    """Generate HTML body content for one article."""
    gen_cfg = config["generation"]

    raw = generate_text(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=build_user_prompt(keyword_entry["keyword"], keyword_entry["type"]),
        temperature=gen_cfg.get("temperature", 0.7),
        max_tokens=gen_cfg.get("max_tokens", 4096),
        retry_attempts=gen_cfg.get("retry_attempts", 3),
        retry_delays=gen_cfg.get("retry_delay_seconds", [5, 15, 30]),
    )
    return raw


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

def extract_title(html_content: str) -> str:
    """Extract first <h[12]> as title."""
    m = re.search(r"<h[12]>(.+?)</h[12]>", html_content)
    if m:
        return m.group(1).strip()
    return "Untitled"


def generate_description(html_content: str, max_chars: int = 160) -> str:
    """Extract first meaningful <p> as meta description."""
    m = re.search(r"<p>(.+?)</p>", html_content)
    if m:
        desc = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        return desc[:max_chars].rsplit(" ", 1)[0] + ("..." if len(desc) > max_chars else "")
    return ""


def make_slug(title: str) -> str:
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:80].strip("-")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def split_content_at_third(html_body: str) -> tuple:
    """Split HTML content at approximately 1/3 mark, at a paragraph boundary."""
    paragraphs = re.findall(r'<p>.*?</p>', html_body, re.DOTALL)
    if len(paragraphs) < 4:
        return html_body, ""

    split_idx = max(1, len(paragraphs) // 3)

    search_start = 0
    for i, para in enumerate(paragraphs):
        idx = html_body.find(para, search_start)
        if idx < 0:
            return html_body, ""
        if i >= split_idx:
            first = html_body[:idx]
            rest = html_body[idx:]
            return first.strip(), rest.strip()
        search_start = idx + len(para)

    return html_body, ""


def render_article(config, keyword_entry, html_body: str) -> Path:
    """Render one article to content/{subdomain}/{slug}/index.html"""
    jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))

    title = extract_title(html_body)
    description = generate_description(html_body)
    slug = make_slug(title)
    subdomain = keyword_entry["subdomain"]
    sd_names = {sd["slug"]: sd["name"] for sd in config["subdomains"]}
    subdomain_name = sd_names.get(subdomain, subdomain.replace("-", " ").title())

    now = datetime.now(timezone.utc)
    date_iso = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    date_display = now.strftime("%B %d, %Y")

    content_first, content_rest = split_content_at_third(html_body)

    adsense = config.get("adsense", {})
    pub_id = adsense.get("pub_id", "")
    ad_slots = adsense.get("ad_units", {})

    html = jinja_env.get_template("article.html").render(
        site_name=config["site"]["name"],
        subdomains=config["subdomains"],
        current_year=now.year,
        title=title,
        description=description,
        keyword=keyword_entry["keyword"],
        content_first=content_first,
        content_rest=content_rest,
        date_iso=date_iso,
        date_display=date_display,
        subdomain=subdomain,
        subdomain_name=subdomain_name,
        adsense_pub_id=pub_id or None,
        ad_slot_top=ad_slots.get("top_banner", {}).get("slot", ""),
        ad_slot_in=ad_slots.get("in_content", {}).get("slot", ""),
        ad_slot_bottom=ad_slots.get("bottom", {}).get("slot", ""),
        canonical_url=f"/{subdomain}/{slug}/",
        related_articles=None,
        ga_id=config.get("analytics", {}).get("ga_id", ""),
    )

    out_dir = CONTENT_DIR / subdomain / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "index.html"
    out_file.write_text(html, encoding="utf-8")

    logger.info("Rendered: %s", out_file)
    return out_file, slug, title, description, subdomain_name, date_display


# ---------------------------------------------------------------------------
# Index & Sitemap
# ---------------------------------------------------------------------------

def _collect_all_articles() -> list:
    """Scan content/ for all published articles, enriched with index.json metadata."""
    # Build lookup from index.json for date_display and generated_at
    index_lookup = {}
    index_path = CONTENT_DIR / "index.json"
    if index_path.exists():
        entries = json.loads(index_path.read_text(encoding="utf-8"))
        for e in entries:
            url = e.get("url", "")
            index_lookup[url] = {
                "date_display": e.get("date_display", ""),
                "generated_at": e.get("generated_at", ""),
            }

    articles = []
    for sf in CONTENT_DIR.iterdir():
        if not sf.is_dir():
            continue
        for af in sf.iterdir():
            if af.is_dir() and (af / "index.html").exists():
                html = (af / "index.html").read_text(encoding="utf-8")
                title = extract_title(html)
                desc = generate_description(html)
                url = f"/{sf.name}/{af.name}/"
                meta = index_lookup.get(url, {})
                articles.append({
                    "url": url,
                    "title": title,
                    "description": desc,
                    "subdomain": sf.name,
                    "slug": af.name,
                    "subdomain_name": sf.name.replace("-", " ").title(),
                    "date_display": meta.get("date_display", ""),
                    "generated_at": meta.get("generated_at", ""),
                })
    articles.sort(key=lambda a: a["generated_at"] or a["url"], reverse=True)
    return articles


def rebuild_home(config) -> None:
    """Rebuild /content/index.html with one article per subdomain + one extra (7 total)."""
    import random as _random
    _random.seed(42)  # deterministic per build
    jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    all_articles = _collect_all_articles()
    total_count = len(all_articles)

    # Group articles by subdomain
    by_sd = {}
    for a in all_articles:
        by_sd.setdefault(a["subdomain"], []).append(a)

    # Pick one random article per subdomain (6 total)
    picked = []
    for sd_slug in sorted(by_sd.keys()):
        picked.append(_random.choice(by_sd[sd_slug]))

    # Pick one extra random article from any subdomain (different from the 6)
    picked_urls = {a["url"] for a in picked}
    remaining = [a for a in all_articles if a["url"] not in picked_urls]
    if remaining:
        picked.append(_random.choice(remaining))

    _random.shuffle(picked)

    adsense = config.get("adsense", {})
    pub_id = adsense.get("pub_id", "")

    html = jinja_env.get_template("home.html").render(
        site_name=config["site"]["name"],
        subdomains=config["subdomains"],
        current_year=datetime.now(timezone.utc).year,
        articles=picked,
        canonical_url="/",
        adsense_pub_id=pub_id or None,
        ad_slot_top="",
        ga_id=config.get("analytics", {}).get("ga_id", ""),
        is_category_page=False,
        show_hero_ad=False,
        current_category_slug="",
        hero_badge_text="Sharp Insights Provided",
        hero_heading="AI meets Insurance<br>Technology",
        hero_subtitle_text="In-depth coverage of how artificial intelligence is reshaping insurance — from claims automation to underwriting intelligence.",
        total_articles_count=total_count,
        section_title="Most Viewed Articles",
        pagination=None,
    )
    (CONTENT_DIR / "index.html").write_text(html, encoding="utf-8")
    logger.info("Rebuilt home page with %d articles (total: %d).", len(picked), total_count)


def rebuild_sitemap(config) -> None:
    """Rebuild /content/sitemap.xml."""
    domain = config["site"].get("domain", "YOUR_DOMAIN")
    articles = _collect_all_articles()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    lines.append(f"  <url><loc>https://{domain}/</loc><lastmod>{now}</lastmod><priority>1.0</priority></url>")
    # Static pages
    for page in ["about", "contact", "privacy", "terms"]:
        lines.append(f"  <url><loc>https://{domain}/{page}/</loc><lastmod>{now}</lastmod><priority>0.5</priority></url>")
    # Subdomain category pages
    for sd in config["subdomains"]:
        lines.append(f"  <url><loc>https://{domain}/{sd['slug']}/</loc><lastmod>{now}</lastmod><priority>0.8</priority></url>")
    # Articles
    for art in articles:
        lines.append(f"  <url><loc>https://{domain}{art['url']}</loc><lastmod>{now}</lastmod><priority>0.9</priority></url>")
    lines.append("</urlset>")

    (CONTENT_DIR / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("Rebuilt sitemap.xml with %d URLs.", len(articles) + 5 + len(config["subdomains"]))


def rebuild_index_json(articles_meta: list) -> None:
    """Append new articles to index.json for future related-article linking."""
    index_path = CONTENT_DIR / "index.json"
    existing = []
    if index_path.exists():
        existing = json.loads(index_path.read_text(encoding="utf-8"))
    existing.extend(articles_meta)
    index_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Category index pages
# ---------------------------------------------------------------------------

def rebuild_category_pages(config) -> None:
    """Generate index.html (and pageN.html) for each subdomain category, sorted by date."""
    import math
    jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    all_articles = _collect_all_articles()
    total_count = len(all_articles)
    adsense = config.get("adsense", {})
    pub_id = adsense.get("pub_id", "")
    ad_slots = adsense.get("ad_units", {})
    PAGE_SIZE = 10

    for sd in config["subdomains"]:
        slug = sd["slug"]
        cat_articles = [a for a in all_articles if a["url"].startswith(f"/{slug}/")]
        # Already sorted by generated_at from _collect_all_articles
        cat_articles.sort(key=lambda a: a["generated_at"] or "", reverse=True)

        hero = CATEGORY_HERO.get(slug, {
            "badge": sd.get("name", slug),
            "heading": sd.get("name", slug),
            "subtitle": ""
        })

        base_url = f"/{slug}/"
        total_pages = max(1, math.ceil(len(cat_articles) / PAGE_SIZE))

        for page in range(1, total_pages + 1):
            start = (page - 1) * PAGE_SIZE
            end = start + PAGE_SIZE
            page_articles = cat_articles[start:end]

            pagination = {
                "current_page": page,
                "total_pages": total_pages,
                "base_url": base_url,
            }

            html = jinja_env.get_template("home.html").render(
                site_name=f"{sd['name']} — {config['site']['name']}",
                subdomains=config["subdomains"],
                current_year=datetime.now(timezone.utc).year,
                articles=page_articles,
                canonical_url=f"/{slug}/",
                adsense_pub_id=pub_id or None,
                ad_slot_top=ad_slots.get("top_banner", {}).get("slot", ""),
                ga_id=config.get("analytics", {}).get("ga_id", ""),
                is_category_page=True,
                show_hero_ad=True,
                current_category_slug=slug,
                hero_badge_text=hero["badge"],
                hero_heading=hero["heading"],
                hero_subtitle_text=hero["subtitle"],
                total_articles_count=len(cat_articles),
                section_title="Latest Articles",
                pagination=pagination,
            )

            out_dir = CONTENT_DIR / slug
            out_dir.mkdir(parents=True, exist_ok=True)
            if page == 1:
                (out_dir / "index.html").write_text(html, encoding="utf-8")
            else:
                (out_dir / f"page{page}.html").write_text(html, encoding="utf-8")

        logger.info("Rebuilt category page: /%s/ (%d articles, %d pages)", slug, len(cat_articles), total_pages)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Insurtech Insights Content Generator")
    parser.add_argument("--count", type=int, default=1, help="Number of articles to generate")
    args = parser.parse_args()

    config = load_config()
    logger.info("Site: %s | Articles requested: %d", config["site"]["name"], args.count)

    # Pick keywords
    selected = pick_unused_keywords(config, args.count)
    if not selected:
        logger.error("No unused keywords remaining. Add more to keywords/*.json")
        sys.exit(1)
    logger.info("Selected %d keywords from pool.", len(selected))

    # Mark all selected keywords as pending BEFORE generation
    # to prevent concurrent runs from picking the same keywords
    for kw in selected:
        mark_pending(kw)

    articles_meta = []

    for i, kw in enumerate(selected, 1):
        logger.info("[%d/%d] Generating: %s (%s)", i, len(selected), kw["keyword"], kw["type"])
        try:
            html_body = generate_article(kw, config)
        except Exception as exc:
            logger.error("Failed to generate '%s': %s", kw["keyword"], exc)
            continue

        try:
            out_file, slug, title, description, sd_name, date_display = render_article(config, kw, html_body)
        except Exception as exc:
            logger.error("Failed to render '%s': %s", kw["keyword"], exc)
            continue

        mark_completed(kw)
        articles_meta.append({
            "keyword": kw["keyword"],
            "type": kw["type"],
            "subdomain": kw["subdomain"],
            "url": f"/{kw['subdomain']}/{slug}/",
            "title": title,
            "description": description,
            "subdomain_name": sd_name,
            "date_display": date_display,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })

    # Post-run: rebuild infrastructure
    if articles_meta:
        rebuild_index_json(articles_meta)
        rebuild_home(config)
        rebuild_sitemap(config)
        rebuild_category_pages(config)

    logger.info("Done. Generated %d/%d articles successfully.", len(articles_meta), len(selected))


if __name__ == "__main__":
    main()