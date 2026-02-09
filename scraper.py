import io
import re
import zipfile
from typing import List, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


def _is_http_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https")
    except Exception:
        return False


def _clean_filename(name: str) -> str:
    name = re.sub(r"[^\w\-.]+", "_", name.strip())
    return name[:180] if name else "file"


def _extract_image_urls(html: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    urls = set()

    for img in soup.select("img"):
        for attr in ("src", "data-src", "data-lazy", "data-original"):
            u = img.get(attr)
            if u:
                urls.add(urljoin(base_url, u))

    for a in soup.select("a[href]"):
        href = a.get("href")
        if href:
            full = urljoin(base_url, href)
            if re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", full, re.I):
                urls.add(full)

    for tag in soup.find_all(style=True):
        s = tag.get("style") or ""
        for m in re.findall(r'url\((["\']?)(.*?)\1\)', s, flags=re.I):
            full = urljoin(base_url, m[1])
            if re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", full, re.I):
                urls.add(full)

    return [u for u in urls if _is_http_url(u)]


def _download_to_zip(session: requests.Session, img_urls: List[str]) -> Tuple[bytes, List[str], List[str]]:
    ok, failed = [], []
    mem = io.BytesIO()

    with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, url in enumerate(img_urls, start=1):
            try:
                r = session.get(url, timeout=30)
                if r.status_code != 200 or not r.content:
                    failed.append(f"{url} (HTTP {r.status_code})")
                    continue

                ct = (r.headers.get("content-type") or "").lower()
                ext = ".bin"
                if "jpeg" in ct:
                    ext = ".jpg"
                elif "png" in ct:
                    ext = ".png"
                elif "webp" in ct:
                    ext = ".webp"
                elif "gif" in ct:
                    ext = ".gif"
                else:
                    m = re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", url, re.I)
                    if m:
                        ext = "." + m.group(1).lower().replace("jpeg", "jpg")

                filename = _clean_filename(f"img_{i:03d}{ext}")
                zf.writestr(filename, r.content)
                ok.append(url)
            except Exception as e:
                failed.append(f"{url} ({type(e).__name__})")

    mem.seek(0)
    return mem.getvalue(), ok, failed


def scrape_images_with_login(product_url: str, email: str, password: str, headless: bool = True, timeout_ms: int = 45000):
    debug = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = browser.new_context()
        page = context.new_page()

        debug.append(f"Open: {product_url}")
        page.goto(product_url, wait_until="domcontentloaded", timeout=timeout_ms)

        # Click login trigger (adatta selettore se cambia)
        debug.append("Click login trigger")
        page.click("a.login.js_popupLogin", timeout=timeout_ms)

        popup = None
        try:
            popup = page.wait_for_event("popup", timeout=3000)
            debug.append("Popup window detected")
        except PlaywrightTimeoutError:
            debug.append("No popup window (maybe modal)")

        target = popup or page

        debug.append("Fill credentials")
        # email/user
        try:
            target.fill('input[type="text"]', email, timeout=timeout_ms)
        except PlaywrightTimeoutError:
            target.fill('input:not([type="password"])', email, timeout=timeout_ms)

        target.fill('input[type="password"]', password, timeout=timeout_ms)

        debug.append("Submit")
        try:
            target.click('button:has-text("Accedi")', timeout=timeout_ms)
        except PlaywrightTimeoutError:
            target.click('input[type="submit"]', timeout=timeout_ms)

        # Torna al prodotto con sessione
        debug.append("Reload product (post login)")
        page.goto(product_url, wait_until="domcontentloaded", timeout=timeout_ms)

        html = page.content()
        img_urls = _extract_image_urls(html, product_url)
        debug.append(f"Found {len(img_urls)} image URLs")

        # Cookies -> requests
        cookies = context.cookies()
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
            "Referer": product_url,
        })
        for c in cookies:
            sess.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path"))

        zip_bytes, ok, failed = _download_to_zip(sess, img_urls)

        browser.close()

    return zip_bytes, img_urls, ok, failed, debug
