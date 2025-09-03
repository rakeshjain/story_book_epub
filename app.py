"""
Streamlit app: eBanglaLibrary → EPUB

Purpose
- Fetch Bengali articles/chapters from ebanglalibrary.com and bundle them into an EPUB for offline reading.
- Three usage modes:
  1) Manual URLs: paste chapter/article URLs
  2) Crawl from URL: start from a TOC-like page and discover chapter links
  3) Batch from Books Index: discover many book pages from the site index and produce one EPUB per book

High-level flow
1) Gather links (based on the chosen mode)
2) Fetch each page, extract the main content (readability + fallbacks)
3) Derive a sensible book title/author from the start page (when possible)
4) Optionally extract a cover image (best-effort heuristics)
5) Build an EPUB file with chapters, TOC, and optional cover
6) Save the file into the output/ directory

Tips
- Turn DEBUG = True to see step-by-step status in the UI during processing
- Read README.md for examples and troubleshooting
"""

import os
import re
import io
import time
from typing import List, Tuple, Set, Deque, Optional
from urllib.parse import urljoin, urldefrag, urlparse, unquote, quote
from collections import deque

import requests
from bs4 import BeautifulSoup

try:
    from readability import Document  # optional

    _READABILITY_OK = True
except Exception:  # ImportError or others
    Document = None  # type: ignore
    _READABILITY_OK = False
from ebooklib import epub
import streamlit as st

DEBUG = False

APP_TITLE = "eBanglaLibrary → EPUB"
# Define output directory in a cross-platform way
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
SESSION_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
}

st.set_page_config(page_title=APP_TITLE, page_icon="📚", layout="wide")
st.title("📚 eBanglaLibrary → EPUB")
st.caption(
    "Pack copyright-free Bengali articles from ebanglalibrary.com into an EPUB for offline reading."
)


