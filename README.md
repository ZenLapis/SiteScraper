# SiteScraper

Windows desktop website crawler. Give it a starting URL, set limits, watch results stream in, and export to Excel, CSV, JSON or JSON Lines.

- Collects titles, descriptions, headings, text, links, emails, phone numbers, word counts, status codes and errors.
- Fetches pages concurrently, stays on the starting site and respects `robots.txt` by default.
- **Dynamic pages:** some sites load their tables with JavaScript, which a crawler can't see. For known ones SiteScraper reads the same data service the page uses. Currently supported: the TGA certified accommodation facilities list (`tga.gov.tr/.../sertifikali-belgeli-konaklama-tesisleri`), about 19,000 rows in roughly 10 seconds.

## Run

Requires Windows, Python 3.12+ and the Edge WebView2 runtime.

```powershell
py -m pip install -r requirements.txt
pyw sitescraper.py
```

## Tests

```powershell
py -m unittest -v test_sitescraper
```
