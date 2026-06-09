"""
AdSense Policy Compliance Checker
Scans all article HTML files for risk patterns:
- Percentage claims without citations
- Absolute language (guaranteed, 100%, eliminates, cure, miracle)
- Financial advice language
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = ROOT / "content"

# Risk patterns
HIGH_RISK_PATTERNS = {
    "absolute_guarantee": (
        r'\b(guaranteed|guarantee[sd]?\b(?!\s+(?:issue|placement|renewable|acceptance)))',
        "Absolute guarantee language (AdSense YMYL policy violation)"
    ),
    "absolute_eliminate": (
        r'\beliminates?\b',
        "Claims of elimination (over-promise language)"
    ),
    "absolute_cure_miracle": (
        r'\b(cure|miracle)\b',
        "Health-claim language (cure/miracle) inappropriate for insurance tech"
    ),
    "financial_advice": (
        r'\b(you\s+should\s+(invest|buy)|guaranteed\s+returns?|buy\s+this\s+stock|investment\s+advice)\b',
        "Financial advice language (YMYL policy violation)"
    ),
}

MEDIUM_RISK_PATTERNS = {
    "percentage_no_citation": (
        r'(?:reduces?|cuts?|improves?|increases?|boosts?|saves?|lowers?|achieves?|delivers?)\s+(?:[a-z\s]+)?(?:by\s+)?(\d{1,3})\s*%\b',
        "Percentage/statistical claim without inline citation"
    ),
    "absolute_100_pct": (
        r'\b100%\b',
        "100% claim (requires strong evidence)"
    ),
    "absolute_always_never": (
        r'\b(always|never|every\s+time|without\s+fail)\b',
        "Absolute claim (always/never without qualification)"
    ),
}

LOW_RISK_PATTERNS = {
    "overpromise_vague": (
        r'\b(revolutionary|game-?changing|disruptive|unprecedented)\b',
        "Vague over-promise marketing language"
    ),
    "unsourced_statistic": (
        r'\b(studies?\s+show|research\s+indicates?|experts?\s+say|according\s+to\s+(?:a\s+)?(?:recent\s+)?(?:study|report|survey))\b(?!.*?(?:\[|\(|according to|per |McKinsey|Deloitte|Accenture|Gartner|Forrester|BCG|Bain|PwC|EY|KPMG|OECD|World\s+Economic\s+Forum|NAIC|ISO|AM\s+Best|Swiss\s+Re|Munich\s+Re|Willis|Aon|Marsh))',
        "Reference to study/research without naming the source"
    ),
}

CITATION_INDICATORS = [
    r'\[', r'\]', r'\(', r'\)',  # Markdown links
    r'McKinsey', r'Deloitte', r'Accenture', r'Gartner', r'Forrester',
    r'BCG', r'Bain', r'PwC', r'EY', r'KPMG', r'OECD',
    r'World Economic Forum', r'NAIC', r'ISO', r'AM Best',
    r'Swiss Re', r'Munich Re', r'Willis', r'Aon', r'Marsh',
    r'Harvard', r'Stanford', r'MIT', r'Oxford', r'Cambridge',
    r'RAND', r'LIMRA', r'J.D. Power', r'Gallup', r'Pew',
    r'Bloomberg', r'Reuters', r'S&P', r'Moody',
]

def extract_text_content(html_file: Path) -> str:
    """Extract readable text from article HTML."""
    html = html_file.read_text(encoding="utf-8", errors="ignore")
    
    # Find article content div
    start = html.find('<div class="article-content">')
    if start == -1:
        return ""
    start += len('<div class="article-content">')
    
    # Find end: </article> or editorial-note or disclaimer
    end_markers = ['</article>', '<div class="editorial-note"', '<div class="disclaimer"']
    end = len(html)
    for marker in end_markers:
        idx = html.find(marker, start)
        if idx != -1 and idx < end:
            end = idx
    
    content = html[start:end]
    
    # Strip HTML tags
    content = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
    content = re.sub(r'<style[^>]*>.*?</style>', '', content, flags=re.DOTALL)
    content = re.sub(r'<[^>]+>', ' ', content)
    content = re.sub(r'\s+', ' ', content).strip()
    
    return content


def check_citation_nearby(text: str, match_start: int, match_end: int, window: int = 200) -> bool:
    """Check if a citation indicator exists within window chars after a match."""
    after = text[match_end:match_end + window]
    for indicator in CITATION_INDICATORS:
        if re.search(indicator, after):
            return True
    return False


def get_article_name(file_path: Path) -> str:
    """Get readable article path like /ai-claims/slug/"""
    rel = file_path.relative_to(CONTENT_DIR)
    parts = rel.parts
    if len(parts) >= 2:
        return f"content/{parts[0]}/{parts[1]}/index.html"
    return str(rel)


def analyze_level(html_files: list, patterns: dict, level_name: str, check_citation: bool = False):
    """Analyze a set of files against given patterns."""
    findings = []
    for f in sorted(html_files):
        text = extract_text_content(f)
        if not text:
            continue
        
        for pattern_name, (regex, description) in patterns.items():
            for m in re.finditer(regex, text, re.IGNORECASE):
                matched_text = m.group(0)
                # Get context
                ctx_start = max(0, m.start() - 60)
                ctx_end = min(len(text), m.end() + 60)
                context = text[ctx_start:ctx_end].strip()
                
                # For percentage claims, check if citation follows
                if check_citation and pattern_name == "percentage_no_citation":
                    if check_citation_nearby(text, m.start(), m.end()):
                        continue  # Has citation, skip
                
                name = get_article_name(f)
                findings.append(f"- **{name}**: {description}\n  > ...{context}...")
    
    return findings


def main():
    html_files = []
    for sf in CONTENT_DIR.iterdir():
        if not sf.is_dir() or sf.name in ('.github', 'images', 'about', 'contact', 'privacy', 'terms', 'sitemap'):
            continue
        for af in sf.iterdir():
            if not af.is_dir():
                continue
            idx = af / "index.html"
            if idx.exists():
                html_files.append(idx)
    
    total = len(html_files)
    print(f"Found {total} article files to scan.\n")
    
    high = analyze_level(html_files, HIGH_RISK_PATTERNS, "HIGH")
    medium = analyze_level(html_files, MEDIUM_RISK_PATTERNS, "MEDIUM", check_citation=True)
    low = analyze_level(html_files, LOW_RISK_PATTERNS, "LOW")
    
    # Build report
    report = f"""# AdSense Policy Compliance Check

