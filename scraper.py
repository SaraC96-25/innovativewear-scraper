import io
import os
import re
import time
import zipfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# Evita che Playwright provi a scaricare browser in Streamlit Cloud
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
    s = re.sub(r"\s+", " ", s)
    s = s.replace("à", "a").replace("è", "e").replace("é", "e").replace("ì", "i").replace("ò", "o").replace("ù", "u")
    return s


def _wanted_color_match(color_label: str, wanted: List[str]) -> bool:
    """
    wanted: lista di colori target in italiano (nero, bianco, rosso, blu navy, blu royal, grigio).
    Proviamo a matchare con sinonimi/varianti comuni.
    """
    c = _normalize_color_name(color_label)

    synonyms = {
        "nero": ["nero", "black", "noir", "schwarz"],
        "bianco": ["bianco", "white", "blanc", "weiss"],
        "rosso": ["rosso", "red", "rouge", "rot", "bordeaux", "burgundy"],  # bordeaux spesso è “rosso scuro”
        "blu navy": ["navy", "blu navy", "blu notte", "midnight", "marine", "dark blue"],
        "blu royal": ["royal", "blu royal", "royal blue", "blu reale", "electric blue", "azzurro royal"],
        "grigio": ["grigio", "grey", "gray", "antracite", "anthracite", "charcoal", "melange", "mélange"],
    }

    wanted_norm = [_normalize_color_name(w) for w in wanted]
    for w in wanted_norm:
        # match diretto
        if w in c:
            return True
        # match per cluster sinonimi
        for syn in synonyms.get(w, []):
            if syn in c:
                return True
    return False


def _build_color_map_from_html(html: str) -> Dict[str, str]:
    """
    Mappa best-effort: data-color -> label testuale (se presente in pagina).
    Non conosciamo al 100% la struttura del sito, quindi facciamo euristiche.
    """
    soup = BeautifulSoup(html, "lxml")
    out: Dict[str, str] = {}

    # Qualsiasi elemento con attributo data-color che contiene anche un testo utile
    for el in soup.select("[data-color]"):
        dc = str(el.get("data-color") or "").strip()
        if not dc:
            continue

        # prova: titolo/label vicino
        txt = _normalize_color_name(el.get_text(" ", strip=True))
        title = _normalize_color_name(el.get("title") or "")
        aria = _normalize_color_name(el.get("aria-label") or "")
        data_name = _normalize_color_name(el.get("data-name") or "")

        label = next((t for t in [data_name, title, aria, txt] if t), "")
        if label and dc not in out:
            out[dc] = label

    return out


def _extract_gallery_candidates(html: str, base_url: str) -> List[Tuple[str, Optional[str]]]:
    """
    Estrae SOLO immagini della galleria principale.
    Ritorna lista di tuple (url_assoluto, data_color opzionale)
    """
    soup = BeautifulSoup(html, "lxml")
    out: List[Tuple[str, Optional[str]]] = []

    # Target principale che hai indicato
    for img in soup.select("#js_productMainPhoto img.callToZoom, #js_productMainPhoto img"):
        u = img.get("src") or img.get("data-src") or img.get("data-original")
        if not u:
            continue
        out.append((urljoin(base_url, u), str(img.get("data-color") or "").strip() or None))

    # In molti siti ci sono thumb/alt foto con classi simili (callToZoom)
    for img in soup.select("img.callToZoom"):
        u = img.get("src") or img.get("data-src") or img.get("data-original")
        if not u:
            continue
        out.append((urljoin(base_url, u), str(img.get("data-color") or "").strip() or None))

    # Dedup mantenendo ordine
    seen = set()
    uniq: List[Tuple[str, Optional[str]]] = []
    for u, dc in out:
        if u not in seen:
            seen.add(u)
            uniq.append((u, dc))

    return uniq


