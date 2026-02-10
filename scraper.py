import io
import os
import re
import time
import zipfile
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# Evita che Playwright tenti download browser su Streamlit Cloud
os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"


# -----------------------
# Helpers
# -----------------------

def _is_http_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https")
    except Exception:
        return False


def _clean_filename(name: str) -> str:
    name = re.sub(r"[^\w\-.]+", "_", name.strip())
    return name[:180] if name else "file"


def _guess_chromium_executable() -> Optional[str]:
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _normalize_color_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[\(\)\[\]\d]+", " ", s)
    s = re.sub(r"[^a-z\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _wanted_color_from_title(title: str) -> Optional[str]:
    """
    Mappa i nomi del sito alle tue categorie:
    nero, bianco, rosso, blu_navy, blu_royal, grigio
    """
    t = _normalize_color_name(title)

    # Nero
    if "black" in t:
        return "nero"

    # Bianco
    if "white" in t:
        return "bianco"

    # Rosso (classic red, red, burgundy? -> tu hai detto rosso, quindi includo classic red e red;
    # se vuoi includere anche burgundy dimmelo e lo metto)
    if "red" in t:
        return "rosso"

    # Blu navy
    if "navy" in t:
        return "blu_navy"

    # Blu royal
    if "royal" in t:
        return "blu_royal"

    # Grigio
    if "grey" in t or "gray" in t:
        return "grigio"

    return None


def _upgrade_to_full_res(url: str) -> str:
    """
    Esempio: /media/.../opt-490x735-rj265m.jpg -> /media/.../rj265m.jpg
    Togliamo 'opt-WxH-' se presente.
    """
    return re.sub(r"/opt-\d+x\d+-", "/", url)


def _head_ok(session: requests.Session, url: str) -> bool:
    try:
        r = session.head(url, timeout=15, allow_redirects=True)
        if r.status_code == 200:
            return True
        # alcuni server non gestiscono bene HEAD: fallback GET piccolo
        if r.status_code in (403, 405):
            rg = session.get(url, timeout=15, stream=True, allow_redirects=True)
            return rg.status_code == 200
        return False
    except Exception:
        return False


def _download_to_zip(session: requests.Session, items: List[Tuple[str, str]]) -> Tuple[bytes, List[str], List[str]]:
    """
    items: [(url, filename)]
    """
    ok, failed = [], []
    mem = io.BytesIO()

    with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for url, filename in items:
            try:
                r = session.get(url, timeout=30)
                if r.status_code != 200 or not r.content:
                    failed.append(f"{url} (HTTP {r.status_code})")
                    continue

                zf.writestr(filename, r.content)
                ok.append(url)
            except Exception as e:
                failed.append(f"{url} ({type(e).__name__})")

    mem.seek(0)
    return mem.getvalue(), ok, failed


@dataclass
class ScrapeResult:
    zip_bytes: bytes
    found_image_urls: List[str]
    downloaded_ok: List[str]
    downloaded_failed: List[str]
    debug: List[str]


# -----------------------
# Core: click colori + prendi main gallery
# -----------------------

def _collect_main_images_by_selected_colors(
    page,
    product_url: str,
    wanted_categories: List[str],
    timeout_ms: int,
    debug: List[str],
) -> List[Tuple[str, str]]:
    """
    Ritorna lista [(final_img_url_fullres, categoria_colore)] per i colori richiesti.
    """
    base_url = product_url

    # Selector noto dai tuoi snippet
    color_link_sel = "a.js_colorswitch.colorSwitch"
    main_img_sel = "#js_productMainPhoto img.callToZoom"

    page.wait_for_selector(main_img_sel, timeout=timeout_ms)
    page.wait_for_selector(color_link_sel, timeout=timeout_ms)

    color_links = page.query_selector_all(color_link_sel)
    debug.append(f"Color links found: {len(color_links)}")

    # costruisci lista di target (handle + categoria)
    targets = []
    for a in color_links:
        title = (a.get_attribute("title") or "").strip()
        cat = _wanted_color_from_title(title)
        if cat and cat in wanted_categories:
            dc = a.get_attribute("data-color")
            targets.append((a, cat, title, dc))

    debug.append(f"Target colors matched: {len(targets)} -> {[t[1] for t in targets]}")

    # funzione per prendere src corrente + data-color
    def get_main_src_and_dc() -> Tuple[Optional[str], Optional[str]]:
        el = page.query_selector(main_img_sel)
        if not el:
            return None, None
        src = el.get_attribute("src") or el.get_attribute("data-src")
        dc = el.get_attribute("data-color")
        if src:
            src = urljoin(base_url, src)
        return src, (dc.strip() if dc else None)

    results: List[Tuple[str, str]] = []
    seen_categories = set()

    # clicca ogni colore richiesto (1 immagine principale per colore)
    for idx, (handle, cat, title, dc) in enumerate(targets, start=1):
        if cat in seen_categories:
            continue

        # scorrimento e click
        try:
            handle.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass

        old_src, _ = get_main_src_and_dc()
        debug.append(f"[{idx}] Click color: {title} (cat={cat}, data-color={dc}) old_src={old_src}")

        clicked = False
        try:
            handle.click(timeout=timeout_ms)
            clicked = True
        except Exception:
            try:
                page.evaluate("(el) => el.click()", handle)
                clicked = True
            except Exception:
                clicked = False

        if not clicked:
            debug.append(f"[{idx}] Click failed on {title}")
            continue

        # aspetta che l'immagine cambi (src diverso)
        try:
            page.wait_for_function(
                """(sel, oldSrc) => {
                    const el = document.querySelector(sel);
                    if(!el) return false;
                    const s = el.getAttribute('src') || el.getAttribute('data-src') || '';
                    return s && s !== oldSrc;
                }""",
                arg=(main_img_sel, old_src or ""),
                timeout=timeout_ms,
            )
        except Exception:
            # a volte il src non cambia per timing: aspetta un attimo
            time.sleep(0.6)

        time.sleep(0.3)

        new_src, new_dc = get_main_src_and_dc()
        debug.append(f"[{idx}] After click main src={new_src} data-color={new_dc}")

        if not new_src:
            continue

        results.append((new_src, cat))
        seen_categories.add(cat)

    return results


# -----------------------
# Main public function
# -----------------------

def scrape_images_with_login_sync(
    product_url: str,
    email: str,
    password: str,
    headless: bool = True,
    timeout_ms: int = 45000,
) -> ScrapeResult:
    debug: List[str] = []

    chromium_path = _guess_chromium_executable()
    if not chromium_path:
        raise RuntimeError(
            "Chromium non trovato nel container. "
            "Metti 'chromium' in packages.txt (e consigliato 'chromium-driver') "
            "oppure imposta CHROME_PATH."
        )

    wanted_categories = ["nero", "bianco", "rosso", "blu_navy", "blu_royal", "grigio"]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            executable_path=chromium_path,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
                "--disable-gpu",
                "--single-process",
            ],
        )
        context = browser.new_context()
        page = context.new_page()

        debug.append(f"Open product: {product_url}")
        page.goto(product_url, wait_until="domcontentloaded", timeout=timeout_ms)

        # login popup trigger
        debug.append("Click login trigger (popup)")
        page.click("a.login.js_popupLogin", timeout=timeout_ms)

        popup_page = None
        try:
            popup_page = page.wait_for_event("popup", timeout=3000)
        except PlaywrightTimeoutError:
            popup_page = None

        target = popup_page or page

        debug.append("Fill login form (known ids)")
        target.wait_for_selector("#user_email", timeout=timeout_ms)
        target.fill("#user_email", email, timeout=timeout_ms)
        target.fill("#user_password", password, timeout=timeout_ms)

        debug.append("Submit login")
        try:
            target.click('input.js_popupDoLogin[value="Accedi"]', timeout=timeout_ms)
        except Exception:
            target.click('input[type="submit"][value="Accedi"]', timeout=timeout_ms)

        time.sleep(1.2)

        if popup_page:
            debug.append("Close popup")
            try:
                popup_page.close()
            except Exception:
                pass

        debug.append("Reload product page after login")
        page.goto(product_url, wait_until="domcontentloaded", timeout=timeout_ms)
        time.sleep(1.0)

        # 1) raccogli immagini principali per i colori richiesti
        debug.append("Collect main images by clicking selected colors")
        main_images = _collect_main_images_by_selected_colors(
            page=page,
            product_url=product_url,
            wanted_categories=wanted_categories,
            timeout_ms=timeout_ms,
            debug=debug,
        )

        debug.append(f"Main images collected: {len(main_images)}")

        # 2) prepara requests session con cookies Playwright
        cookies = context.cookies()
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; ImageDownloader/1.0)",
            "Accept": "*/*",
            "Referer": product_url,
        })
        for c in cookies:
            sess.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path"))

        # 3) upgrade a full-res dove possibile + costruisci zip items
        found_urls: List[str] = []
        zip_items: List[Tuple[str, str]] = []

        for i, (url, cat) in enumerate(main_images, start=1):
            full = _upgrade_to_full_res(url)
            final_url = full if (full != url and _head_ok(sess, full)) else url

            found_urls.append(final_url)

            # estensione da url
            ext = ".jpg"
            m = re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", final_url, re.I)
            if m:
                ext = "." + m.group(1).lower().replace("jpeg", "jpg")

            filename = _clean_filename(f"{cat}_{i:02d}{ext}")
            zip_items.append((final_url, filename))

            debug.append(f"Pick [{cat}] -> {final_url} as {filename}")

        zip_bytes, ok, failed = _download_to_zip(sess, zip_items)

        browser.close()

        return ScrapeResult(
            zip_bytes=zip_bytes,
            found_image_urls=found_urls,
            downloaded_ok=ok,
            downloaded_failed=failed,
            debug=debug,
        )
