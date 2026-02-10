import streamlit as st
from scraper import scrape_images_with_login_sync as scrape_images_with_login

st.set_page_config(page_title="Downloader immagini (login)", layout="centered")
st.title("Downloader immagini (con login)")

st.caption("Estrae immagini post-login e crea uno ZIP. Se esplode, il debug qui sotto ti dice *dove*.")

default_email = st.secrets.get("PARTNER_EMAIL", "")
default_password = st.secrets.get("PARTNER_PASSWORD", "")

product_url = st.text_input("URL prodotto", "")
email = st.text_input("Email / codice utente", value=default_email)
password = st.text_input("Password", value=default_password, type="password")

col1, col2 = st.columns([1,1])
with col1:
    headless = st.checkbox("Headless", value=True)
with col2:
    show_debug = st.checkbox("Mostra debug", value=True)

if st.button("Scarica ZIP immagini", use_container_width=True, type="primary"):
    if not product_url.strip():
        st.error("Inserisci un URL prodotto.")
        st.stop()
    if not email.strip() or not password.strip():
        st.error("Inserisci credenziali.")
        st.stop()

    with st.spinner("Login + estrazione immagini..."):
    try:
        result = scrape_images_with_login(
            product_url=product_url.strip(),
            email=email.strip(),
            password=password,
            headless=headless,
        )

        zip_bytes = result.zip_bytes
        found = result.found_image_urls
        ok = result.downloaded_ok
        failed = result.downloaded_failed
        debug = result.debug

    except Exception as e:
        st.error(f"Errore: {type(e).__name__}: {e}")
        st.stop()


    st.success(f"Immagini trovate: {len(found)} — scaricate: {len(ok)} — fallite: {len(failed)}")

    st.download_button(
        "Download ZIP",
        data=zip_bytes,
        file_name="immagini.zip",
        mime="application/zip",
        use_container_width=True,
    )

    if failed:
        with st.expander("URL fallite"):
            st.write("\n".join(failed))

    if show_debug:
        with st.expander("Debug"):
            st.write("\n".join(debug))