def _url_variants_highres(url: str) -> List[str]:
    """
    Genera candidate URL "più grandi" a partire da una URL tipo:
    /media/.../opt-490x735-rj265m.jpg

    Strategie:
    - rimuovi prefix opt-WxH-
    - rimuovi qualsiasi opt-...- (generico)
    - prova a sostituire con misure comuni (solo se serve)
    """
    variants = [url]

    # Caso tipico: opt-490x735-<name>.jpg  => <name>.jpg
    v1 = re.sub(r"/opt-\d+x\d+-", "/", url)
    if v1 != url:
        variants.append(v1)

    # Generico: opt-QUALCOSA-  => togli opt-
    v2 = re.sub(r"/opt-[^/]+-", "/", url)
    if v2 != url and v2 not in variants:
        variants.append(v2)

    # Se rimane nel filename: .../opt-490x735-rj265m.jpg
    v3 = re.sub(r"opt-\d+x\d+-", "", url)
    if v3 != url and v3 not in variants:
        variants.append(v3)

    # Alcuni siti hanno “large/zoom” ecc. (best effort)
    variants.append(url.replace("/opt-", "/"))

    # Dedup
    out = []
    seen = set()
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _fetch_image_info(session: requests.Session, url: str) -> Optional[Tuple[bytes, int, int]]:
    """
    Scarica l'immagine e ritorna (bytes, width, height) se è un'immagine valida.
    """
    try:
        r = session.get(url, timeout=30)
        if r.status_code != 200 or not r.content:
            return None
        data = r.content
        im = Image.open(io.BytesIO(data))
        w, h = im.size
        return data, int(w), int(h)
    except Exception:
        return None


def _best_highres_image(session: requests.Session, url: str, debug: List[str]) -> Tuple[str, Optional[bytes]]:
    """
    Prova varianti URL e sceglie quella con area (w*h) maggiore.
    """
    best = None  # (area, w, h, url, bytes)
    for v in _url_variants_highres(url):
        info = _fetch_image_info(session, v)
        if not info:
            continue
        data, w, h = info
        area = w * h
        if (best is None) or (area > best[0]):
            best = (area, w, h, v, data)

    if best:
        debug.append(f"Highres OK: {url} -> {best[3]} ({best[1]}x{best[2]})")
        return best[3], best[4]

    debug.append(f"Highres FAIL: {url} (nessuna variante valida)")
    return url, None


def _download_to_zip_bytes(files: List[Tuple[str, bytes]]) -> bytes:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, (name, content) in enumerate(files, start=1):
            zf.writestr(name, content)
    mem.seek(0)
    return mem.getvalue()


@dataclass
class ScrapeResult:
    zip_bytes: bytes
    found_image_urls: List[str]
    downloaded_ok: List[str]
    downloaded_failed: List[str]
    debug: List[str]

def _safe_click(page, selector: str, timeout_ms: int, debug: List[str]) -> bool:
    try:
        page.click(selector, timeout=timeout_ms)
        return True
    except Exception as e:
        debug.append(f"Click failed: {selector} ({type(e).__name__})")
        return False


def _collect_images_by_clicking_colors(page, base_url: str, timeout_ms: int, debug: List[str]) -> List[Tuple[str, Optional[str]]]:
    """
    Clicca tutte le varianti colore e raccoglie l'immagine principale per ciascuna.
    Ritorna: [(img_url, data_color)]
    """
    results: List[Tuple[str, Optional[str]]] = []

    main_img_sel = "#js_productMainPhoto img"
    page.wait_for_selector(main_img_sel, timeout=timeout_ms)

    def get_main_src_and_color() -> Tuple[Optional[str], Optional[str]]:
        el = page.query_selector(main_img_sel)
        if not el:
            return None, None
        src = el.get_attribute("src") or el.get_attribute("data-src")
        dc = el.get_attribute("data-color")
        if src:
            src = urljoin(base_url, src)
        return src, (dc.strip() if dc else None)

    # 1) prova a prendere tutti i "color swatch" cliccabili (euristica)
    # - qualunque elemento con data-color
    # - link/bottoni/label spesso usati per varianti
    color_selectors = [
        '[data-color]',                 # generico
        'a[data-color]',
        'button[data-color]',
        'label[data-color]',
        'li[data-color]',
        '.js_changeColor [data-color]', # se esiste
        '.colors [data-color]',
        '.color [data-color]',
        '#js_colors [data-color]',
    ]

    # prendi lista unica di elementi (Playwright locator-based)
    handles = []
    seen = set()
    for sel in color_selectors:
        for h in page.query_selector_all(sel):
            # evita duplicati: usa outerHTML hash (grezzo ma funziona)
            try:
                key = h.evaluate("el => el.outerHTML")[:200]
            except Exception:
                key = str(h)
            if key in seen:
                continue
            seen.add(key)
            handles.append(h)

    debug.append(f"Color candidates found: {len(handles)}")

    # Se non troviamo nulla, ritorniamo solo la featured
    if not handles:
        src, dc = get_main_src_and_color()
        if src:
            results.append((src, dc))
        return results

    # 2) clicca ogni variante e aspetta cambio immagine
    baseline_src, _ = get_main_src_and_color()
    collected_src = set()

    for idx, h in enumerate(handles, start=1):
        # alcuni elementi non sono visibili/cliccabili -> prova scroll e click
        try:
            h.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass

        # prova click direttamente sul handle
        old_src, _ = get_main_src_and_color()

        clicked = False
        try:
            h.click(timeout=2000)
            clicked = True
        except Exception:
            # fallback: prova click via JS
            try:
                page.evaluate("(el) => el.click()", h)
                clicked = True
            except Exception:
                clicked = False

        if not clicked:
            continue

        # aspetta che cambi src (o che si stabilizzi)
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
            # non sempre cambia src (alcuni swatch duplicati) => prosegui comunque
            pass

        time.sleep(0.3)

        src, dc = get_main_src_and_color()
        if not src:
            continue

        if src in collected_src:
            continue

        collected_src.add(src)
        results.append((src, dc))
        debug.append(f"[{idx}] Collected main image: {src} (data-color={dc})")

    # se per qualche motivo non ha raccolto nulla, fallback featured
    if not results and baseline_src:
        results.append((baseline_src, None))

    return results


