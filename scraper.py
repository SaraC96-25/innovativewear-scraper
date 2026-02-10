import io
import os
import re
import time
import zipfile
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# Evita che Playwright provi a scaricare browser (su Streamlit Cloud spesso fallisce)
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


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _clean_filename(name: str) -> str:
    name = re.sub(r"[^\w\-.]+", "_", name.strip())
    return name[:180] if name else "file"


def _guess_chromium_executable() -> Optional[str]:
    # Percorsi tipici in container Debian
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


def _best_image_url_candidates(img_url: str) -> List[str]:
    """
    Dal tuo esempio:
      /media/.../opt-490x735-rj265m.jpg
    spesso esiste anche:
      /media/.../rj265m.jpg  (originale, più grande)
    Quindi: proviamo prima "senza opt-WxH-", poi fallback.
    """
    if not img_url:
        return []

    cands = [img_url]

    # rimuove "opt-123x456-" se presente
    c2 = re.sub(r"/opt-\d+x\d+-", "/", img_url)
    if c2 != img_url:
        cands.insert(0, c2)

    # se ci sono thumb tipo 113x40-..., prova a rimuovere anche quello
    c3 = re.sub(r"/\d+x\d+-", "/", img_url)
    if c3 not in cands:
        cands.insert(0, c3)

    # unisci e dedup preservando ordine
    out = []
    seen = set()
    for u in cands:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _pick_best_existing_url(session: requests.Session, candidates: List[str]) -> str:
    """
    Prova HEAD/GET leggero per capire quale URL esiste davvero.
    Se HEAD non è permesso, ripiega su GET con stream=True.
    """
    for u in candidates:
        try:
            r = session.head(u, timeout=15, allow_redirects=True)
            if r.status_code == 200:
                return u
        except Exception:
            pass

    for u in candidates:
        try:
            r = session.get(u, timeout=20, stream=True, allow_redirects=True)
            if r.status_code == 200:
                return u
        except Exception:
            pass

    return candidates[-1] if candidates else ""


def _download_bytes(session: requests.Session, url: str) -> Tuple[Optional[bytes], str]:
    try:
        r = session.get(url, timeout=40, allow_redirects=True)
        if r.status_code != 200 or not r.content:
            return None, f"HTTP {r.status_code}"
        return r.content, ""
    except Exception as e:
        return None, type(e).__name__


@dataclass
class ScrapeResult:
    zip_bytes: bytes
    found_image_urls: List[str]
    downloaded_ok: List[str]
    downloaded_failed: List[str]
    debug: List[str]


# -----------------------
# Core logic
# -----------------------

def _login_via_modal(page, email: str, password: str, timeout_ms: int, debug: List[str]) -> None:
    # Trigger: a.login.js_popupLogin
    debug.append("Click login trigger (popup modal)")
    page.click("a.login.js_popupLogin", timeout=timeout_ms)

    debug.append("Wait modal body")
    page.wait_for_selector("#js_popupSignInBody", timeout=timeout_ms)

    debug.append("Fill email/password")
    page.fill("#user_email", email, timeout=timeout_ms)
    page.fill("#user_password", password, timeout=timeout_ms)

    debug.append("Submit login")
    page.click("input.js_popupDoLogin, input[type='submit'][value*='Accedi']", timeout=timeout_ms)

    # Aspetta che il modal sparisca o che cambi qualcosa in pagina
    try:
        page.wait_for_selector("#js_popupSignInBody", state="detached", timeout=timeout_ms)
        debug.append("Modal closed after login")
    except PlaywrightTimeoutError:
        debug.append("Modal did not detach (ok if site keeps it hidden). Continue.")

    # Small buffer for session cookies
    time.sleep(1.5)


def _extract_color_swatch_map(page, timeout_ms: int, debug: List[str]):
    """
    Legge tutte le swatch:
    <a class="js_colorswitch colorSwitch" data-color="CR" title="Classic Red (CR)">...
       <div class="color-code-thumb">CR</div>
    """
    swatches = page.locator("a.js_colorswitch.colorSwitch")
    n = swatches.count()
    debug.append(f"Found swatches: {n}")

    items = []
    for i in range(n):
        h = swatches.nth(i)
        title = (h.get_attribute("title") or "").strip()
        data_color = (h.get_attribute("data-color") or "").strip()

        code_text = ""
        try:
            code_text = h.locator(
                "xpath=ancestor::div[contains(@class,'wrapperSwitchColore')]//div[contains(@class,'color-code-thumb')]"
            ).inner_text(timeout=300).strip()
        except Exception:
            pass

        items.append({
            "index": i,
            "handle": h,
            "title": title,
            "data_color": data_color,
            "code_text": code_text,
        })

    return items


def _match_swatch(item, wanted_norm: List[str]) -> bool:
    if not wanted_norm:
        return True

    t = _norm(item["title"])
    dc = _norm(item["data_color"])
    ct = _norm(item["code_text"])

    for w in wanted_norm:
        # match “codice” esatto
        if w and (w == dc or w == ct):
            return True
        # match sul titolo (contains)
        if w and w in t:
            return True

    return False


