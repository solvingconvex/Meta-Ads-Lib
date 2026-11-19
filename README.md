# Meta Ads Lib

Export active ads from the public Meta Ad Library to Excel. You choose the advertiser Pages, keywords, and countries on each run. Each country gets a worksheet with ad copy, embedded pictures, Library IDs, start dates, and platforms.

The [sample workbook](examples/foodpanda.xlsx) shows the Singapore and Malaysia reports for one advertiser. Its creative links open the public ad pages, and its thumbnails work offline.

## Quick start

Open PowerShell in the project folder. With Python 3.11 or newer installed, run these commands once.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium --only-shell
```

For a keyword search, replace the quoted phrases and two-letter country codes with your own. This example writes `research.xlsx` in the current folder.

```powershell
.\.venv\Scripts\python.exe meta_ads.py --countries US GB --keywords "running shoes" "hiking boots" --limit 25 --output research.xlsx
```

## Search specific advertisers

In the [Meta Ad Library](https://business.facebook.com/ads/library/), select the advertiser you want to research. Copy the search URL containing `view_all_page_id=`, or copy the numeric Page ID from that parameter. A Page ID identifies the advertiser. A Library ID identifies one ad.

The following PowerShell command prompts for your own Page IDs or advertiser search URLs. You can paste one or several, separated by spaces.

```powershell
$pages = (Read-Host 'Enter advertiser Page IDs or search URLs separated by spaces').Trim() -split '\s+'
.\.venv\Scripts\python.exe meta_ads.py --countries US GB --pages @pages --output advertiser_ads.xlsx
```

To include keyword results in the same workbook, reuse the Pages you just entered and add `--keywords`.

```powershell
.\.venv\Scripts\python.exe meta_ads.py --countries US GB --pages @pages --keywords "summer sale" "free shipping" --limit 50 --output combined_ads.xlsx
```

Each Page and keyword runs as a separate search in every selected country. Combining them can include ads from other advertisers that match a keyword. When several searches return the same ad in one country, its rows list all matches under **Matched searches**.

The default limit is 100 unique Library IDs per country per search. Set `--limit 0` to continue until Meta signals the last results page. The scraper reads the initial results directly over HTTP and opens a shared headless browser when it needs more results. The defaults allow three searches and eight image downloads at once. Use `--search-workers` and `--image-workers` to adjust these limits.

## Read the workbook

Columns A–C on every country tab are **Library ID**, **Started running on**, and **Platforms**. The ID is clickable text that preserves every digit, the start date is sortable, and platforms are listed by name. If Meta omits the date or platforms, the corresponding cell reads **Unavailable**.

The country code refers to the country selected in Meta's search. Each distinct creative card has a row. Cards from the same ad share a Library ID. **Ad text** includes the shared body and available card copy. **Image source** opens the full-size image or video poster when Meta supplies one. Embedded previews remain available offline. Meta's image links can expire.

A successful export replaces the requested output once writing finishes. If a search stalls or an image download fails, the scraper saves the available rows to a separate `.partial.xlsx` workbook, identifies the affected search or pictures, and returns exit code 2. If every search fails before returning data, it returns 1. A successful search with zero matches gets an empty country sheet with a message.

The workbook covers the IDs and creative variants exposed by the public search at retrieval time. Source and search interface details are available in the [Meta Ad Library](https://business.facebook.com/ads/library/).