# -----------------------
# Main
# -----------------------

def scrape_images_with_login_sync(
    product_url: str,
    email: str,
    password: str,
    headless: bool = True,
    timeout_ms: int = 30000,
    wanted_colors: Optional[List[str]] = None,
) -> ScrapeResult:
    debug: List[str] = []
    wanted_colors = wanted_colors or ["nero", "bianco", "rosso", "blu navy", "blu royal", "grigio"]

    chromium_path = _guess_chromium_executable()
    if not chromium_path:
        raise RuntimeError(
            "Chromium non trovato nel container. "
            "Aggiungi 'chromium' in packages.txt (obbligatorio) oppure imposta CHROME_PATH."
        )

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

        # Click login trigger (popup/modal)
        debug.append("Click login trigger (popup/modal)")
        page.click("a.login.js_popupLogin", timeout=timeout_ms)

        # Form nel modal che mi hai dato: #user_email, #user_password, submit .js_popupDoLogin
        debug.append("Fill login form in modal")
        page.wait_for_selector("#js_popupSignInBody #user_email", timeout=timeout_ms)
        page.fill("#js_popupSignInBody #user_email", email)
        page.fill("#js_popupSignInBody #user_password", password)

        debug.append("Submit login")
        page.click("#js_popupSignInBody .js_popupDoLogin", timeout=timeout_ms)

        # attesa breve per cookie/sessione
        time.sleep(1.2)

        # reload prodotto con sessione attiva
        debug.append("Reload product after login")
        page.goto(product_url, wait_until="domcontentloaded", timeout=timeout_ms)
        time.sleep(1.0)

        html = page.content()

        # Mappa data-color -> label (best effort)
        color_map = _build_color_map_from_html(html)
        if color_map:
            debug.append(f"Color map candidates: {color_map}")

        # Estrai SOLO galleria
        candidates = _extract_gallery_candidates(html, product_url)
        debug.append(f"Gallery candidates: {len(candidates)}")

        # Filtra per colori desiderati se possibile
        filtered: List[str] = []
        for url, dc in candidates:
            if dc and dc in color_map:
                label = color_map[dc]
                if _wanted_color_match(label, wanted_colors):
                    filtered.append(url)
            else:
                # se non sappiamo il colore, NON scartiamo subito (meglio scaricare che perdere)
                filtered.append(url)

        # Dedup
        filtered = list(dict.fromkeys(filtered))
        debug.append(f"After color filter (best effort): {len(filtered)}")

        # requests session con cookie Playwright
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; ImageDownloader/1.0)",
            "Accept": "*/*",
            "Referer": product_url,
        })
        for c in context.cookies():
            sess.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path"))

        ok_urls: List[str] = []
        failed: List[str] = []
        files_for_zip: List[Tuple[str, bytes]] = []

        for i, url in enumerate(filtered, start=1):
            best_url, data = _best_highres_image(sess, url, debug)
            if not data:
                failed.append(url)
                continue

            # estensione da url finale
            ext = ".jpg"
            m = re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", best_url, re.I)
            if m:
                ext = "." + m.group(1).lower().replace("jpeg", "jpg")

            filename = _clean_filename(f"gallery_{i:03d}{ext}")
            files_for_zip.append((filename, data))
            ok_urls.append(best_url)

        zip_bytes = _download_to_zip_bytes(files_for_zip)
        browser.close()

        return ScrapeResult(
            zip_bytes=zip_bytes,
            found_image_urls=filtered,
            downloaded_ok=ok_urls,
            downloaded_failed=failed,
            debug=debug,
        )