def _wait_after_color_change(page, seconds: int, debug: List[str]) -> None:
    debug.append(f"Wait after click: {seconds}s")
    time.sleep(max(0, int(seconds)))


def _get_main_photo_url(page, product_url: str, timeout_ms: int, debug: List[str]) -> str:
    """
    Prende SOLO la foto principale nella gallery:
      #js_productMainPhoto img.callToZoom
    """
    page.wait_for_selector("#js_productMainPhoto img.callToZoom", timeout=timeout_ms)
    src = page.locator("#js_productMainPhoto img.callToZoom").get_attribute("src") or ""
    src = src.strip()

    if not src:
        return ""

    full = urljoin(product_url, src)
    debug.append(f"Main photo src: {full}")
    return full


def scrape_images_with_login_sync(
    product_url: str,
    email: str,
    password: str,
    wanted_colors: Optional[List[str]] = None,
    wait_after_click_seconds: int = 15,
    headless: bool = True,
    timeout_ms: int = 45000,
) -> ScrapeResult:
    debug: List[str] = []

    chromium_path = _guess_chromium_executable()
    if not chromium_path:
        raise RuntimeError(
            "Chromium non trovato nel container. "
            "Metti 'chromium' in packages.txt oppure imposta CHROME_PATH."
        )

    wanted_norm = [_norm(x) for x in (wanted_colors or []) if _norm(x)]
    debug.append(f"Wanted colors (normalized): {wanted_norm}")

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

        _login_via_modal(page, email=email, password=password, timeout_ms=timeout_ms, debug=debug)

        # Ricarica prodotto con sessione
        debug.append("Reload product page after login")
        page.goto(product_url, wait_until="domcontentloaded", timeout=timeout_ms)
        time.sleep(1.0)

        # Sessione requests con cookie di Playwright
        cookies = context.cookies()
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; ImageDownloader/1.0)",
            "Accept": "*/*",
            "Referer": product_url,
        })
        for c in cookies:
            sess.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path"))

        # Legge swatch e decide cosa cliccare
        swatch_items = _extract_color_swatch_map(page, timeout_ms=timeout_ms, debug=debug)

        # Se l’utente ha lista colori, clicchiamo nell’ORDINE dell’utente.
        # Per farlo, per ogni wanted cerchiamo la prima swatch che matcha.
        ordered_to_click = []
        if wanted_norm:
            for w in wanted_norm:
                match = None
                for it in swatch_items:
                    # match "forte": codice esatto o titolo contiene
                    t = _norm(it["title"])
                    dc = _norm(it["data_color"])
                    ct = _norm(it["code_text"])
                    if w == dc or w == ct or (w in t):
                        match = it
                        break
                if match:
                    ordered_to_click.append(match)
                else:
                    debug.append(f"WARNING: no swatch matched '{w}'")
        else:
            # Se non specifichi lista, clicca tutte le swatch trovate (non consigliato, ma utile)
            ordered_to_click = [it for it in swatch_items if _match_swatch(it, wanted_norm)]

        debug.append(f"Swatches selected for clicking: {len(ordered_to_click)}")

        found_urls: List[str] = []
        downloaded_ok: List[str] = []
        downloaded_failed: List[str] = []

        # ZIP in memoria
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for k, it in enumerate(ordered_to_click, start=1):
                title = it["title"]
                data_color = it["data_color"]
                code_text = it["code_text"]

                label = code_text or data_color or title or f"color_{k}"
                label_clean = _clean_filename(label)

                debug.append(f"[{k}] Click swatch: title='{title}' data-color='{data_color}' code='{code_text}'")

                # click e attesa lunga (come richiesto)
                try:
                    it["handle"].click(timeout=timeout_ms)
                except Exception as e:
                    downloaded_failed.append(f"{label} (click failed: {type(e).__name__})")
                    debug.append(f"[{k}] ERROR click: {type(e).__name__}: {e}")
                    continue

                _wait_after_color_change(page, wait_after_click_seconds, debug)

                # prendi SOLO immagine principale (gallery)
                main_url = _get_main_photo_url(page, product_url=product_url, timeout_ms=timeout_ms, debug=debug)
                if not main_url:
                    downloaded_failed.append(f"{label} (main image not found)")
                    continue

                found_urls.append(main_url)

                # prova a prendere versione hi-res
                cands = _best_image_url_candidates(main_url)
                best = _pick_best_existing_url(sess, cands)
                debug.append(f"[{k}] Best candidate: {best}")

                img_bytes, err = _download_bytes(sess, best)
                if not img_bytes:
                    downloaded_failed.append(f"{best} ({err})")
                    continue

                # estensione
                ext = ".jpg"
                m = re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", best, re.I)
                if m:
                    ext = "." + m.group(1).lower().replace("jpeg", "jpg")

                filename = _clean_filename(f"{k:02d}_{label_clean}{ext}")
                zf.writestr(filename, img_bytes)
                downloaded_ok.append(best)

        mem.seek(0)
        zip_bytes = mem.getvalue()

        browser.close()

        return ScrapeResult(
            zip_bytes=zip_bytes,
            found_image_urls=found_urls,
            downloaded_ok=downloaded_ok,
            downloaded_failed=downloaded_failed,
            debug=debug,
        )