@st.cache_data(show_spinner=False)
def fetch_html(url: str) -> str:
    """Download a URL and return the decoded HTML text.

    - Uses a desktop browser User-Agent to avoid basic bot-blocks
    - Raises for HTTP errors (requests raises on non-2xx)
    - Sets encoding to apparent_encoding to preserve Bangla text correctly
    - Cached by Streamlit within a session to avoid repeated network calls
    """
    resp = requests.get(url, headers=SESSION_HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def extract_cover_image(html: str, base_url: str) -> Optional[Tuple[str, bytes]]:
    """Heuristically find a good cover image within a page.

    - Prefers images in typical content areas and those with large dimensions
    - Skips logos, favicons, ads, emoji, sprites, etc.
    - Returns (absolute_image_url, image_bytes) or None
    """
    try:
        soup = BeautifulSoup(html, "lxml")
        if DEBUG:
            st.info("Debug: Parsed HTML for cover extraction")
        cover_candidates = []
        if DEBUG:
            st.info("Debug: Starting cover candidate collection")

        # Helpers
        def _should_skip_url(u: str) -> bool:
            u = (u or "").lower()
            bad_sub = [
                "logo",
                "favicon",
                "sprite",
                "ads",
                "advert",
                "banner",
                "placeholder",
                "wp-includes",
                "/themes/",
                "comment",
                "emoji",
                "gravatar",
                "profile",
                "avatar",
                "share",
                "social",
                "button",
                "icon",
            ]
            if any(s in u for s in bad_sub):
                return True
            if u.endswith(".svg") or u.endswith(".gif"):
                return True
            return False

        # Look for preload links and return after first successful download
        for link in soup.find_all("link", rel="preload"):
            if link.get("as") == "image":
                if DEBUG:
                    st.info("Debug: Found preload link")
                img_url = None
                if link.get("imagesrcset"):
                    srcset = link.get("imagesrcset").strip()
                    parts = [part.strip() for part in srcset.split(",") if part.strip()]
                    if parts:
                        first_part = parts[0]
                        tokens = first_part.split()
                        img_url = tokens[0]
                if img_url:
                    if not img_url.startswith(("http://", "https://")):
                        img_url = urljoin(base_url, img_url)
                    try:
                        if DEBUG:
                            st.info(
                                f"Debug: Attempting to download first preloaded cover from {img_url}"
                            )
                        img_resp = requests.get(
                            img_url, headers=SESSION_HEADERS, timeout=30
                        )
                        img_resp.raise_for_status()
                        content_type = img_resp.headers.get("content-type", "")
                        if content_type.startswith("image/"):
                            return img_url, img_resp.content
                    except Exception:
                        continue
        # Fallback: Look for images in the first few paragraphs only if no better candidates (low priority)

        if not cover_candidates:
            if DEBUG:
                st.info("Debug: No preload candidates, entering fallback")
            for i, p in enumerate(soup.find_all("p")[:3]):  # Only first 3 paragraphs
                for img in p.find_all("img"):
                    src = img.get("src")
                    if src:
                        cover_candidates.append((src, 3, "Image in first paragraphs"))
                        if DEBUG:
                            st.info(f"Debug: Added fallback candidate: {src}")
        if DEBUG:
            st.info(f"Debug: Total candidates: {len(cover_candidates)}")
        if not cover_candidates:
            if DEBUG:
                st.info("Debug: No candidates found, returning None")
            return None

        # De-duplicate and filter obviously bad candidates
        filtered = []
        seen_urls = set()
        for u, pr, why in cover_candidates:
            if not u or _should_skip_url(u):
                continue
            key = u.split("?")[0]
            if key in seen_urls:
                continue
            seen_urls.add(key)
            # small boost for CDN images
            if "cdn.ebanglalibrary.com" in u:
                pr += 2
            filtered.append((u, pr, why))
        if not filtered:
            return None

        # Sort by priority (desc)
        filtered.sort(key=lambda x: x[1], reverse=True)

        for img_url, priority, reason in filtered:
            try:
                # Make URL absolute
                if not img_url.startswith(("http://", "https://")):
                    img_url = urljoin(base_url, img_url)

                if DEBUG:
                    st.info(f"Debug: Attempting to download cover from {img_url}")
                if DEBUG:
                    st.info(f"Pulling the cover image for the book from: {img_url}")
                # Download the image
                img_resp = requests.get(img_url, headers=SESSION_HEADERS, timeout=30)
                if DEBUG:
                    st.info(f"Debug: Download status code: {img_resp.status_code}")
                img_resp.raise_for_status()

                # Check if it's actually an image
                content_type = img_resp.headers.get("content-type", "")
                if DEBUG:
                    st.info(
                        f"Debug: Content-Type: {content_type}, Size: {len(img_resp.content)} bytes"
                    )
                if content_type.startswith("image/"):
                    img_data = img_resp.content
                    if DEBUG:
                        st.info("Debug: Successful image download")
                    return img_url, img_data

            except Exception:
                continue

        return None

    except Exception:
        return None


def extract_content(url: str, html: str) -> Tuple[str, str]:
    """Return (title, clean_html) using readability (if available), then fallback selectors."""
    # Try readability first if available
    if _READABILITY_OK and Document is not None:
        try:
            doc = Document(html)
            title = doc.short_title() or "Untitled"
            content_html = doc.summary(html_partial=True)
            if content_html:
                cleaned = sanitize_article_html(content_html)
                cleaned = strip_redundant_headings(title, cleaned)
                if len(BeautifulSoup(cleaned, "lxml").get_text(strip=True)) > 150:
                    return title, cleaned
        except Exception:
            pass

    # Fallback: try common content containers
    soup = BeautifulSoup(html, "lxml")
    title_tag = soup.find(["h1", "h2", "title"]) or soup.title
    title = title_tag.get_text(strip=True) if title_tag else "Untitled"

    candidates = [
        {"name": "article", "score": 2},
        {"name": "main", "score": 2},
        {
            "name": "div",
            "attrs": {
                "class": re.compile(r"(post|entry|content|article|chapter|chap)", re.I)
            },
        },
        {
            "name": "section",
            "attrs": {"class": re.compile(r"content|chapter|chap", re.I)},
        },
    ]
    best = None
    for cand in candidates:
        nodes = soup.find_all(cand.get("name"), cand.get("attrs", {}))
        for n in nodes:
            text_len = len(n.get_text(strip=True))
            if text_len > 300 and (best is None or text_len > best[0]):
                best = (text_len, n)
    if best is not None:
        cleaned = sanitize_article_html(str(best[1]))
        cleaned = strip_redundant_headings(title, cleaned)
        return title, cleaned

    # Last resort: whole body
    body = soup.body or soup
    cleaned = sanitize_article_html(str(body))
    cleaned = strip_redundant_headings(title, cleaned)
    return title, cleaned


def sanitize_article_html(html_fragment: str) -> str:
    """Remove non-content elements like comments, forms, footers, and common boilerplate.

    Returns a cleaned HTML fragment string.
    """
    soup = BeautifulSoup(html_fragment, "lxml")

    # Remove obvious non-content tags entirely
    for tag in soup.find_all(
        [
            "script",
            "style",
            "noscript",
            "form",
            "iframe",
            "header",
            "footer",
            "nav",
            "aside",
        ]
    ):
        tag.decompose()

    # Remove elements by common id/class patterns
    patterns = re.compile(
        r"(comment|comments|respond|reply|share|breadcrumb|sidebar|widget|meta|advert|ad-)",
        re.I,
    )
    for el in soup.find_all(True):
        try:
            el_id = ""
            el_classes = []
            el_role = ""
            if hasattr(el, "get"):
                el_id = el.get("id") or ""
                el_classes_raw = el.get("class")
                el_classes = el_classes_raw if isinstance(el_classes_raw, list) else []
                el_role = el.get("role") or ""
            attrs = " ".join([el_id, " ".join(el_classes), el_role])
            if patterns.search(attrs or ""):
                el.decompose()
        except Exception:
            continue

    # Remove blocks that are clearly comment prompts by text (decompose unique parents once)
    bad_text_re = re.compile(r"(leave a comment|মন্তব্য|কমেন্ট|প্রতিক্রিয়া|respond)", re.I)
    parents_to_remove_ids: Set[int] = set()
    parents_to_remove: List = []
    for el in soup.find_all(string=bad_text_re):  # 'string' is the preferred arg
        parent = getattr(el, "parent", None)
        if parent is None:
            continue
        pid = id(parent)
        if pid in parents_to_remove_ids:
            continue
        parents_to_remove_ids.add(pid)
        parents_to_remove.append(parent)
    for node in parents_to_remove:
        try:
            node.decompose()
        except Exception:
            pass

    # Remove anchors to comment sections
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(x in href for x in ["#respond", "#comments", "?replytocom="]):
            a.decompose()

    # Remove nav-like blocks such as "Bookmark", "Back to Book", "Next Lesson" etc.
    nav_text_re = re.compile(
        r"(bookmark|\*?bookmark\*?|বুকমার্ক|back to book|next lesson|previous lesson|prev lesson|next|prev|পূর্ববর্তী|পরবর্তী)",
        re.I,
    )
    # Remove anchors with only nav text
    for a in list(soup.find_all("a")):
        txt = a.get_text(" ", strip=True).lower()
        if txt and nav_text_re.search(txt):
            try:
                # If parent contains mostly nav text, drop parent; else drop link
                parent = a.parent
                if parent and len(parent.get_text(" ", strip=True)) <= len(txt) + 20:
                    parent.decompose()
                else:
                    a.decompose()
            except Exception:
                continue
    # Remove small blocks that contain only nav text
    for tag_name in ["p", "div", "ul", "ol", "li"]:
        for el in list(soup.find_all(tag_name)):
            try:
                t = el.get_text(" ", strip=True)
                # Short and clearly a navigation snippet
                if t and len(t) <= 60 and nav_text_re.search(t):
                    el.decompose()
            except Exception:
                continue

    # Explicitly remove lone "Bookmark" buttons/links, including star variants
    bookmark_exact = re.compile(r"^\s*(বুকমার্ক|bookmark)(?:\s*[☆★]?)\s*$", re.I)
    for tag_name in ["button", "a", "span", "div", "p"]:
        for el in list(soup.find_all(tag_name)):
            try:
                txt = el.get_text(" ", strip=True)
                id_cls = " ".join([el.get("id", ""), " ".join(el.get("class") or [])])
                if bookmark_exact.match(txt) or re.search(r"bookmark", id_cls, re.I):
                    # If this is inside a larger container that has only this control, drop the container
                    parent = el.parent
                    if (
                        parent
                        and len(
                            (parent.get_text(" ", strip=True) or "")
                            .replace(txt, "")
                            .strip()
                        )
                        == 0
                    ):
                        parent.decompose()
                    else:
                        el.decompose()
            except Exception:
                continue

    # Finally, aggressively strip trailing nav-only nodes at the end of the content
    def _strip_trailing_nav(container):
        try:
            children = list(container.children)
            i = len(children) - 1
            removed_any = False
            while i >= 0 and children:
                node = children[i]
                text = ""
                if getattr(node, "get_text", None):
                    text = node.get_text(" ", strip=True)
                else:
                    text = str(node).strip()
                if text and len(text) <= 80 and nav_text_re.search(text):
                    try:
                        node.extract()
                        removed_any = True
                    except Exception:
                        break
                    i -= 1
                    continue
                break
            return removed_any
        except Exception:
            return False

    # Apply to body or soup
    target = soup.body if soup.body else soup
    # Repeat up to 3 times in case of nested wrappers
    for _ in range(3):
        if not _strip_trailing_nav(target):
            break

    # Optional: unwrap containers that only add classes
    # Keep structure otherwise; return inner HTML if <body> present
    body = soup.body
    if body:
        return "".join(str(c) for c in body.contents)
    return str(soup)


def _norm_text(s: str) -> str:
    """Normalize: trim, lowercase, collapse all whitespace to single spaces for matching."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def strip_redundant_headings(title: str, html_fragment: str) -> str:
    """Remove duplicate chapter headings so they don't repeat in the content.

    Keeps the first heading that matches the title (or a near match), removes subsequent
    occurrences across h1/h2/h3. If none match exactly, performs a loose containment check.
    """
    try:
        soup = BeautifulSoup(html_fragment, "lxml")
        want = _norm_text(title)
        seen = False
        for tag_name in ["h1", "h2", "h3"]:
            for h in list(soup.find_all(tag_name)):
                txt = _norm_text(h.get_text(" ", strip=True))
                is_match = (txt == want) or (
                    txt and want and (txt in want or want in txt)
                )
                if is_match:
                    if seen:
                        h.decompose()
                    else:
                        seen = True
        # If headings still appear more than once verbatim, remove extras by text dedupe
        seen_texts: Set[str] = set()
        for tag_name in ["h1", "h2", "h3"]:
            for h in list(soup.find_all(tag_name)):
                txt = _norm_text(h.get_text(" ", strip=True))
                key = f"{tag_name}:{txt}"
                if txt and key in seen_texts:
                    h.decompose()
                else:
                    seen_texts.add(key)
        body = soup.body
        return "".join(str(c) for c in body.contents) if body else str(soup)
    except Exception:
        return html_fragment


def extract_lessons_from_book_page(start_url: str, html: str) -> List[str]:
    """Best-effort extraction of chapter/lesson links from a book page.

    Strategy: collect all anchors with '/lessons/' or '/topics/' in href, same-domain, keep order of
    appearance, return absolute, de-duplicated list.
    """
    soup = BeautifulSoup(html, "lxml")
    found: List[str] = []
    seen: Set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not href:
            continue
        # Check for both lessons and topics URLs
        if "/lessons/" not in href and "/topics/" not in href:
            continue
        abs_link = _clean_link(start_url, href)
        if not abs_link:
            continue
        if not _same_domain(start_url, abs_link):
            continue
        if abs_link in seen:
            continue
        seen.add(abs_link)
        found.append(abs_link)
    return found


def extract_lesson_pairs_from_book_page(start_url: str, html: str) -> List[Tuple[str, str]]:
    """Extract (display_name, absolute_url) pairs for lessons/topics from a book/TOC page.

    - Same filtering as extract_lessons_from_book_page: only same-domain and href containing '/lessons/' or '/topics/'.
    - Preserve order of first appearance and de-duplicate by absolute URL.
    - Display name is normalized anchor text; falls back to URL basename if empty.
    """
    soup = BeautifulSoup(html, "lxml")
    pairs: List[Tuple[str, str]] = []
    seen: Set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href")
        if not href:
            continue
        if "/lessons/" not in href and "/topics/" not in href:
            continue
        abs_link = _clean_link(start_url, href)
        if not abs_link:
            continue
        if not _same_domain(start_url, abs_link):
            continue
        if abs_link in seen:
            continue
        seen.add(abs_link)
        txt = (a.get_text(" ", strip=True) or "").strip()
        if not txt:
            # Fallback to last path segment without query
            try:
                txt = os.path.basename(abs_link.split("?")[0]) or abs_link
            except Exception:
                txt = abs_link
        pairs.append((txt, abs_link))
    return pairs


def make_epub(
    book_title: str,
    items: List[Tuple[str, str, str]],
    author: Optional[str] = None,
    cover_image: Optional[Tuple[str, bytes]] = None,
) -> Tuple[bytes, Optional[str]]:
    """
    Build an EPUB from items: list of (section_title, url, html_content) -> (bytes, author).
    Optionally include a cover image.
    Returns the EPUB data as bytes and the author string.
    """
    book = epub.EpubBook()
    book.set_title(book_title)
    book.set_language("bn")
    if author:
        # Add as EPUB DC:creator
        try:
            book.add_author(author)
        except Exception:
            pass

    # Add cover image if provided
    cover_item = None
    cover_page = None
    if cover_image:
        try:
            img_url, img_data = cover_image
            if DEBUG:
                st.info(
                    f"Debug: Starting cover processing for {img_url}, data size: {len(img_data)} bytes"
                )

            # Determine file extension and media type from URL
            if "." in img_url:
                # Handle special case for .jpg.webp or similar double extensions
                if ".jpg.webp" in img_url.lower() or ".jpeg.webp" in img_url.lower():
                    ext = "webp"
                    if DEBUG:
                        st.info(
                            f"Debug: Detected .jpg.webp extension, using 'webp' as format"
                        )
                else:
                    ext = img_url.split(".")[-1].lower()
                    # Remove any query parameters
                    if "?" in ext:
                        ext = ext.split("?")[0]
                if ext not in ["jpg", "jpeg", "png", "gif", "webp"]:
                    ext = "jpg"  # Default to jpg
                if DEBUG:
                    st.info(f"Debug: Detected extension: {ext}")
            else:
                ext = "jpg"
                if DEBUG:
                    st.info(f"Debug: No extension in URL, defaulting to jpg")

            # Map extensions to proper MIME types
            mime_types = {
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "png": "image/png",
                "gif": "image/gif",
                "webp": "image/webp",
            }
            media_type = mime_types.get(ext, "image/jpeg")

            cover_filename = f"cover.{ext}"
            if DEBUG:
                st.info(
                    f"Debug: Cover filename: {cover_filename}, Media type: {media_type}"
                )

            # Create a proper cover image item
            # Use EpubItem instead of EpubCover for better control
            cover_item = epub.EpubItem(
                uid="cover-img",
                file_name=cover_filename,
                media_type=media_type,
                content=img_data,
            )

            # Log the cover item details
            if DEBUG:
                st.info(
                    f"🔧 Cover item created: id={cover_item.id}, file={cover_item.file_name}, type={cover_item.media_type}"
                )
            if DEBUG:
                st.info(f"🔧 Cover content size: {len(cover_item.content)} bytes")

            # Add cover item to book
            book.add_item(cover_item)
            if DEBUG:
                st.info(f"Debug: Added cover item to EPUB: {cover_item.file_name}")

            # Create a cover page that displays the image
            cover_page = epub.EpubHtml(
                title="Cover", file_name="cover.xhtml", lang="bn"
            )
            cover_page.content = f"""
            <html xmlns="http://www.w3.org/1999/xhtml" xml:lang="bn">
                <head>
                    <title>{book_title}</title>
                    <meta http-equiv="Content-Type" content="text/html; charset=utf-8"/>
                    <style type="text/css">
                        body {{ margin: 0; padding: 0; text-align: center; }}
                        img {{ max-width: 100%; max-height: 100vh; margin: 0; padding: 0; }}
                    </style>
                </head>
                <body>
                    <div>
                        <img src="{cover_filename}" alt="Cover Image"/>
                    </div>
                </body>
            </html>
            """

            # Add the cover page to the book
            book.add_item(cover_page)
            if DEBUG:
                st.info(f"Debug: Added cover page XHTML to EPUB")

            # Set the cover property in the book - this is the key part
            # First set the cover image
            book.set_cover(cover_filename, img_data)

            # Add metadata to explicitly link the cover image
            book.add_metadata(
                None, "meta", "", {"name": "cover", "content": "cover-img"}
            )

            # Add metadata for cover in OPF file
            book.add_metadata(
                "OPF", "meta", "", {"name": "cover", "content": "cover-img"}
            )

            # Add guide reference for the cover
            book.guide.append(
                {"type": "cover", "href": "cover.xhtml", "title": "Cover"}
            )
            if DEBUG:
                st.info("✅ Cover image and cover page added to book")

        except Exception as e:
            st.error(f"Failed to add cover image: {e}")
            st.error(f"Error details: {type(e).__name__}: {str(e)}")
            # Continue without cover if there's an error
            cover_item = None
            cover_page = None

    chapters = []
    for idx, (title, url, content_html) in enumerate(items, start=1):
        chapter = epub.EpubHtml(
            title=title, file_name=f"chap_{idx:03d}.xhtml", lang="bn"
        )
        chapter.set_content(f"<h1>{title}</h1>{content_html}")
        book.add_item(chapter)
        chapters.append(chapter)

    # Navigation
    book.toc = tuple(chapters)

    # Build spine - include cover page if present, then nav, then chapters
    if cover_image and cover_page:
        book.spine = [cover_page, "nav", *chapters]
    else:
        book.spine = ["nav", *chapters]

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # Write to bytes
    buf = io.BytesIO()
    epub.write_epub(buf, book)

    # Debug: Show EPUB structure
    if cover_image:
        if DEBUG:
            st.info(
                f"🔍 EPUB structure - Items: {len(book.items)}, Spine: {len(book.spine)}"
            )
        if DEBUG:
            st.info(
                f"🔍 Cover image present: {'cover-img' in [item.id for item in book.items if hasattr(item, 'id')]} "
            )
        if DEBUG:
            st.info(
                f"🔍 Cover page present: {{'cover.xhtml' in [item.file_name for item in book.items if hasattr(item, 'file_name')]}} "
            )

    return buf.getvalue(), author


def parse_title_author_from_html(
    html: str,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Parse page title into (raw_full_title, title_only, author) using '-' or '–' as separator.

    raw_full_title is the exact page <title> (or <h1>) text. title_only is the part before the
    separator if found; author is the part after. Any of them may be None if not detected.
    """
    soup = BeautifulSoup(html, "lxml")
    raw = None
    if soup.title and soup.title.string:
        raw = soup.title.string.strip()
    else:
        h1 = soup.find("h1")
        if h1:
            raw = h1.get_text(strip=True)
    if not raw:
        return None, None, None
    # Split on dash/en dash surrounded by spaces if possible
    m = re.split(r"\s[-–]\s", raw, maxsplit=1)
    if len(m) == 2:
        title = m[0].strip() or None
        author = m[1].strip() or None
        return raw, title, author
    return raw, raw, None


def fs_safe_basename_from_title(title: str) -> str:
    """Return a filesystem-safe name while preserving Bengali characters.

    - Replaces path separators (/, \\) with visually similar fullwidth forms
    - Removes NULs and trims/problematic trailing dots
    """
    s = (title or "").strip()
    if not s:
        return "ebanglalibrary"
    s = s.replace("/", "／").replace("\\", "＼")
    s = s.replace("\x00", "")
    # Avoid trailing spaces or dots which can be problematic on some filesystems
    s = s.strip().rstrip(".")
    return s or "ebanglalibrary"


def _bn_text(s: str) -> str:
    """Trim helper tolerant to None; preserves Bengali text unchanged."""
    return (s or "").strip()


def derive_author_full(html: str, author_token: Optional[str]) -> Optional[str]:
    if not html or not author_token:
        return author_token
    try:
        soup = BeautifulSoup(html, "lxml")
        corpus = soup.get_text(" ", strip=True)
        if not corpus:
            return author_token
        # Capture up to 20 Bengali chars around the token
        pat = re.compile(
            rf"([\u0980-\u09FF\s]{{0,20}}{re.escape(author_token)}[\u0980-\u09FF\s]{{0,20}})"
        )
        matches = pat.findall(corpus)
        if not matches:
            return author_token
        # Pick the longest match as likely full name context
        cand = max(matches, key=lambda x: len(x))
        cand = re.sub(r"\s+", " ", cand).strip()
        # Trim to at most 4 Bengali words
        words = cand.split()
        if len(words) > 4:
            cand = " ".join(words[:4])
        return cand or author_token
    except Exception:
        return author_token


def page_indicates_edited(html: str) -> bool:
    if not html:
        return False
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    return any(tok in text for tok in ["সম্পাদিত", "সম্পাদক", "সম্পাদনা"])


def build_output_basename(
    raw_full_title: Optional[str],
    title_only: Optional[str],
    author_token: Optional[str],
    start_html: Optional[str],
) -> str:
    # Prefer constructed form: "<title_only> – <author_full>[ সম্পাদিত]" when possible
    t_only = _bn_text(title_only or "")
    r_full = _bn_text(raw_full_title or "")
    author_full = derive_author_full(start_html or "", author_token)
    edited = page_indicates_edited(start_html or "")

    if t_only and author_full:
        suffix = " সম্পাদিত" if edited and not author_full.endswith("সম্পাদিত") else ""
        base = f"{t_only} – {author_full}{suffix}"
        return fs_safe_basename_from_title(base)
    # Fallback to raw page title
    base = r_full or t_only or "eBanglaLibrary Collection"
    return fs_safe_basename_from_title(base)


def _clean_link(base_url: str, href: str) -> Optional[str]:
    """Resolve href to an absolute URL and filter out non-HTML assets and mail/tel links."""
    if not href:
        return None
    if href.startswith("mailto:") or href.startswith("tel:"):
        return None
    href = urljoin(base_url, href)
    href, _frag = urldefrag(href)
    # basic asset filter
    if re.search(
        r"\.(jpg|jpeg|png|gif|svg|pdf|zip|rar|7z|mp3|mp4|avi|webm)(?:\?|$)", href, re.I
    ):
        return None
    return href


def _same_domain(u1: str, u2: str) -> bool:
    """Check if two URLs share the same network location (host:port)."""
    return urlparse(u1).netloc == urlparse(u2).netloc

# New helper: derive a friendly name from URL path when no title/name available
def pretty_display_name_from_url(u: str) -> str:
    try:
        p = urlparse(u)
        path = (p.path or "/").rstrip("/")
        seg = path.split("/")[-1]
        seg = unquote(seg)
        seg = seg.replace("-", " ").replace("_", " ").strip()
        return seg or u
    except Exception:
        return u


def discover_links(
    start_url: str,
    max_depth: int = 1,
    allow_outside: bool = False,
    include_pattern: str = "",
    exclude_pattern: str = "",
    max_pages: int = 100,
) -> List[str]:
    """Discover chapter/article links from a TOC/start page.

    First, try to detect a book page and extract '/lessons/' chapter links in-page.
    If that yields results, return them directly (ordered, de-duplicated).
    Otherwise, fall back to a bounded BFS crawl with optional regex filters.
    """
    include_re = re.compile(include_pattern) if include_pattern else None
    exclude_re = re.compile(exclude_pattern) if exclude_pattern else None

    # 1) Free path for books: extract lessons directly from the start page
    try:
        start_html = fetch_html(start_url)
        lessons = extract_lessons_from_book_page(start_url, start_html)
        if lessons:
            # apply regex filters if provided
            if include_re:
                lessons = [u for u in lessons if include_re.search(u)]
            if exclude_re:
                lessons = [u for u in lessons if not exclude_re.search(u)]
            if not allow_outside:
                lessons = [u for u in lessons if _same_domain(start_url, u)]
            # cap
            return lessons[:max_pages]
    except Exception:
        pass

    # 2) Fallback: BFS crawl
    queue: Deque[Tuple[str, int]] = deque([(start_url, 0)])
    visited_pages: Set[str] = set()
    found: List[str] = []

    while queue and len(visited_pages) < max_pages:
        url, depth = queue.popleft()
        if url in visited_pages:
            continue
        visited_pages.add(url)
        try:
            html = fetch_html(url)
        except Exception:
            continue
        soup = BeautifulSoup(html, "lxml")

        # collect links on this page
        for a in soup.find_all("a", href=True):
            link = _clean_link(url, a.get("href"))
            if not link:
                continue
            if not allow_outside and not _same_domain(start_url, link):
                continue
            if include_re and not include_re.search(link):
                continue
            if exclude_re and exclude_re.search(link):
                continue
            # Heuristic: prefer links with text suggesting chapters/entries
            link_text = (a.get_text(strip=True) or "").lower()
            if link not in found:
                found.append(link)

        # enqueue neighbors for BFS up to max_depth
        if depth < max_depth:
            # limit enqueued neighbors to top N per page to avoid explosion
            neighbors_added = 0
            for a in soup.find_all("a", href=True):
                link = _clean_link(url, a.get("href"))
                if not link:
                    continue
                if link in visited_pages:
                    continue
                if not allow_outside and not _same_domain(start_url, link):
                    continue
                if include_re and not include_re.search(link):
                    continue
                if exclude_re and exclude_re.search(link):
                    continue
                queue.append((link, depth + 1))
                neighbors_added += 1
                if neighbors_added >= 50:
                    break

    # Remove the start page itself if present
    found = [u for u in found if u != start_url]

    # Simple stable sort: by URL path length then lexicographic
    found.sort(key=lambda u: (len(urlparse(u).path), u))
    return found[:max_pages]


def discover_books_from_index(
    index_url: str,
    max_books: int = 500,
    max_index_pages: int = 20,
) -> List[str]:
    """Discover individual book pages from:
    - The Books index (and its pagination), or
    - The Authors list (and its pagination), or
    - A specific Author page (and its pagination)

    Heuristics:
    - Treat pages whose path matches "/books/" or "/books/page/<n>/" as index pages
    - Collect links that include "/books/" but are not index pages themselves
    - Keep order of discovery and de-duplicate
    """

    # Recognize index pages for Books, Authors list, and individual Author pages (decoded paths)
    def _is_books_index_path(path_dec: str) -> bool:
        return re.match(r"^/books/(?:page/\d+/?)?$", path_dec) is not None

    def _is_authors_index_path(path_dec: str) -> bool:
        return re.match(r"^/authors/(?:page/\d+/?)?$", path_dec) is not None

    def _is_index_path(path_dec: str) -> bool:
        return _is_books_index_path(path_dec) or _is_authors_index_path(path_dec)

    def _is_author_detail_index_path(path_dec: str) -> bool:
        # Author detail page (optionally paginated): /authors/<slug>/[page/N]
        return re.match(r"^/authors/[^/]+/(?:page/\d+/?)?$", path_dec) is not None

    def _looks_like_book_page_path(path_dec: str) -> bool:
        # Book detail pages generally live under /books/... or /book/...
        if "/books/" in path_dec:
            return True
        if re.search(r"/book(?:s)?/", path_dec):
            return True
        return False

    start_path_dec = unquote(urlparse(index_url).path or "")
    starting_author_detail = _is_author_detail_index_path(start_path_dec)

    queue: Deque[str] = deque([index_url])
    visited_index_pages: Set[str] = set()
    found_books: List[str] = []
    seen_books: Set[str] = set()

    while (
        queue
        and len(visited_index_pages) < max_index_pages
        and len(found_books) < max_books
    ):
        page_url = queue.popleft()
        if page_url in visited_index_pages:
            continue
        visited_index_pages.add(page_url)
        try:
            html = fetch_html(page_url)
        except Exception:
            continue
        soup = BeautifulSoup(html, "lxml")

        # Collect book links on this index page
        for a in soup.find_all("a", href=True):
            link = _clean_link(page_url, a.get("href"))
            if not link or not _same_domain(index_url, link):
                continue
            path_raw = urlparse(link).path or ""
            path_dec = unquote(path_raw)
            if not _looks_like_book_page_path(path_dec):
                continue
            # Skip index pages themselves; keep only book detail pages
            if _is_books_index_path(path_dec):
                continue
            if link in seen_books:
                continue
            seen_books.add(link)
            found_books.append(link)
            if len(found_books) >= max_books:
                break

        # Discover pagination/index neighbors. If starting from a specific author
        # page, keep neighbors limited to that same author's pagination only.
        if len(visited_index_pages) < max_index_pages:
            for a in soup.find_all("a", href=True):
                link = _clean_link(page_url, a.get("href"))
                if not link or not _same_domain(index_url, link):
                    continue
                path_raw = urlparse(link).path or ""
                path_dec = unquote(path_raw)
                allow = False
                if starting_author_detail:
                    # only allow this author's own pagination pages
                    allow = _is_author_detail_index_path(path_dec)
                else:
                    # from books/authors index, follow index pages and author detail pages
                    allow = _is_index_path(path_dec) or _is_author_detail_index_path(
                        path_dec
                    )
                if allow and link not in visited_index_pages:
                    queue.append(link)

    return found_books[:max_books]


with st.sidebar:
    st.header("Input")
    # Choose how you want to provide content URLs:
    # - Manual URLs: paste one URL per line
    # - Crawl from URL: start at a TOC page and auto-discover chapter links
    # - Batch from Books Index: one EPUB per book from the main Books index
    mode = st.radio(
        "Mode", ["Manual URLs", "Crawl from URL", "Batch from Books Index"], index=0
    )

    if mode == "Manual URLs":
        # Free-form input for one-off articles/chapters
        urls_text = st.text_area(
            "Paste one or more article URLs (one per line)",
            placeholder="https://www.ebanglalibrary.com/some-article\nhttps://www.ebanglalibrary.com/another-article",
            height=150,
        )
    elif mode == "Crawl from URL":
        # Start at a Table-of-Contents (TOC) page and let the app crawl links
        start_url = st.text_input(
            "Start (TOC) URL", placeholder="https://www.ebanglalibrary.com/..."
        )
        # How deep should the crawler follow in-site links from the start page
        max_depth = st.slider("Crawl depth", 0, 3, 1)
        # Stop after visiting this many pages (helps keep runs short)
        max_pages = st.slider("Max pages", 5, 300, 100, step=5)
        # Restrict/allow URLs using regex patterns (leave empty for no filter)
        include_pattern = st.text_input("Include pattern (regex)", value="")
        exclude_pattern = st.text_input("Exclude pattern (regex)", value="")
        # Request throttling between chapter fetches for crawl mode
        throttle_min = st.number_input(
            "Request delay (minutes)",
            min_value=0.0,
            max_value=10.0,
            value=0.5,
            step=0.1,
        )
    else:
        # Batch mode (from index pages) — builds one EPUB per book discovered
        index_choice = st.selectbox(
            "Index type",
            ["Books index", "Authors index"],
            index=0,
            help="Choose Books or Authors. For Authors, you can specify a particular author slug.",
        )
        if index_choice == "Books index":
            default_index_url = "https://www.ebanglalibrary.com/books/"
            author_slug = ""
        else:
            # Optional: limit to a single author's listing pages
            author_slug = st.text_input(
                "Author slug (optional)",
                value="",
                help="If provided, will crawl only that author's pages, e.g., rabindranath-tagore",
                placeholder="e.g., rabindranath-tagore",
            )
            if author_slug.strip():
                default_index_url = f"https://www.ebanglalibrary.com/authors/{quote(author_slug.strip())}/"
            else:
                default_index_url = "https://www.ebanglalibrary.com/authors/"

        index_url = st.text_input(
            "Index URL (Books/Authors)",
            value=default_index_url,
            placeholder=default_index_url,
        )
        # Fixed default values as requested
        max_index_pages = 10  # Default value
        st.info(f"Max index pages: {max_index_pages} (default)")

        # Max books with fixed default
        max_books = 5  # Default value
        st.info(f"Max books: {max_books} (default)")

        # Max chapters with fixed default
        per_book_cap = 100  # Default value
        st.info(f"Max chapters per book: {per_book_cap} (default)")

        # Allow user to override defaults if needed
        use_custom_values = st.checkbox("Override default values", value=False)
        if use_custom_values:
            max_index_pages = st.number_input(
                "Max index pages", min_value=1, max_value=50, value=10
            )
            max_books = st.number_input(
                "Max books", min_value=1, max_value=1000, value=5
            )
            per_book_cap = st.number_input(
                "Max chapters per book", min_value=1, max_value=500, value=100
            )
        # Request throttling between requests in batch mode
        throttle_min = st.number_input(
            "Request delay (minutes)",
            min_value=0.0,
            max_value=10.0,
            value=0.5,
            step=0.1,
        )

    # Title is auto-parsed from the page; no manual entry
    allow_outside = st.checkbox("Allow non-ebanglalibrary.com URLs", value=False)

    # Cover image options
    st.subheader("📖 Cover Image Settings")
    extract_covers = st.checkbox("Extract cover images from title pages", value=True)


# General guidance for new users on when to use each mode
st.info(
    "Tip: For TOC pages, use Crawl mode; otherwise paste URLs manually. Use Batch mode to generate one EPUB per book from the books index."
)

# Single EPUB workflow (Manual URLs or Crawl)
if st.button("Pack EPUB", type="primary"):
    if DEBUG:
        st.info("Debug: Pack EPUB button pressed, starting process")
    if mode == "Manual URLs":
        # Parse user-provided URLs line-by-line
        raw_urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
        start_html_for_meta = None
    elif mode == "Crawl from URL":
        # Validate the starting page and run link discovery according to settings
        if not start_url:
            st.warning("Please enter a start URL.")
            st.stop()
        with st.spinner("Discovering chapter links…"):
            discovered = discover_links(
                start_url=start_url,
                max_depth=max_depth,
                allow_outside=allow_outside,
                include_pattern=include_pattern,
                exclude_pattern=exclude_pattern,
                max_pages=max_pages,
            )
        if not discovered:
            st.error("No links discovered. Adjust depth/filters and try again.")
            st.stop()
        st.success(f"Discovered {len(discovered)} links.")
        # Fetch start page html for title/author metadata and names
        try:
            start_html_for_meta = fetch_html(start_url)
        except Exception:
            start_html_for_meta = None
        name_map = {}
        if start_html_for_meta:
            try:
                pairs = extract_lesson_pairs_from_book_page(start_url, start_html_for_meta)
                name_map = {url: name for (name, url) in pairs}
            except Exception:
                name_map = {}
        display_names = [name_map.get(u) or pretty_display_name_from_url(u) for u in discovered]
        st.dataframe({"name": display_names, "url": discovered})
        raw_urls = discovered
    else:
        st.warning(
            "Batch mode uses the button below. Choose Manual or Crawl for single EPUB packing."
        )
        st.stop()

    if not raw_urls:
        st.warning("Please provide at least one URL.")
        st.stop()

    urls = []
    for u in raw_urls:
        if allow_outside or "ebanglalibrary.com" in u:
            urls.append(u)
    urls = list(dict.fromkeys(urls))  # de-dup keep order

    if not urls:
        st.warning("No valid URLs to process.")
        st.stop()

    # Early skip if target EPUB already exists (Crawl mode)
    if mode == "Crawl from URL":
        author_meta_early: Optional[str] = None
        raw_full_title_early: Optional[str] = None
        meta_title_only_early: Optional[str] = None
        if "start_html_for_meta" in locals() and start_html_for_meta:
            raw_full_title_early, meta_title_only_early, meta_author_early = parse_title_author_from_html(start_html_for_meta)
            if meta_author_early:
                author_meta_early = meta_author_early

        if raw_full_title_early:
            out_base_early = fs_safe_basename_from_title(raw_full_title_early)
        else:
            out_base_early = build_output_basename(
                raw_full_title_early,
                meta_title_only_early,
                author_meta_early,
                (start_html_for_meta if "start_html_for_meta" in locals() else None),
            )

        final_output_dir_early = OUTPUT_DIR
        if author_meta_early:
            author_dir_name_early = fs_safe_basename_from_title(author_meta_early)
            if author_dir_name_early:
                final_output_dir_early = os.path.join(OUTPUT_DIR, author_dir_name_early)

        os.makedirs(final_output_dir_early, exist_ok=True)
        out_path_early = os.path.join(final_output_dir_early, f"{out_base_early}.epub")

        if DEBUG:
            st.info(f"Debug: (Crawl) Checking for existing file at: {out_path_early}")

        if os.path.exists(out_path_early):
            st.write(f"Existing file found: {out_path_early}")
            with open(out_path_early, "rb") as f:
                existing_data = f.read()
            st.download_button(
                label="Download Existing EPUB",
                data=existing_data,
                file_name=f"{out_base_early}.epub",
                mime="application/epub+zip",
            )
            st.stop()

    items: List[Tuple[str, str, str]] = []
    progress = st.progress(0)
    status = st.empty()

    for i, url in enumerate(urls, start=1):
        try:
            # Fetch HTML first to derive a friendly display name (Manual mode) or use crawl map
            html = fetch_html(url)
            raw_full_title_tmp, meta_title_only_tmp, _ = parse_title_author_from_html(html)
            display_name = (
                raw_full_title_tmp
                or meta_title_only_tmp
                or (name_map.get(url) if 'name_map' in locals() else None)
                or pretty_display_name_from_url(url)
            )
            status.write(f"Fetching: {display_name}")
            title, content_html = extract_content(url, html)
            items.append((title, url, content_html))
        except Exception as e:
            st.error(f"Failed: {url} ({e})")
        # Throttle requests in Crawl mode
        if mode == "Crawl from URL" and throttle_min:
            time.sleep(throttle_min * 60.0)
        progress.progress(i / len(urls))

    if not items:
        st.error("No items extracted. Please check the URLs.")
        st.stop()

    # Auto-derive title and author metadata from start page (if available)
    author_meta: Optional[str] = None
    book_title: Optional[str] = None
    raw_full_title: Optional[str] = None
    cover_image: Optional[Tuple[str, bytes]] = None

    if "start_html_for_meta" in locals() and start_html_for_meta:
        raw_full_title, meta_title_only, meta_author = parse_title_author_from_html(
            start_html_for_meta
        )
        # Document name should be exactly the page title
        if raw_full_title:
            book_title = raw_full_title
        elif meta_title_only:
            book_title = meta_title_only
        if meta_author:
            author_meta = meta_author
        # Try to extract cover image from the start page
        if extract_covers:
            try:
                if DEBUG:
                    st.info("Debug: Attempting cover extraction from start page")
                cover_image = extract_cover_image(start_html_for_meta, start_url)
                if cover_image:
                    img_url, img_data = cover_image
                    st.success(
                        f"🔍 Found cover image from title page: {img_url.split('/')[-1]} ({len(img_data)} bytes)"
                    )
                else:
                    st.info("🔍 No cover image found on title page")
            except Exception as e:
                st.warning(f"Failed to extract cover image: {e}")

    # Fallback title if crawling was not used
    if not book_title:
        # Derive from first URL's domain/path
        try:
            first_title, _html = None, None
            # fetch first url to get title
            _html = fetch_html(raw_urls[0])
            raw_full, first_title, _author = parse_title_author_from_html(_html)
            book_title = raw_full or first_title or "eBanglaLibrary Collection"

            # Try to extract cover image from the first page if we don't have one
            if DEBUG:
                st.info(
                    f"Debug: No cover yet, extract_covers is {extract_covers}, attempting from first page"
                )
            if not cover_image and extract_covers:
                try:
                    if DEBUG:
                        st.info("Debug: Attempting cover extraction from first URL")
                    cover_image = extract_cover_image(_html, raw_urls[0])
                    if cover_image:
                        img_url, img_data = cover_image
                        st.success(
                            f"🔍 Found cover image from first page: {img_url.split('/')[-1]} ({len(img_data)} bytes)"
                        )
                    else:
                        st.info("🔍 No cover image found on first page")
                except Exception as e:
                    st.warning(f"Failed to extract cover image: {e}")
        except Exception:
            book_title = "eBanglaLibrary Collection"

    st.success(f"Collected {len(items)} article(s). Building EPUB…")

    # Show cover image status
    if cover_image:
        img_url, img_data = cover_image
        st.info(f"📖 Cover image: {img_url.split('/')[-1]} ({len(img_data)} bytes)")
        st.info(f"🔍 Image URL: {img_url}")

        # Verify image data integrity
        if len(img_data) < 1024:
            st.warning("⚠️ Cover image seems too small, may be corrupted")
        elif len(img_data) > 10 * 1024 * 1024:
            st.warning("⚠️ Cover image is very large, may cause issues")
        else:
            st.success(f"✅ Cover image data verified ({len(img_data)} bytes)")
    else:
        st.info("📖 No cover image will be included in this EPUB")

    st.info(
        "🔨 Building EPUB with cover image..."
        if cover_image
        else "🔨 Building EPUB without cover image..."
    )
    data, author = make_epub(
        book_title, items, author=author_meta, cover_image=cover_image
    )

    # Output filename: if page has a full title, use it verbatim; else construct a best-effort name
    if raw_full_title:
        out_base = fs_safe_basename_from_title(raw_full_title)
    else:
        out_base = build_output_basename(
            raw_full_title,
            (meta_title_only if "meta_title_only" in locals() else None),
            author_meta,
            (start_html_for_meta if "start_html_for_meta" in locals() else None),
        )

    # Determine output directory based on author
    final_output_dir = OUTPUT_DIR
    if author:
        # Sanitize author name for directory path
        author_dir_name = fs_safe_basename_from_title(author)
        if author_dir_name:
            final_output_dir = os.path.join(OUTPUT_DIR, author_dir_name)

    os.makedirs(final_output_dir, exist_ok=True)
    out_path = os.path.join(final_output_dir, f"{out_base}.epub")

    if DEBUG:
        st.info(f"Debug: Checking for existing file at: {out_path}")

    # Check for existing file before fetching chapters
    if os.path.exists(out_path):
        st.write(f"Existing file found: {out_path}")
        with open(out_path, "rb") as f:
            existing_data = f.read()
        st.download_button(
            label=f"Download Existing EPUB",
            data=existing_data,
            file_name=f"{out_base}.epub",
            mime="application/epub+zip",
        )
        st.stop()

    with open(out_path, "wb") as f:
        f.write(data)

    st.download_button(
        label="Download EPUB",
        data=data,
        file_name=f"{out_base}.epub",
        mime="application/epub+zip",
    )
    st.write(f"Saved to: {out_path}")


# Batch mode: generate one EPUB per book discovered from the books index
if mode == "Batch from Books Index" and st.button(
    "Batch Generate EPUBs", type="primary"
):
    if DEBUG:
        st.info("Debug: Batch Generate button pressed, starting batch process")
    if not index_url:
        # Require the base index page (Books or a specific Author index)
        st.warning("Please enter the books index URL.")
        st.stop()

    generated_count = 0
    saved_files = []
    with st.spinner("Discovering books from index…"):
        book_urls = discover_books_from_index(
            index_url=index_url,
            max_books=max_books,
            max_index_pages=max_index_pages,
        )

    if not book_urls:
        st.error("No books discovered from the index.")
        st.stop()

    st.success(f"Discovered {len(book_urls)} book(s).")
    with st.spinner("Resolving book titles…"):
        book_titles: List[str] = []
        for bu in book_urls:
            try:
                bh = fetch_html(bu)
                rf, to, _ = parse_title_author_from_html(bh)
                nm = rf or to or pretty_display_name_from_url(bu)
            except Exception:
                nm = pretty_display_name_from_url(bu)
            book_titles.append(nm)
    st.dataframe({"title": book_titles, "url": book_urls})

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    overall = st.progress(0)
    status = st.empty()
    saved_files: List[str] = []

    for b_idx, book_url in enumerate(book_urls, start=1):
        if DEBUG:
            st.info(f"Debug: Processing book {b_idx}: {book_url}")
        try:
            status.write(f"Book {b_idx}/{len(book_urls)}: {book_title}")
            book_html = fetch_html(book_url)
            raw_full_title, meta_title_only, meta_author = parse_title_author_from_html(
                book_html
            )
            # Determine book title
            if raw_full_title:
                book_title = raw_full_title
            elif meta_title_only:
                book_title = meta_title_only
            else:
                book_title = "eBanglaLibrary Book"
                
            # Determine output filename early
            if raw_full_title:
                out_base = fs_safe_basename_from_title(raw_full_title)
            else:
                out_base = build_output_basename(
                    raw_full_title, meta_title_only, meta_author, book_html
                )

            # Determine output directory based on author (using meta_author for now)
            final_output_dir = OUTPUT_DIR
            if meta_author:
                # Sanitize author name for directory path
                author_dir_name = fs_safe_basename_from_title(meta_author)
                if author_dir_name:
                    final_output_dir = os.path.join(OUTPUT_DIR, author_dir_name)

            os.makedirs(final_output_dir, exist_ok=True)
            out_path = os.path.join(final_output_dir, f"{out_base}.epub")

            if DEBUG:
                st.info(f"Debug: Checking for existing file at: {out_path}")

            # Check for existing file before fetching chapters
            if os.path.exists(out_path):
                status.write(f"Skipping existing file: {out_path}")
                continue

            # Try to extract cover image from the book page
            cover_image: Optional[Tuple[str, bytes]] = None
            if extract_covers:
                try:
                    if DEBUG:
                        st.info(
                            f"Debug: Attempting cover extraction for book {book_title}"
                        )
                    cover_image = extract_cover_image(book_html, book_url)
                    if cover_image:
                        img_url, img_data = cover_image
                        status.write(
                            f"🔍 Found cover image for '{book_title}': {img_url.split('/')[-1]} ({len(img_data)} bytes)"
                        )
                    else:
                        status.write(f"🔍 No cover image found for '{book_title}'")
                except Exception as e:
                    status.write(
                        f"⚠️ Failed to extract cover image for '{book_title}': {e}"
                    )

            # Build lesson name map and discover chapter links from this book page
            try:
                pairs = extract_lesson_pairs_from_book_page(book_url, book_html)
                lesson_name_map = {url: name for (name, url) in pairs}
            except Exception:
                lesson_name_map = {}
            lessons = extract_lessons_from_book_page(book_url, book_html)
            if not lessons:
                st.warning(f"No chapters found for book: {book_url}")
                overall.progress(b_idx / len(book_urls))
                time.sleep(throttle_min * 60.0)
                continue

            lessons = lessons[:per_book_cap]

            items: List[Tuple[str, str, str]] = []
            for i, chap_url in enumerate(lessons, start=1):
                try:
                    display_name = lesson_name_map.get(chap_url) or pretty_display_name_from_url(chap_url)
                    status.write(f"Fetching chapter {i}/{len(lessons)}: {display_name}")
                    html = fetch_html(chap_url)
                    title, content_html = extract_content(chap_url, html)
                    items.append((title, chap_url, content_html))
                except Exception as e:
                    st.error(f"Failed chapter: {chap_url} ({e})")

            if not items:
                st.warning(f"No chapters extracted for book: {book_url}")
                overall.progress(b_idx / len(book_urls))
                time.sleep(throttle_min * 60.0)
                continue

            data, author = make_epub(
                book_title, items, author=meta_author, cover_image=cover_image
            )

            # Update output directory if author from make_epub is different from meta_author
            if author and author != meta_author:
                final_output_dir = OUTPUT_DIR
                author_dir_name = fs_safe_basename_from_title(author)
                if author_dir_name:
                    final_output_dir = os.path.join(OUTPUT_DIR, author_dir_name)
                    os.makedirs(final_output_dir, exist_ok=True)
                    out_path = os.path.join(final_output_dir, f"{out_base}.epub")
                    
                    # Re-check if file exists in the new author directory
                    if DEBUG:
                        st.info(f"Debug: Re-checking for existing file at new path: {out_path}")
                    if os.path.exists(out_path):
                        status.write(f"Skipping existing file: {out_path}")
                        continue

            with open(out_path, "wb") as f:
                f.write(data)

            generated_count += 1
            saved_files.append(out_path)
            st.write(f"Saved: {out_path}")
        except Exception as e:
            st.error(f"Book failed: {book_url} ({e})")
        overall.progress(b_idx / len(book_urls))
        time.sleep(throttle_min * 60.0)

    if saved_files:
        st.success(f"Generated {len(saved_files)} EPUB file(s).")
    else:
        st.error("No EPUBs were generated.")
