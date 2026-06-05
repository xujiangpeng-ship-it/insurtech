"""Rebuild home page and category pages using main.py functions."""
import sys
import os

# Add generate directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import load_config, rebuild_home, rebuild_category_pages, rebuild_sitemap

config = load_config()
print(f"Site: {config['site']['name']}")

print("Rebuilding home page...")
rebuild_home(config)

print("Rebuilding category pages...")
rebuild_category_pages(config)

print("Rebuilding sitemap...")
rebuild_sitemap(config)

print("Done: home, category pages, and sitemap rebuilt.")
