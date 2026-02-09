# scraper.py
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

# IMPORTANT: su Streamlit Cloud evita download browser via Playwright
os.environ.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")


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
    """
    Streamlit Cloud: installa chromium via apt (packages.txt) e usalo con executable_path.
    Percorsi tipici:
      - /usr/bin/chromium
      - /usr/bin/chromium-browser
    """
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        os.environ.get("CHROMIUM_PATH", ""),
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _extract_image_urls(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    urls = set()

    # img tags
    for img in soup.select("img"):
        for attr in ("src", "data-src", "data-lazy", "data-original", "data-zoom-image"):
            u = img.get(attr)
            if u:
                urls.add(urljoin(base_url, u))

    # anchor links diretti a immagini
    for a in soup.select("a[href]"):
        href = a.get("href")
        if href:
            full = urljoin(base_url, href)
            if re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", full, re.I):
                urls.add(full)

    # immagini dentro style="background-image:url(...)"
    for tag in soup.find_all(style=True):
        s = tag.get("style") or ""
        for m in re.findall(r'url\((["\']?)(.*?)\1\)', s, flags=re.I):
            full = urljoin(base_url, m[1])
            if re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", full, re.I):
                urls.add(full)

    # pulizia
    out = [u for u in urls if _is_http_url(u)]
    out.sort()
    return out


def _download_to_zip(
    session: requests.Session,
    img_urls: List[str],
    per_request_timeout: int = 30,
) -> Tuple[bytes, List[str], List[str]]:
    ok, failed = [], []
    mem = io.BytesIO()

    with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, url in enumerate(img_urls, start=1):
            try:
                r = session.get(url, timeout=per_request_timeout)
                if r.status_code != 200 or not r.content:
                    failed.append(f"{url} (HTTP {r.status_code})")
                    continue

                ext = None
                ct = (r.headers.get("content-type") or "").lower()
                if "jpeg" in ct:
                    ext = ".jpg"
                elif "png" in ct:
                    ext = ".png"
                elif "webp" in ct:
                    ext = ".webp"
                elif "gif" in ct:
                    ext = ".gif"

                if not ext:
                    m = re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", url, re.I)
                    ext = "." + m.group(1).lower().replace("jpeg", "jpg") if m else ".bin"

                filename = _clean_filename(f"img_{i:03d}{ext}")
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
# Main scraping function
# -----------------------

def scrape_images_with_login_sync(
    product_url: str,
    email: str,
    password: str,
    headless: bool = True,
    timeout_ms: int = 30000,
) -> ScrapeResult:
    """
    Flusso:
    - apri product_url
    - clicca "Accedi" (apre modal inline)
    - compila email/password nel modal e submit
    - ricarica product_url mantenendo sessione
    - estrai immagini dalla pagina post-login
    - scarica immagini e crea zip
    """
    debug: List[str] = []

    chromium_path = _guess_chromium_executable()
    if not chromium_path:
        raise RuntimeError(
            "Chromium non trovato nel container. "
            "Assicurati che in packages.txt ci sia 'chromium' e (consigliato) 'chromium-driver'. "
            "Oppure imposta CHROME_PATH."
        )
    debug.append(f"Chromium path: {chromium_path}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            executable_path=chromium_path,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
                "--disable-gpu",
            ],
        )

        # UA “normale” (aiuta con siti schizzinosi)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        # 1) open prodotto
        debug.append(f"Open product: {product_url}")
        page.goto(product_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_load_state("networkidle", timeout=timeout_ms)

        # 2) apri modal login
        debug.append("Click login trigger (modal)")
        page.click("a.login.js_popupLogin", timeout=timeout_ms)

        # 3) aspetta modal visibile
        debug.append("Wait modal visible")
        page.wait_for_selector("#js_popupSignInBody", state="visible", timeout=timeout_ms)

        # 4) fill credenziali usando ID certi del tuo HTML
        debug.append("Fill credentials (modal)")
        page.fill("#user_email", email, timeout=timeout_ms)
        page.fill("#user_password", password, timeout=timeout_ms)

        # 5) submit
        debug.append("Submit login (modal)")
        page.click("input.js_popupDoLogin", timeout=timeout_ms)

        # 6) attendi chiusura modal o stabilizzazione rete
        debug.append("Wait modal to close / network idle")
        try:
            page.wait_for_selector("#js_popupSignInBody", state="hidden", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            # alcuni siti non "nascondono" subito il body del modal
            page.wait_for_load_state("networkidle", timeout=timeout_ms)

        time.sleep(0.5)

        # 7) reload product post-login
        debug.append("Reload product page after login")
        page.goto(product_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        time.sleep(0.4)

        html = page.content()
        img_urls = _extract_image_urls(html, product_url)
        debug.append(f"Found {len(img_urls)} image urls in HTML")

        # 8) cookies Playwright -> requests
        cookies = context.cookies()
        sess = requests.Session()
        sess.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "*/*",
                "Referer": product_url,
            }
        )
        for c in cookies:
            # domain può essere ".example.com" o "example.com" -> requests accetta
            sess.cookies.set(
                c["name"],
                c["value"],
                domain=c.get("domain"),
                path=c.get("path") or "/",
            )

        # 9) download zip
        zip_bytes, ok, failed = _download_to_zip(sess, img_urls)

        debug.append(f"Downloaded OK: {len(ok)}")
        debug.append(f"Downloaded failed: {len(failed)}")

        browser.close()

        return ScrapeResult(
            zip_bytes=zip_bytes,
            found_image_urls=img_urls,
            downloaded_ok=ok,
            downloaded_failed=failed,
            debug=debug,
        )