**Scan date**: 2026-06-09  
**Files scanned**: {total} article HTML files  
**Scope**: All article body content in `content/*/.../index.html`

---

## 高风险（需立即修复）

"""
    if high:
        report += "\n".join(high) + "\n"
    else:
        report += "无高风险项。\n"
    
    report += "\n## 中风险（建议修复）\n\n"
    if medium:
        report += "\n".join(medium) + "\n"
    else:
        report += "无中风险项。\n"
    
    report += "\n## 低风险（可选修复）\n\n"
    if low:
        report += "\n".join(low) + "\n"
    else:
        report += "无低风险项。\n"
    
    report += f"""
---

## 修复建议

### 高风险项修复方法
1. **绝对化措辞**：将 "guaranteed" 改为 "demonstrated" 或 "has been shown to"；"eliminates" 改为 "significantly reduces"；删除 "cure"/"miracle" 类医学术语。
2. **财务建议**：添加 disclaimer 链接，将 "you should invest" 改为 "some investors consider"，并标注 "This is not financial advice"。

### 中风险项修复方法
1. **百分比声明**：每个百分比声明后紧跟引用来源，格式：`[Source: McKinsey, 2024]` 或使用 Markdown 链接 `[^1]`。
2. **100% 声明**：改为 "nearly all" 或 "the vast majority"，或提供具体数据出处。
3. **Always/Never**：添加限定词如 "in most cases"、"typically"、"generally"。

### 低风险项修复方法
1. **过度营销词**：将 "revolutionary" 改为 "innovative" 或 "advanced"。
2. **未具名研究引用**：补全研究机构名称和年份。
"""
    
    output_path = ROOT / "generate" / "adsense_policy_check.md"
    output_path.write_text(report, encoding="utf-8")
    print(f"Report written to: {output_path}")
    print(f"\nSummary:")
    print(f"  HIGH:   {len(high)} findings")
    print(f"  MEDIUM: {len(medium)} findings")
    print(f"  LOW:    {len(low)} findings")


if __name__ == "__main__":
    main()