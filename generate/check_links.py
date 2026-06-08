"""
Check for broken links in all HTML files under content/.
Scans <a href> and <img src>. Reports internal 404s and external dead links.
Usage: python generate/check_links.py
"""
import re
import urllib.request
import urllib.error
import ssl
import os
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = ROOT / "content"
# Windows long path prefix
CONTENT_DIR_LONG = "\\\\?\\" + str(CONTENT_DIR)
MAX_WORKERS = 10
TIMEOUT = 10


def find_html_files():
    """Yield all .html files under content/ using os.walk with long-path support."""
    for dirpath, dirnames, filenames in os.walk(CONTENT_DIR_LONG):
        for fname in filenames:
            if fname.endswith(".html"):
                # Convert back to Path without \\?\ prefix for display
                full_path = os.path.join(dirpath, fname)
                # Strip \\?\ prefix for Path
                yield Path(full_path.replace("\\\\?\\", "", 1))


def extract_links(html_path_obj):
    """Extract all <a href> and <img src> from an HTML file."""
    # Use long path prefix for reading to avoid MAX_PATH issues
    long_path = "\\\\?\\" + str(html_path_obj)
    try:
        with open(long_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except (FileNotFoundError, OSError):
        return []
    rel_path = str(html_path_obj.relative_to(CONTENT_DIR))

    links = []

    # <a href="...">
    for m in re.finditer(r'<a\s[^>]*href="([^"]*)"', text):
        url = m.group(1)
        if url.startswith("#") or url.startswith("mailto:") or url.startswith("javascript:"):
            continue
        links.append(("a", url, rel_path))

    # <img src="...">
    for m in re.finditer(r'<img\s[^>]*src="([^"]*)"', text):
        url = m.group(1)
        if url.startswith("data:"):
            continue
        links.append(("img", url, rel_path))

    return links


def check_internal_link(url, source_file):
    """Check if an internal link points to an existing file."""
    if url.startswith("//"):
        url = "https:" + url

    if url.startswith("/"):
        # Map to content/ directory
        if url == "/":
            target = CONTENT_DIR / "index.html"
        else:
            clean = url.lstrip("/")
            if clean.endswith("/"):
                target = CONTENT_DIR / clean / "index.html"
            else:
                target = CONTENT_DIR / clean

        # Use long path for existence check
        target_long = "\\\\?\\" + str(target)
        if not os.path.exists(target_long):
            return ("BROKEN_INTERNAL", url, source_file, f"Target not found: {target}")
        return None

    return None  # Not an internal link


def check_external_link(url, source_file):
    """Check external link with HEAD request."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, method="HEAD")
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx)
        if resp.status >= 400:
            return ("HTTP_ERROR", url, source_file, f"Status {resp.status}")
        return None
    except urllib.error.HTTPError as e:
        return ("HTTP_ERROR", url, source_file, f"Status {e.code}")
    except urllib.error.URLError as e:
        return ("NETWORK_ERROR", url, source_file, str(e.reason))
    except Exception as e:
        return ("ERROR", url, source_file, str(e))


def main():
    print("Scanning all HTML files under content/ ...\n")

    all_links = []
    for html_file in find_html_files():
        all_links.extend(extract_links(html_file))

    print(f"Found {len(all_links)} total links in {len(set(l[2] for l in all_links))} files.\n")

    broken = []
    internal_links = [(url, src) for tag, url, src in all_links if url.startswith("/") or url.startswith("//")]
    external_links = [(url, src) for tag, url, src in all_links if url.startswith("http") and not url.startswith("//")]

    # Check internal links
    print(f"Checking {len(internal_links)} internal links...")
    for url, src in internal_links:
        result = check_internal_link(url, src)
        if result:
            broken.append(result)

    # Check external links (with concurrency)
    print(f"Checking {len(external_links)} external links...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_external_link, url, src): (url, src) for url, src in external_links}
        for future in as_completed(futures):
            result = future.result()
            if result:
                broken.append(result)

    # Report
    print("\n" + "=" * 80)
    if broken:
        print(f"BROKEN LINKS FOUND: {len(broken)}")
        print("-" * 80)
        for i, (err_type, url, source, detail) in enumerate(broken, 1):
            print(f"{i}. [{err_type}] {url}")
            print(f"   Source: {source}")
            print(f"   Detail: {detail}")
            print()
    else:
        print("No broken links found.")

    print(f"Total scanned: {len(all_links)} | Broken: {len(broken)}")
    return len(broken)


if __name__ == "__main__":
    exit(main())