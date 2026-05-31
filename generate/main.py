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
KEYWORDS_DIR = ROOT / "keywords"
TEMPLATES_DIR = ROOT / "templates"


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Keyword Picker
# ---------------------------------------------------------------------------

def pick_unused_keywords(config, count: int):
    """Randomly pick *count* unused keywords across all subdomains."""
    subdomains = config["subdomains"]
    pool = []
    for sd in subdomains:
        kw_path = KEYWORDS_DIR / f"{sd['slug']}.json"
        if not kw_path.exists():
            logger.warning("Keyword file not found: %s", kw_path)
            continue
        with open(kw_path, encoding="utf-8") as fh:
            keywords = json.load(fh)
        for kw in keywords:
            if not kw.get("is_used", False):
                pool.append(kw)

    if len(pool) < count:
        logger.warning("Only %d unused keywords available (requested %d). Using all.", len(pool), count)
        count = len(pool)

    selected = random.sample(pool, count)
    return selected


def mark_used(keyword_entry):
    """Persist is_used=True + generated_at in the source JSON."""
    subdomain = keyword_entry["subdomain"]
    kw_path = KEYWORDS_DIR / f"{subdomain}.json"
    with open(kw_path, encoding="utf-8") as fh:
        keywords = json.load(fh)
    for kw in keywords:
        if kw["keyword"] == keyword_entry["keyword"]:
            kw["is_used"] = True
            kw["generated_at"] = datetime.now(timezone.utc).isoformat()
            break
    with open(kw_path, "w", encoding="utf-8") as fh:
        json.dump(keywords, fh, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Content Generation
# ---------------------------------------------------------------------------

CONTENT_LENGTH_RULES = {
    "tutorial":       (2000, 4000),
    "tool-review":    (800, 1500),
    "news":           (600, 1000),
    "comparison":     (1500, 2500),
    "explainer":      (1500, 3000),
    "case-study":     (1000, 2000),
    "how-to":         (1000, 2500),
}

SYSTEM_PROMPT = textwrap.dedent("""\
You are a senior insurance technology journalist writing for "Insurtech Insights", a publication covering AI in insurance.

ABSOLUTE RULES:
- Write as a human domain expert. Never use AI clichés: no "In today's rapidly evolving landscape", "delve into", "game-changer", "unlock the power of", "harness the potential", "in conclusion", "it is important to note that".
- Short sentences. Active voice. First-person perspective where natural ("I've seen claims teams..."). 
- Include one real trade-off, limitation, or risk per section. No puff pieces.
- Use industry jargon naturally: loss ratio, combined ratio, TPA, MGA, bordereaux, parametric trigger, STP, UW. Don't define basic terms.
- Take a stance. Don't hedge. If something is overhyped, say so.
- Cite specific companies, dollar figures, or percentages where relevant.
- No summary/conclusion paragraph at the end unless the content type absolutely requires it.

FORMAT:
- Output in raw HTML suitable for direct insertion into a Jinja2 {{ content }} block.
- Use <h2>, <h3>, <p>, <ul>/<li>, <table> as needed.
- Do NOT include <!DOCTYPE>, <html>, <head>, <body> tags.
- Do NOT wrap in ```html or any code fence.
- Word count: adhere strictly to the range specified.""")

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
    min_words, max_words = CONTENT_LENGTH_RULES.get(content_type, (800, 2000))
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
    accumulated = []
    rest_accumulated = []
    found_split = False

    for i, para in enumerate(paragraphs):
        if not found_split and i >= split_idx:
            first = html_body[:html_body.index(para)]
            rest = html_body[html_body.index(para):]
            return first.strip(), rest.strip()
        accumulated.append(para)

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
    """Scan content/ for all published articles."""
    articles = []
    for sf in CONTENT_DIR.iterdir():
        if not sf.is_dir():
            continue
        for af in sf.iterdir():
            if af.is_dir() and (af / "index.html").exists():
                html = (af / "index.html").read_text(encoding="utf-8")
                title = extract_title(html)
                desc = generate_description(html)
                articles.append({
                    "url": f"/{sf.name}/{af.name}/",
                    "title": title,
                    "description": desc,
                    "subdomain": sf.name,
                    "slug": af.name,
                    "subdomain_name": sf.name.replace("-", " ").title(),
                    "date_display": ""  # approximate; we store timestamps in keyword JSON
                })
    articles.sort(key=lambda a: a["url"], reverse=True)
    return articles


def rebuild_home(config) -> None:
    """Rebuild /content/index.html with one article per subdomain + one extra."""
    import random as _random
    _random.seed(42)  # deterministic per build
    jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    all_articles = _collect_all_articles()

    # Group articles by subdomain
    by_sd = {}
    for a in all_articles:
        by_sd.setdefault(a["subdomain"], []).append(a)

    # Pick one random article per subdomain
    picked = []
    for sd_slug in sorted(by_sd.keys()):
        picked.append(_random.choice(by_sd[sd_slug]))

    _random.shuffle(picked)

    adsense = config.get("adsense", {})
    pub_id = adsense.get("pub_id", "")
    ad_slots = adsense.get("ad_units", {})

    html = jinja_env.get_template("home.html").render(
        site_name=config["site"]["name"],
        subdomains=config["subdomains"],
        current_year=datetime.now(timezone.utc).year,
        articles=picked,
        canonical_url="/",
        adsense_pub_id=pub_id or None,
        ad_slot_top=ad_slots.get("top_banner", {}).get("slot", ""),
        ga_id=config.get("analytics", {}).get("ga_id", ""),
    )
    (CONTENT_DIR / "index.html").write_text(html, encoding="utf-8")
    logger.info("Rebuilt home page with %d articles.", len(articles[:20]))


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
    """Generate index.html for each subdomain category."""
    jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    all_articles = _collect_all_articles()
    adsense = config.get("adsense", {})
    pub_id = adsense.get("pub_id", "")
    ad_slots = adsense.get("ad_units", {})

    for sd in config["subdomains"]:
        slug = sd["slug"]
        cat_articles = [a for a in all_articles if a["url"].startswith(f"/{slug}/")]
        html = jinja_env.get_template("home.html").render(
            site_name=f"{sd['name']} — {config['site']['name']}",
            subdomains=config["subdomains"],
            current_year=datetime.now(timezone.utc).year,
            articles=cat_articles[:20],
            canonical_url=f"/{slug}/",
            adsense_pub_id=pub_id or None,
            ad_slot_top=ad_slots.get("top_banner", {}).get("slot", ""),
            ga_id=config.get("analytics", {}).get("ga_id", ""),
        )
        out_dir = CONTENT_DIR / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text(html, encoding="utf-8")
        logger.info("Rebuilt category page: /%s/ (%d articles)", slug, len(cat_articles[:20]))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Insurtech Insights Content Generator")
    parser.add_argument("--count", type=int, default=10, help="Number of articles to generate")
    args = parser.parse_args()

    config = load_config()
    logger.info("Site: %s | Articles requested: %d", config["site"]["name"], args.count)

    # Pick keywords
    selected = pick_unused_keywords(config, args.count)
    if not selected:
        logger.error("No unused keywords remaining. Add more to keywords/*.json")
        sys.exit(1)
    logger.info("Selected %d keywords from pool.", len(selected))

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

        mark_used(kw)
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