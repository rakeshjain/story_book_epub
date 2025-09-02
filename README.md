# eBanglaLibrary → EPUB (All)

Pack Bengali articles/books from `ebanglalibrary.com` into EPUB files for offline reading. Supports single-EPUB packing and batch generation of one EPUB per book from Books or Authors indexes.

## Features
- Manual URLs: paste one or more article/chapter URLs and pack into a single EPUB
- Crawl from URL: discover chapters from a TOC/start URL and pack into a single EPUB
- Batch from Books/Authors index: discover books from `https://www.ebanglalibrary.com/books/` or `https://www.ebanglalibrary.com/authors/` and generate individual EPUBs per book
- Optional Author slug to target a specific author under `/authors/<slug>/`
- Request delay control in minutes (0–10) for polite crawling
- Clean content extraction (readability if available, with robust fallbacks)
- Cover image extraction: Automatically attempts to find and include a suitable cover image from title or early content sections; filters out logos, icons, and other non-content images
- EPUBs saved to `output/`

## Quick Start

### Linux
```bash
cd story_book_epub

# Option A (simple): run with system Python
/usr/bin/python3 -m streamlit run app.py --server.address=0.0.0.0 --server.port=8501

# Option B (virtualenv)
# sudo apt install -y python3-venv && python3 -m venv .venv && source .venv/bin/activate
# pip install -r requirements.txt
# streamlit run app.py --server.address=0.0.0.0 --server.port=8501
```

### Windows
```powershell
# Navigate to the project directory
cd story_book_epub

# Option A: run with system Python
python -m streamlit run app.py --server.port=8501

# Option B: using virtual environment
# python -m venv .venv
# .venv\Scripts\activate
# pip install -r requirements.txt
# streamlit run app.py --server.port=8501
```

Open http://localhost:8501

## Using the App

### Cover Image Settings
The app can automatically search for and include a cover image:

- Extract cover images: Enable/disable automatic cover image detection (checkbox)

How it works:
1. Tries any preloaded image hinted by the page (for example, a link with rel="preload" that provides an image srcset) and downloads the first valid image.
2. If none are found, falls back to images within the first few paragraphs of the page.
3. Skips obvious non-cover assets such as logos, favicons, sprites, social icons, and buttons.
4. Slightly prefers images hosted on cdn.ebanglalibrary.com.

Note: There is currently no manual URL override or image quality selector in the UI.

### Manual URLs
- Sidebar → Mode: "Manual URLs"
- Paste article links (one per line)
- Click "Pack EPUB"

### Crawl from URL
- Sidebar → Mode: "Crawl from URL"
- Enter a start/TOC URL (on ebanglalibrary.com)
- Set "Crawl depth" (0 = just page links, 1–3 = follow discovered pages)
- Set "Max pages" to cap crawling
- Optional regex filters (Include/Exclude)
- Click "Pack EPUB"

### Batch from Books/Authors index (one EPUB per book)
- Sidebar → Mode: "Batch from Books Index"
- Index type: choose "Books index" or "Authors index"
- If Authors index: optionally enter an author slug (e.g., `rabindranath-tagore`) to crawl only `/authors/<slug>/`
- Index URL: auto-filled based on your choice, but editable
- Set limits: "Max index pages", "Max books", "Max chapters per book"
- Set "Request delay (minutes)" = 0–10
- Click "Batch Generate EPUBs". Each discovered book is saved as a separate EPUB under `output/`.

Tips:
- Leave "Allow non-ebanglalibrary.com URLs" unchecked to keep only site links
- Increase limits gradually to avoid fetching too many pages

## Output
- Single-EPUB modes: saves one file to `output/<title>.epub` and offers a download button
- Batch mode: saves multiple files to `output/<book-title>.epub` (Bengali titles preserved)
- The output directory is automatically created relative to the script location, ensuring cross-platform compatibility between Windows and Linux

## Installation Notes
- If `readability-lxml` cannot be installed, the app still works using the built-in fallback extractor. You can install later:
```bash
python3 -m pip install --user --break-system-packages readability-lxml
```
- If you see `ModuleNotFoundError: ebooklib` in a venv, either:
```bash
pip install ebooklib
```
  or on Debian/Ubuntu use the system package:
```bash
sudo apt install -y python3-ebooklib
/usr/bin/python3 -m streamlit run app.py --server.address=0.0.0.0 --server.port=8501
```

## Cross-Platform Compatibility
This application is designed to work seamlessly on both Windows and Linux operating systems:

- **Path handling**: All file paths are created using `os.path` functions to ensure proper directory separators (backslashes on Windows, forward slashes on Linux)
- **Output directory**: The output folder is automatically created relative to the script location, not the current working directory
- **Character encoding**: The application properly handles Bengali Unicode characters in filenames and content

## Troubleshooting
- No books/links discovered: adjust limits, increase depth (for Crawl), or try different index pages
- Poor content extraction: enable readability or try a different start page
- Slug issues: author slugs should match the site's URL, e.g., `https://www.ebanglalibrary.com/authors/rabindranath-tagore/`
- Install errors: use a virtualenv or `--user --break-system-packages`
- **Windows-specific**: If you encounter issues with Bengali characters in filenames, ensure your system locale supports Unicode characters

### Cover Image Issues
- Wrong image selected: Disable "Extract cover images" to omit a cover, or try a different start page.
- No cover found: The page may not contain a suitable image; consider adding a cover later with external tools.
- File size concerns: Covers are downloaded as-is; large source images will produce larger EPUB files.

## Debug Mode

To enable debug logging in the app, set `DEBUG = True` in `app.py`. This will show additional info messages during EPUB creation and processing.

## Disclaimer
Use only for copyright-free content. Respect the site’s terms and applicable laws.
