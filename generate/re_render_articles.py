"""
Re-render all existing articles with updated article.html template.
Adds author byline and editorial note to every article page.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from html.parser import HTMLParser

import yaml
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = ROOT / "content"
TEMPLATES_DIR = ROOT / "templates"

VOID_TAGS = {'br', 'hr', 'img', 'input', 'meta', 'link', 'area', 'base', 'col',
             'embed', 'source', 'track', 'wbr', 'command', 'keygen', 'param'}


class TagBalancer(HTMLParser):
    """Parse HTML fragment and re-emit with balanced tags."""
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.output = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in VOID_TAGS:
            self.output.append(self.get_starttag_text())
            return
        self.stack.append(tag.lower())
        self.output.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        tag_lower = tag.lower()
        found = -1
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i] == tag_lower:
                found = i
                break
        if found >= 0:
            while len(self.stack) > found:
                popped = self.stack.pop()
                self.output.append(f'</{popped}>')

    def handle_data(self, data):
        self.output.append(data)

    def handle_comment(self, data):
        self.output.append(f'<!--{data}-->')

    def handle_entityref(self, name):
        self.output.append(f'&{name};')

    def handle_charref(self, name):
        self.output.append(f'&#{name};')

    def close_remaining(self):
        while self.stack:
            tag = self.stack.pop()
            self.output.append(f'</{tag}>')


def fix_html_body(html_body: str) -> str:
    """Fix HTML body: fix malformed tags, strip divs, balance all tags."""
    # Fix malformed closing tags like </p\n (missing >)
    html_body = re.sub(r'</(p|li|td|th|tr|pre|code|h[1-6])\s*\n', r'</\1>\n', html_body)

    # Strip all div tags — LLM shouldn't generate these
    html_body = re.sub(r'<div\b[^>]*>', '', html_body, flags=re.IGNORECASE)
    html_body = re.sub(r'</div\s*>', '', html_body, flags=re.IGNORECASE)

    # Balance all remaining tags
    parser = TagBalancer()
    try:
        parser.feed(html_body)
        parser.close_remaining()
        return ''.join(parser.output)
    except Exception:
        return html_body


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def extract_title(html_content: str) -> str:
    m = re.search(r"<h[12]>(.+?)</h[12]>", html_content)
    if m:
        return m.group(1).strip()
    return "Untitled"


def extract_description(html_content: str, max_chars: int = 160) -> str:
    m = re.search(r"<p>(.+?)</p>", html_content)
    if m:
        desc = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        return desc[:max_chars].rsplit(" ", 1)[0] + ("..." if len(desc) > max_chars else "")
    return ""


def extract_article_body(html_text: str) -> str:
    """Extract the raw content from within <div class="article-content">...</div>"""
    start_marker = '<div class="article-content">'
    start_idx = html_text.find(start_marker)
    if start_idx == -1:
        return ""

    start_idx += len(start_marker)

    end_markers = [
        '</article>',
        '<div class="editorial-note"',
        '<section class="related-section"',
        '<div class="ad-slot">AD – Responsive Bottom',
    ]

    end_idx = len(html_text)
    for marker in end_markers:
        idx = html_text.find(marker, start_idx)
        if idx != -1 and idx < end_idx:
            end_idx = idx

    body = html_text[start_idx:end_idx].strip()

    # Remove the entire mid-content ad block (wrapper div + all contents)
    body = re.sub(
        r'<!-- Ad Mid-Content -->\s*<div class="ad-slot"[^>]*>.*?</div>',
        '',
        body,
        flags=re.DOTALL,
    )

    # Remove any remaining ad scripts/blocks
    body = re.sub(
        r'<ins class="adsbygoogle"[^>]*>.*?</ins>\s*<script>\s*\(adsbygoogle[^)]*\)[^<]*</script>',
        '',
        body,
        flags=re.DOTALL,
    )

    # Strip all trailing </div> (closing article-content and any artifacts)
    while body.rstrip().endswith('</div>'):
        body = body.rstrip()[:-len('</div>')].rstrip()

    # Fix malformed tags, strip stray divs, balance all tags
    body = fix_html_body(body)

    return body.strip()


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


def main():
    config = load_config()
    jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))

    # Load index.json for metadata
    index_path = CONTENT_DIR / "index.json"
    index_lookup = {}
    if index_path.exists():
        entries = json.loads(index_path.read_text(encoding="utf-8"))
        for e in entries:
            url = e.get("url", "")
            index_lookup[url] = {
                "title": e.get("title", ""),
                "description": e.get("description", ""),
                "date_display": e.get("date_display", ""),
                "keyword": e.get("keyword", ""),
            }

    sd_names = {sd["slug"]: sd["name"] for sd in config["subdomains"]}
    adsense = config.get("adsense", {})
    pub_id = adsense.get("pub_id", "")
    ad_slots = adsense.get("ad_units", {})
    now = datetime.now(timezone.utc)

    # Articles with a refreshed dateModified to signal ongoing maintenance
    DATE_MODIFIED_UPDATED = "2026-06-05T00:00:00+00:00"
    UPDATED_SLUGS = {
        "/ai-claims/can-ai-driven-claims-processing-cut-your-loss-ratio-by-15-or-just-move-the-fraud/",
        "/ai-claims/insurance-claims-ai-roi-calculation-framework-a-practitioners-step-by-step-guide/",
        "/ai-claims/cut-claims-cycle-time-by-40-a-step-by-step-ai-playbook/",
        "/ai-underwriting/is-your-underwriting-data-still-stuck-in-the-1990s/",
        "/ai-underwriting/how-hiscox-slashed-quote-cycle-time-by-99-with-ai-and-what-it-cost-them/",
        "/ai-fraud-detection/can-image-recognition-ai-really-stop-40-billion-in-annual-insurance-fraud/",
        "/ai-fraud-detection/claims-fraud-scoring-ai-model-development-guide/",
        "/ai-policy-cx/omnichannel-insurance-cx-orchestration-a-practitioners-implementation-guide/",
        "/embedded-insurance/global-regulators-are-circling-embedded-insurance-and-its-not-pretty/",
        "/decision-intelligence/insurance-ai-talent-build-vs-buy-vs-borrow-vs-bypass/",
    }
    success = 0
    skipped = 0

    for sf in CONTENT_DIR.iterdir():
        if not sf.is_dir():
            continue
        subdomain = sf.name
        subdomain_name = sd_names.get(subdomain, subdomain.replace("-", " ").title())

        for af in sf.iterdir():
            if not af.is_dir():
                continue
            article_file = af / "index.html"
            if not article_file.exists():
                continue

            slug = af.name
            url = f"/{subdomain}/{slug}/"

            # Read existing rendered HTML
            old_html = article_file.read_text(encoding="utf-8")

            # Extract body content
            body = extract_article_body(old_html)
            if not body:
                print(f"  SKIP {url}: could not extract article body")
                skipped += 1
                continue

            # Get metadata from index.json
            meta = index_lookup.get(url, {})
            title = meta.get("title") or extract_title(body)
            description = meta.get("description") or extract_description(body)
            date_display = meta.get("date_display", "")
            keyword = meta.get("keyword", slug.replace("-", " "))

            # Split content
            content_first, content_rest = split_content_at_third(body)
            full_content = body

            # Build date_iso from date_display if possible
            date_iso = ""
            if date_display:
                try:
                    dt = datetime.strptime(date_display, "%B %d, %Y")
                    date_iso = dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                except ValueError:
                    date_iso = ""

            # Re-render with updated template
            date_modified_iso = DATE_MODIFIED_UPDATED if url in UPDATED_SLUGS else date_iso
            html = jinja_env.get_template("article.html").render(
                site_name=config["site"]["name"],
                subdomains=config["subdomains"],
                current_year=now.year,
                current_date=date_display or now.strftime("%B %d, %Y"),
                title=title,
                description=description,
                keyword=keyword,
                content=full_content,
                content_first=content_first,
                content_rest=content_rest,
                date_iso=date_iso,
                date_modified_iso=date_modified_iso,
                date_display=date_display,
                subdomain=subdomain,
                subdomain_name=subdomain_name,
                adsense_pub_id=pub_id or None,
                ad_slot_top=ad_slots.get("top_banner", {}).get("slot", ""),
                ad_slot_in=ad_slots.get("in_content", {}).get("slot", ""),
                ad_slot_bottom=ad_slots.get("bottom", {}).get("slot", ""),
                canonical_url=url,
                ga_id=config.get("analytics", {}).get("ga_id", ""),
            )

            article_file.write_text(html, encoding="utf-8")
            print(f"  OK  {url} -> {title[:60]}")
            success += 1

    print(f"\nDone: {success} articles re-rendered, {skipped} skipped.")


if __name__ == "__main__":
    main()
