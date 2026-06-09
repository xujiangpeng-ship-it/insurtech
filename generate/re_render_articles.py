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

# PIL for image dimension retrieval (WebP width/height injection)
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

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


DEFAULT_IMG_W = 800
DEFAULT_IMG_H = 450


def get_image_dimensions(src: str) -> tuple:
    """Resolve img src to local file, return (width, height) or defaults."""
    if not HAS_PIL:
        return DEFAULT_IMG_W, DEFAULT_IMG_H
    # Resolve absolute-relative paths like /images/logo.webp
    if src.startswith("/"):
        img_path = CONTENT_DIR / src.lstrip("/")
    elif src.startswith("http"):
        return DEFAULT_IMG_W, DEFAULT_IMG_H
    else:
        img_path = CONTENT_DIR / src
    try:
        if img_path.exists():
            with Image.open(str(img_path)) as img:
                return img.size  # (width, height)
    except Exception:
        pass
    return DEFAULT_IMG_W, DEFAULT_IMG_H


def fix_html_body(html_body: str) -> str:
    """Fix HTML body: fix malformed tags, strip divs, balance all tags, add external link attrs, lazy-load imgs."""
    # Fix malformed closing tags like </p\n (missing >)
    html_body = re.sub(r'</(p|li|td|th|tr|pre|code|h[1-6])\s*\n', r'</\1>\n', html_body)

    # Strip all div tags — LLM shouldn't generate these
    html_body = re.sub(r'<div\b[^>]*>', '', html_body, flags=re.IGNORECASE)
    html_body = re.sub(r'</div\s*>', '', html_body, flags=re.IGNORECASE)

    # Add target="_blank" rel="noopener noreferrer" to external links (http*), skip internal (/)
    def _fix_external_link(m):
        tag = m.group(0)
        if 'target=' in tag:
            return tag  # already has target
        return tag.replace('<a ', '<a target="_blank" rel="noopener noreferrer" ')

    html_body = re.sub(r'<a\s[^>]*href="https?://[^"]*"[^>]*>', _fix_external_link, html_body)

    # Add loading="lazy" and width/height to img tags
    def _fix_img(m):
        tag = m.group(0)
        src_match = re.search(r'src="([^"]*)"', tag)
        if not src_match:
            return tag
        src = src_match.group(1)
        # Add loading="lazy" if missing
        if 'loading=' not in tag.lower():
            tag = tag.replace('<img ', '<img loading="lazy" ')
        # Add width/height if missing
        if 'width=' not in tag.lower() or 'height=' not in tag.lower():
            w, h = get_image_dimensions(src)
            # Remove existing width/height if present (partial)
            tag = re.sub(r'\s(width|height)="[^"]*"', '', tag)
            tag = tag.replace('<img ', f'<img width="{w}" height="{h}" ')
        return tag

    html_body = re.sub(r'<img\s[^>]*>', _fix_img, html_body)

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


def extract_faq_schema(body_html: str, max_qa: int = 5) -> str:
    """Extract Q&A pairs from HTML body and return FAQPage JSON-LD string.

    Scans for h2/h3 headings ending with '?' as questions, then extracts
    the following paragraph(s) up to the next heading as answers.
    Returns empty string if no Q&A pairs found.
    """
    if not body_html:
        return ""

    qa_pairs = []

    # Find all h2/h3 headings that end with a question mark
    heading_re = re.compile(
        r'<h([23])\b[^>]*>(.*?\?)</h\1>',
        re.DOTALL | re.IGNORECASE,
    )

    matches = list(heading_re.finditer(body_html))
    if not matches:
        return ""

    for i, m in enumerate(matches):
        if len(qa_pairs) >= max_qa:
            break

        question = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        # Clean up HTML entities
        question = question.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'")

        # Extract answer: content from after this heading to before the next heading
        start = m.end()
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = len(body_html)

        answer_raw = body_html[start:end]

        # Strip all HTML tags
        answer = re.sub(r'<[^>]+>', ' ', answer_raw)
        # Collapse whitespace
        answer = re.sub(r'\s+', ' ', answer).strip()
        # Clean up HTML entities
        answer = answer.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'")
        # Limit length
        if len(answer) > 300:
            answer = answer[:300].rsplit(' ', 1)[0] + '...'

        if len(answer) > 20:  # require meaningful answer
            # Escape for JSON
            qa_pairs.append((question, answer))

    if not qa_pairs:
        return ""

    entities = []
    for q, a in qa_pairs:
        # JSON-escape strings
        q_escaped = q.replace('\\', '\\\\').replace('"', '\\"')
        a_escaped = a.replace('\\', '\\\\').replace('"', '\\"')
        entities.append(f'''    {{
      "@type": "Question",
      "name": "{q_escaped}",
      "acceptedAnswer": {{
        "@type": "Answer",
        "text": "{a_escaped}"
      }}
    }}''')

    ent_str = ",\n".join(entities)
    schema = f'''<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
{ent_str}
  ]
}}
</script>'''

    return schema


