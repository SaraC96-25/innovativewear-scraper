import streamlit as st
import inspect
import iw_scraper
from iw_scraper import scrape_images_with_login_sync

st.set_page_config(page_title="Downloader immagini (login)", layout="centered")
st.title("Downloader immagini (con login)")

st.caption("Estrae la foto principale della gallery per ogni variante colore e crea uno ZIP.")
st.caption(f"Scraper file: {iw_scraper.__file__}")
st.caption(f"Signature: {inspect.signature(scrape_images_with_login_sync)}")

default_email = st.secrets.get("PARTNER_EMAIL", "")
default_password = st.secrets.get("PARTNER_PASSWORD", "")

product_url = st.text_input("URL prodotto", "")
email = st.text_input("Email / codice utente", value=default_email)
password = st.text_input("Password", value=default_password, type="password")

wanted_text = st.text_area(
    "Colori / codici da cliccare (uno per riga o separati da virgola)",
    value="black\nwhite\nclassic red\nfrench navy\nbright royal\nconvoy grey",
    height=140
)

wait_seconds = st.number_input("Attesa dopo click variante (secondi)", min_value=0, max_value=60, value=15, step=1)

col1, col2 = st.columns([1, 1])
with col1:
    headless = st.checkbox("Headless", value=True)
with col2:
    show_debug = st.checkbox("Mostra debug", value=True)

def parse_wanted(text: str):
    if not text:
        return []
    # supporta newline e virgole
    raw = []
    for line in text.splitlines():
        raw.extend([x.strip() for x in line.split(",")])
    return [x for x in raw if x]

if st.button("Scarica ZIP immagini", use_container_width=True, type="primary"):
    if not product_url.strip():
        st.error("Inserisci un URL prodotto.")
        st.stop()
    if not email.strip() or not password.strip():
        st.error("Inserisci credenziali.")
        st.stop()

    wanted_colors = parse_wanted(wanted_text)

    with st.spinner("Login + click varianti + download immagini..."):
        try:
            result = scrape_images_with_login_sync(
                product_url=product_url.strip(),
                email=email.strip(),
                password=password,
                wanted_colors=wanted_colors,
                wait_after_click_seconds=int(wait_seconds),
                headless=headless,
            )
        except Exception as e:
            st.error(f"Errore: {type(e).__name__}: {e}")
            st.stop()

    st.success(
        f"Varianti trovate: {len(wanted_colors) if wanted_colors else 'tutte'} — "
        f"immagini scaricate: {len(result.downloaded_ok)} — fallite: {len(result.downloaded_failed)}"
    )

    st.download_button(
        "Download ZIP",
        data=result.zip_bytes,
        file_name="immagini.zip",
        mime="application/zip",
        use_container_width=True,
    )

    if result.downloaded_failed:
        with st.expander("URL fallite"):
            st.write("\n".join(result.downloaded_failed))

    if show_debug:
        with st.expander("Debug"):
            st.write("\n".join(result.debug))
