import streamlit as st
from scraper import scrape_images_with_login_sync

st.set_page_config(page_title="Downloader immagini (login)", layout="centered")
st.title("Downloader immagini (con login)")

st.caption("Clicca solo le varianti che incolli qui sotto, aspetta 15s dopo ogni click, e scarica le immagini principali in alta risoluzione.")

default_email = st.secrets.get("PARTNER_EMAIL", "")
default_password = st.secrets.get("PARTNER_PASSWORD", "")

product_url = st.text_input("URL prodotto", "")
email = st.text_input("Email / codice utente", value=default_email)
password = st.text_input("Password", value=default_password, type="password")

colors_text = st.text_area(
    "Colori da cliccare (uno per riga). Puoi usare nome, codice (CR/FN/BH/CG) o numero (36/30).",
    value="Black\nWhite\nClassic Red\nFrench Navy\nBright Royal\nConvoy Grey",
    height=160,
)

col1, col2 = st.columns([1, 1])
with col1:
    headless = st.checkbox("Headless", value=True)
with col2:
    show_debug = st.checkbox("Mostra debug", value=True)

wait_seconds = st.slider("Attesa dopo click variante (secondi)", min_value=3, max_value=30, value=15, step=1)

if st.button("Scarica ZIP immagini", use_container_width=True, type="primary"):
    if not product_url.strip():
        st.error("Inserisci un URL prodotto.")
        st.stop()
    if not email.strip() or not password.strip():
        st.error("Inserisci credenziali.")
        st.stop()

    wanted_colors = [c.strip() for c in colors_text.splitlines() if c.strip()]

    with st.spinner("Login + click varianti + download immagini..."):
        try:
            result = scrape_images_with_login_sync(
                product_url=product_url.strip(),
                email=email.strip(),
                password=password,
                wanted_colors=wanted_colors,
                wait_after_click_seconds=wait_seconds,
                headless=headless,
            )
        except Exception as e:
            st.error(f"Errore: {type(e).__name__}: {e}")
            st.stop()

    found = result.found_image_urls
    ok = result.downloaded_ok
    failed = result.downloaded_failed
    debug = result.debug
    zip_bytes = result.zip_bytes

    st.success(f"Varianti processate: {len(wanted_colors)} — immagini trovate: {len(found)} — scaricate: {len(ok)} — fallite: {len(failed)}")

    st.download_button(
        "Download ZIP",
        data=zip_bytes,
        file_name="immagini.zip",
        mime="application/zip",
        use_container_width=True,
    )

    if failed:
        with st.expander("URL fallite / errori"):
            st.write("\n".join(failed))

    if show_debug:
        with st.expander("Debug"):
            st.write("\n".join(debug))