def extract_howto_schema(body_html: str, title: str, description: str = "") -> str:
    """Extract HowTo steps from HTML body and return HowTo JSON-LD string.

    Looks for ordered list items or paragraphs starting with 'Step N' / 'N.'
    patterns. Returns empty string if fewer than 2 steps found.
    """
    if not body_html:
        return ""

    steps = []

    # Strategy 1: Look for ordered list items <li> inside <ol>
    ol_match = re.search(r'<ol[^>]*>(.*?)</ol>', body_html, re.DOTALL)
    if ol_match:
        items = re.findall(r'<li[^>]*>(.*?)</li>', ol_match.group(1), re.DOTALL)
        for item in items:
            text = re.sub(r'<[^>]+>', ' ', item)
            text = re.sub(r'\s+', ' ', text).strip()
            text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'")
            if len(text) > 10:
                if len(text) > 200:
                    text = text[:200].rsplit(' ', 1)[0] + '...'
                steps.append(text)

    # Strategy 2: Look for "Step N:" or "Step N." patterns in paragraphs/headings
    if not steps:
        step_patterns = re.findall(
            r'(?:<h[23][^>]*>|<p>)\s*(?:Step\s*(\d+)[:\.\)]\s*)(.*?)(?:</h[23]>|</p>)',
            body_html,
            re.IGNORECASE | re.DOTALL,
        )
        if step_patterns:
            steps = []
            for num, text in step_patterns:
                text = re.sub(r'<[^>]+>', ' ', text).strip()
                text = re.sub(r'\s+', ' ', text)
                text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'")
                if len(text) > 10:
                    if len(text) > 200:
                        text = text[:200].rsplit(' ', 1)[0] + '...'
                    steps.append(text)

    if len(steps) < 2:
        return ""

    step_entities = []
    for i, step_text in enumerate(steps, 1):
        escaped = step_text.replace('\\', '\\\\').replace('"', '\\"')
        step_entities.append(f'''    {{
      "@type": "HowToStep",
      "position": {i},
      "name": "Step {i}",
      "text": "{escaped}"
    }}''')

    desc_escaped = description.replace('\\', '\\\\').replace('"', '\\"') if description else ""
    title_escaped = title.replace('\\', '\\\\').replace('"', '\\"')

    steps_str = ",\n".join(step_entities)
    schema = f'''<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "HowTo",
  "name": "{title_escaped}",
  "description": "{desc_escaped}",
  "step": [
{steps_str}
  ]
}}
</script>'''

    return schema


def build_also_read(current_url: str, current_subdomain: str, article_index: list, limit: int = 3) -> list:
    """Find related articles for Also Read section.

    Preference: same subdomain first, then fill from other subdomains.
    Excludes the current article itself. Sorted by generated_at descending.
    """
    candidates = [
        a for a in article_index
        if a.get("url", "") != current_url and a.get("title", "")
    ]
    if not candidates:
        return []

    # Separate by subdomain
    same_cat = [a for a in candidates if a.get("subdomain", "") == current_subdomain]
    other_cat = [a for a in candidates if a.get("subdomain", "") != current_subdomain]

    # Sort by generated_at descending
    same_cat.sort(key=lambda a: a.get("generated_at", "") or "", reverse=True)
    other_cat.sort(key=lambda a: a.get("generated_at", "") or "", reverse=True)

    result = same_cat[:limit]
    if len(result) < limit:
        result.extend(other_cat[:(limit - len(result))])

    return result[:limit]


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
    article_index = []
    if index_path.exists():
        entries = json.loads(index_path.read_text(encoding="utf-8"))
        for e in entries:
            url = e.get("url", "")
            entry = {
                "url": url,
                "title": e.get("title", ""),
                "description": e.get("description", ""),
                "date_display": e.get("date_display", ""),
                "keyword": e.get("keyword", ""),
                "subdomain": e.get("subdomain", ""),
                "generated_at": e.get("generated_at", ""),
            }
            index_lookup[url] = entry
            article_index.append(entry)

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

            # Extract FAQ Schema
            faq_schema = extract_faq_schema(body)

            # Extract HowTo Schema
            howto_schema = extract_howto_schema(body, title, description)

            # Inject Also Read section at end of body
            also_reads = build_also_read(url, subdomain, article_index)
            if also_reads:
                links = []
                for ar in also_reads:
                    links.append(f'<a href="{ar["url"]}">{ar["title"]}</a>')
                also_read_html = '<p class="also-read"><strong>Also Read:</strong> ' + ' &nbsp;·&nbsp; '.join(links) + '</p>'
                body = body + '\n' + also_read_html

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
                faq_schema=faq_schema or None,
                howto_schema=howto_schema or None,
            )

            article_file.write_text(html, encoding="utf-8")
            print(f"  OK  {url} -> {title[:60]}")
            success += 1

    print(f"\nDone: {success} articles re-rendered, {skipped} skipped.")


if __name__ == "__main__":
    main()
