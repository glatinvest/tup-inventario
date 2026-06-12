
import streamlit as st
import pandas as pd
from datetime import date
import re
import tempfile
import xml.etree.ElementTree as ET

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

from supabase import create_client, Client


# =========================
# CONFIG
# =========================

st.set_page_config(
    page_title="T'up Burger - Inventario Online",
    layout="wide"
)

CATEGORIE = [
    "Carne",
    "Pane",
    "Formaggi",
    "Salumi",
    "Verdure",
    "Salse",
    "Fritti",
    "Packaging",
    "Bevande",
    "Pulizia",
    "Altro",
]

UNITA = ["kg", "g", "pz", "conf", "lt", "ml", "CT", "TA", "CF", "NR"]
TIPI = ["Food", "No Food"]
PUNTI_VENDITA = ["De Cosmi", "Via Roma"]

UTENTI = {
    "Admin": {"password": "tupadmin", "store": "Tutti"},
    "De Cosmi": {"password": "decosmi", "store": "De Cosmi"},
    "Via Roma": {"password": "viaroma", "store": "Via Roma"},
}


# =========================
# SUPABASE
# =========================

@st.cache_resource
def get_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)


def sb():
    return get_supabase()


def fetch_table(table):
    try:
        res = sb().table(table).select("*").execute()
        return pd.DataFrame(res.data)
    except Exception as e:
        st.error(f"Errore lettura tabella {table}: {e}")
        return pd.DataFrame()


def insert_rows(table, rows):
    if not rows:
        return
    sb().table(table).insert(rows).execute()


def update_row(table, row_id, values):
    sb().table(table).update(values).eq("id", row_id).execute()


def delete_rows_by_ids(table, ids):
    if not ids:
        return
    sb().table(table).delete().in_("id", ids).execute()


def delete_inventory(data_inventario, punto_vendita):
    sb().table("inventari").delete() \
        .eq("data_inventario", data_inventario) \
        .eq("punto_vendita", punto_vendita) \
        .execute()


def delete_references_by_ids(ids):
    if not ids:
        return
    # elimina anche inventari collegati
    referenze = fetch_table("referenze")
    codici = referenze[referenze["id"].isin(ids)]["codice"].astype(str).tolist()
    if codici:
        sb().table("inventari").delete().in_("codice", codici).execute()
        sb().table("trasferimenti").delete().in_("codice", codici).execute()
    delete_rows_by_ids("referenze", ids)


# =========================
# UTILITY
# =========================

def pulisci_testo(testo):
    return str(testo).strip()


def nome_match(testo):
    s = str(testo).lower().strip()
    s = re.sub(r"[^a-z0-9àèéìòù\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s


def pulisci_numero(valore):
    if pd.isna(valore):
        return 0.0

    s = str(valore).strip()
    s = s.replace("€", "").replace(" ", "")

    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")

    try:
        return float(s)
    except Exception:
        return 0.0


def genera_codice(nome, df):
    base = "".join([c for c in str(nome).upper() if c.isalnum()])[:8]
    if not base:
        base = "REF"

    codice = base
    i = 1

    existing = []
    if not df.empty and "codice" in df.columns:
        existing = df["codice"].astype(str).values

    while codice in existing:
        codice = f"{base}{i}"
        i += 1

    return codice


def normalizza_unita(u):
    u = str(u).strip().upper().replace(".", "")
    if u in ["N", "NR", "PZ", "PEZZI"]:
        return "pz"
    if u in ["K", "KG", "KGS", "CHG"]:
        return "kg"
    if u in ["L", "LT"]:
        return "lt"
    if u in ["CF", "CONF", "CONFEZIONE"]:
        return "conf"
    if u in ["CT", "TA"]:
        return u
    return str(u).strip() or "pz"


def tipo_default_da_fornitore(nome_fornitore):
    f = str(nome_fornitore).lower()
    if any(x in f for x in ["carta", "imballaggi", "nasta", "packaging", "panzini", "gpcarta"]):
        return "No Food"
    return "Food"


def euro(valore):
    try:
        return f"€ {float(valore):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "€ 0,00"


def stores_visibili():
    ruolo = st.session_state.get("ruolo", "Admin")
    store = UTENTI.get(ruolo, {}).get("store", "Tutti")
    if store == "Tutti":
        return PUNTI_VENDITA
    return [store]


def ultimo_inventario_per_store(inventari, store):
    if inventari.empty:
        return pd.DataFrame(), ""

    df = inventari[inventari["punto_vendita"].astype(str) == store].copy()

    if df.empty:
        return pd.DataFrame(), ""

    data_ultima = sorted(df["data_inventario"].astype(str).unique().tolist())[-1]
    return df[df["data_inventario"].astype(str) == data_ultima].copy(), data_ultima


# =========================
# LETTURA FATTURE
# =========================

def estrai_testo_pdf(file_bytes):
    if pdfplumber is None:
        raise ImportError("pdfplumber non installato")

    testo = ""

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    with pdfplumber.open(tmp_path) as pdf:
        for page in pdf.pages:
            testo += (page.extract_text(x_tolerance=2, y_tolerance=3) or "") + "\n"

            try:
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        celle = [str(c).strip() for c in row if c is not None and str(c).strip()]
                        if celle:
                            testo += " | ".join(celle) + "\n"
            except Exception:
                pass

    return testo


def estrai_fornitore(testo, nome_file):
    righe = [r.strip() for r in testo.splitlines() if r.strip()]

    parole_da_evitare = [
        "fattura",
        "documento",
        "partita iva",
        "codice fiscale",
        "cliente",
        "destinatario",
        "spett.le",
        "totale",
        "iban",
        "pagamento",
        "t'up",
        "tup",
    ]

    # Priorità ai dati dentro la sezione FORNITORE, non all'intermediario che ha emesso il PDF
    for i, r in enumerate(righe):
        if r.strip().lower() == "fornitore" and i + 1 < len(righe):
            return righe[i + 1].strip()[:100]

    for r in righe[:80]:
        rl = r.lower()

        if len(r) < 3:
            continue

        if any(p in rl for p in parole_da_evitare):
            continue

        if re.search(r"\b(srl|s\.r\.l\.|spa|s\.p\.a\.|sas|snc|ditta|azienda|panzini|nasta|marr|dac)\b", rl):
            return r[:100]

    return nome_file[:100]


def estrai_referenze_xml(file_bytes, nome_file=""):
    risultati = []

    try:
        root = ET.fromstring(file_bytes)
    except Exception:
        return risultati

    def strip_ns(tag):
        return tag.split("}", 1)[-1] if "}" in tag else tag

    def all_by_name(node, name):
        return [el for el in node.iter() if strip_ns(el.tag) == name]

    def child_text(node, name, default=""):
        for child in list(node):
            if strip_ns(child.tag) == name:
                return (child.text or default).strip()
        return default

    fornitore = nome_file[:100]
    cedenti = all_by_name(root, "CedentePrestatore")

    if cedenti:
        anag = all_by_name(cedenti[0], "Anagrafica")
        if anag:
            denom = child_text(anag[0], "Denominazione", "")
            if denom:
                fornitore = denom

    dettagli = all_by_name(root, "DettaglioLinee")

    for det in dettagli:
        descrizione = child_text(det, "Descrizione", "")
        quantita = pulisci_numero(child_text(det, "Quantita", "0"))
        unita = child_text(det, "UnitaMisura", "pz")
        prezzo = pulisci_numero(child_text(det, "PrezzoUnitario", "0"))
        importo = pulisci_numero(child_text(det, "PrezzoTotale", "0"))
        iva = child_text(det, "AliquotaIVA", "")

        codice_fornitore = ""
        for cod_art in [c for c in list(det) if strip_ns(c.tag) == "CodiceArticolo"]:
            for c in list(cod_art):
                if strip_ns(c.tag) == "CodiceValore":
                    codice_fornitore = (c.text or "").strip()

        if not descrizione:
            continue

        descr_low = descrizione.lower()
        if any(x in descr_low for x in ["ordine cliente", "preventivo", "spese di trasporto", "trasporto"]):
            continue

        risultati.append({
            "importa": True,
            "referenza": descrizione[:120],
            "tipo": tipo_default_da_fornitore(fornitore),
            "categoria": "Altro",
            "unita": normalizza_unita(unita),
            "fornitore": fornitore,
            "codice_fornitore": codice_fornitore,
            "prezzo_unitario": prezzo,
            "ultima_quantita": quantita,
            "ultimo_importo": importo,
            "iva": str(iva).replace(".", ","),
            "attivo": "si",
        })

    return risultati


def estrai_referenze_pdf(testo, fornitore):
    righe_raw = [r.strip() for r in testo.splitlines() if r and r.strip()]
    risultati = []

    righe = []
    dentro_prodotti = False
    stop_section = [
        "metodo di pagamento",
        "riepilogo iva",
        "calcolo fattura",
        "regime fiscale",
        "dati aggiuntivi",
        "documenti correlati",
        "allegati",
        "causale documento",
        "dati trasporto",
        "totale documento",
        "netto a pagare",
    ]

    for r in righe_raw:
        rl = r.lower()

        if "prodotti e servizi" in rl or "dettaglio linee" in rl:
            dentro_prodotti = True
            continue

        if dentro_prodotti and any(x in rl for x in stop_section):
            break

        if dentro_prodotti:
            if not rl.startswith("nr descrizione"):
                righe.append(r)

    if not righe:
        righe = righe_raw

    product_re = re.compile(
        r"^(\d{1,6})\s+"                         # numero riga: 1 oppure 0001
        r"(.+?)\s+"                               # descrizione
        r"(\d+(?:[.,]\d+)?)\s+"                  # quantità
        r"([A-Za-zÀ-ÿ.]+)\s+"                     # unità, anche K. / KG. / PZ
        r"(\d+(?:[.,]\d{1,6})?)\s*€\s+"         # prezzo unitario
        r"(?:-|\d+(?:[.,]\d{1,2})?\s*%)?\s*"   # eventuale sconto o trattino
        r"(\d+(?:[.,]\d{1,6})?)\s*€\s+"         # importo
        r"(\d+(?:[.,]\d{1,2})?)\s*%"             # iva
    )

    codice_re = re.compile(r"Cod\.valore:\s*([A-Za-z0-9_-]+)")

    corrente = None

    for r in righe:
        r = re.sub(r"\s+", " ", r).strip()
        rl = r.lower()

        if not r:
            continue

        if rl.startswith("nr descrizione") or rl.startswith("copia analogica") or rl.startswith("fattura nr"):
            continue

        cod_match = codice_re.search(r)
        if cod_match and corrente is not None:
            corrente["codice_fornitore"] = cod_match.group(1).strip()
            risultati.append(corrente)
            corrente = None
            continue

        m = product_re.search(r)

        if m:
            if corrente is not None:
                risultati.append(corrente)

            descrizione = re.sub(r"\s+", " ", m.group(2)).strip()[:120]
            descr_low = descrizione.lower()

            if any(x in descr_low for x in [
                "ordine cliente",
                "preventivo",
                "spese di trasporto",
                "trasporto",
                "spesa accessoria",
            ]):
                corrente = None
                continue

            quantita = pulisci_numero(m.group(3))
            unita = normalizza_unita(m.group(4))
            prezzo = pulisci_numero(m.group(5))
            importo = pulisci_numero(m.group(6))
            iva = str(m.group(7)).replace(".", ",")

            if prezzo <= 0 and importo <= 0:
                if not any(x in descr_low for x in ["dispenser", "campione", "omaggio", "kit"]):
                    corrente = None
                    continue

            if len(re.findall(r"[a-zA-ZàèéìòùÀÈÉÌÒÙ]", descrizione)) < 3:
                corrente = None
                continue

            corrente = {
                "importa": True,
                "referenza": descrizione,
                "tipo": tipo_default_da_fornitore(fornitore),
                "categoria": "Altro",
                "unita": unita,
                "fornitore": fornitore,
                "codice_fornitore": "",
                "prezzo_unitario": prezzo,
                "ultima_quantita": quantita,
                "ultimo_importo": importo,
                "iva": iva,
                "attivo": "si",
            }
            continue

        if corrente is not None:
            if any(x in rl for x in [
                "tipo dato",
                "riferimento testo",
                "riferimento numero",
                "cod.tipo",
                "cod.valore",
                "metodo di pagamento",
                "regime fiscale",
            ]):
                continue

            if len(re.findall(r"[a-zA-ZàèéìòùÀÈÉÌÒÙ]", r)) >= 2 and not re.match(r"^\d+\s+", r):
                corrente["referenza"] = (corrente["referenza"] + " " + r)[:120]

    if corrente is not None:
        risultati.append(corrente)

    puliti = []
    visti = set()

    for item in risultati:
        key = (nome_match(item.get("referenza", "")), str(item.get("codice_fornitore", "")))
        if key in visti:
            continue
        visti.add(key)
        puliti.append(item)

    return puliti


# =========================
# APP
# =========================

st.title("🍔 T'up Burger - Inventario Online")

if "loggato" not in st.session_state:
    st.session_state["loggato"] = False

if not st.session_state["loggato"]:
    st.subheader("Accesso")
    ruolo = st.selectbox("Utente", list(UTENTI.keys()))
    password = st.text_input("Password", type="password")

    if st.button("Entra"):
        if password == UTENTI[ruolo]["password"]:
            st.session_state["loggato"] = True
            st.session_state["ruolo"] = ruolo
            st.rerun()
        else:
            st.error("Password errata.")

    st.stop()

with st.sidebar:
    st.caption("Database: Supabase")
    st.success(f"Accesso: {st.session_state.get('ruolo', 'Admin')}")

    if st.button("Logout"):
        st.session_state["loggato"] = False
        st.session_state["ruolo"] = None
        st.rerun()

    menu = st.radio(
        "Menu",
        [
            "Dashboard",
            "Import fatture",
            "Anagrafica referenze",
            "Inventario mensile",
            "Trasferimenti merci",
            "Export",
        ]
    )




# =========================
# DASHBOARD
# =========================

if menu == "Dashboard":
    st.subheader("Dashboard magazzino")

    referenze = fetch_table("referenze")
    inventari = fetch_table("inventari")
    trasferimenti = fetch_table("trasferimenti")

    if referenze.empty:
        st.info("Non ci sono ancora referenze in anagrafica.")
    else:
        ref = referenze.copy()

        if "prezzo_unitario" not in ref.columns:
            ref["prezzo_unitario"] = 0.0
        if "scorta_minima" not in ref.columns:
            ref["scorta_minima"] = 0.0

        ref["prezzo_unitario"] = pd.to_numeric(ref["prezzo_unitario"], errors="coerce").fillna(0)
        ref["scorta_minima"] = pd.to_numeric(ref["scorta_minima"], errors="coerce").fillna(0)

        totale_generale = 0

        for store in stores_visibili():
            st.markdown(f"### {store}")

            ultimo, data_ultima = ultimo_inventario_per_store(inventari, store)

            if ultimo.empty:
                st.info(f"Nessun inventario salvato per {store}.")
                continue

            ultimo["quantita"] = pd.to_numeric(ultimo["quantita"], errors="coerce").fillna(0)

            valorizzato = ultimo.merge(
                ref[["codice", "prezzo_unitario", "scorta_minima"]],
                on="codice",
                how="left"
            )

            valorizzato["prezzo_unitario"] = pd.to_numeric(valorizzato["prezzo_unitario"], errors="coerce").fillna(0)
            valorizzato["scorta_minima"] = pd.to_numeric(valorizzato["scorta_minima"], errors="coerce").fillna(0)
            valorizzato["valore"] = valorizzato["quantita"] * valorizzato["prezzo_unitario"]

            valore_store = valorizzato["valore"].sum()
            totale_generale += valore_store

            food_valore = valorizzato[valorizzato["tipo"].astype(str).str.lower() == "food"]["valore"].sum()
            nofood_valore = valorizzato[valorizzato["tipo"].astype(str).str.lower() == "no food"]["valore"].sum()

            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Ultimo inventario", data_ultima)
            col2.metric("Valore totale", euro(valore_store))
            col3.metric("Food", euro(food_valore))
            col4.metric("No Food", euro(nofood_valore))

            sotto_scorta = valorizzato[
                (valorizzato["scorta_minima"] > 0)
                & (valorizzato["quantita"] <= valorizzato["scorta_minima"])
            ].copy()

            if not sotto_scorta.empty:
                st.warning(f"Referenze sotto scorta: {len(sotto_scorta)}")
                st.dataframe(
                    sotto_scorta[[
                        "referenza", "tipo", "categoria", "unita",
                        "quantita", "scorta_minima", "prezzo_unitario", "valore"
                    ]],
                    use_container_width=True
                )

            with st.expander(f"Dettaglio valorizzato {store}"):
                st.dataframe(
                    valorizzato[[
                        "referenza", "tipo", "categoria", "unita",
                        "quantita", "prezzo_unitario", "valore", "note"
                    ]].sort_values(["tipo", "categoria", "referenza"]),
                    use_container_width=True
                )

        st.divider()
        st.metric("Valore magazzino totale visibile", euro(totale_generale))

        if not trasferimenti.empty:
            st.subheader("Ultimi trasferimenti")
            view_tr = trasferimenti.copy()

            visibili = stores_visibili()
            if visibili != PUNTI_VENDITA:
                store = visibili[0]
                view_tr = view_tr[
                    (view_tr["da_punto_vendita"] == store)
                    | (view_tr["a_punto_vendita"] == store)
                ]

            if view_tr.empty:
                st.info("Nessun trasferimento per questo punto vendita.")
            else:
                st.dataframe(
                    view_tr.sort_values("data_trasferimento", ascending=False).head(20),
                    use_container_width=True
                )

# =========================
# IMPORT FATTURE
# =========================

elif menu == "Import fatture":
    st.subheader("Import referenze da fatture PDF o XML")

    st.write(
        "Carica le fatture. L'app prova a riconoscere solo le righe prodotto. "
        "Prima di salvare puoi togliere la spunta alle righe sbagliate."
    )

    files = st.file_uploader(
        "Carica fatture PDF o XML",
        type=["pdf", "xml"],
        accept_multiple_files=True,
    )

    if files:
        righe = []

        for f in files:
            try:
                file_bytes = f.read()
                nome_file = f.name.lower()

                if nome_file.endswith(".xml"):
                    righe_file = estrai_referenze_xml(file_bytes, f.name)
                else:
                    testo = estrai_testo_pdf(file_bytes)
                    fornitore = estrai_fornitore(testo, f.name)
                    righe_file = estrai_referenze_pdf(testo, fornitore)

                righe.extend(righe_file)

                if not righe_file:
                    st.warning(f"Nessuna referenza trovata in {f.name}")

            except Exception as e:
                st.warning(f"Errore lettura {f.name}: {e}")

        if not righe:
            st.warning("Non ho trovato referenze leggibili.")
        else:
            df = pd.DataFrame(righe)
            if "scorta_minima" not in df.columns:
                df["scorta_minima"] = 0.0
            df["match"] = df["referenza"].apply(nome_match) + "|" + df["fornitore"].astype(str).str.lower()
            df = df.drop_duplicates("match").drop(columns=["match"])

            colonne = [
                "importa",
                "referenza",
                "tipo",
                "categoria",
                "unita",
                "fornitore",
                "codice_fornitore",
                "prezzo_unitario",
                "ultima_quantita",
                "ultimo_importo",
                "iva",
                "scorta_minima",
                "attivo",
            ]
            df = df[[c for c in colonne if c in df.columns]]

            st.success(f"Possibili referenze trovate: {len(df)}")

            df_edit = st.data_editor(
                df,
                use_container_width=True,
                num_rows="dynamic",
                column_config={
                    "importa": st.column_config.CheckboxColumn("importa"),
                    "tipo": st.column_config.SelectboxColumn("tipo", options=TIPI),
                    "categoria": st.column_config.SelectboxColumn("categoria", options=CATEGORIE),
                    "unita": st.column_config.SelectboxColumn("unita", options=UNITA),
                },
                key="fatture_editor",
            )

            if st.button("Salva in anagrafica"):
                referenze = fetch_table("referenze")
                df_da_salvare = df_edit[df_edit["importa"] == True].copy() if "importa" in df_edit.columns else df_edit.copy()

                inserite = 0
                aggiornate = 0
                saltate = 0
                errori = []

                if referenze.empty:
                    referenze = pd.DataFrame(columns=[
                        "id", "codice", "referenza", "codice_fornitore", "fornitore"
                    ])

                # Campi di appoggio per confronti robusti
                referenze["match_nome"] = referenze["referenza"].apply(nome_match) if "referenza" in referenze.columns else ""
                referenze["match_codice_fornitore"] = referenze["codice_fornitore"].fillna("").astype(str).str.strip() if "codice_fornitore" in referenze.columns else ""
                referenze["match_codice"] = referenze["codice"].fillna("").astype(str).str.strip() if "codice" in referenze.columns else ""

                for _, row in df_da_salvare.iterrows():
                    nome = pulisci_testo(row.get("referenza", ""))
                    if not nome:
                        saltate += 1
                        continue

                    codice_fornitore = str(row.get("codice_fornitore", "")).strip()
                    fornitore = str(row.get("fornitore", "")).strip()
                    match_nome = nome_match(nome)

                    # Usa il codice fornitore come codice interno quando disponibile.
                    # Così evitiamo doppioni e il codice resta stabile tra fatture.
                    if codice_fornitore:
                        codice = codice_fornitore
                    else:
                        codice = genera_codice(nome, referenze)

                    valori = {
                        "codice": codice,
                        "referenza": nome,
                        "tipo": row.get("tipo", "Food"),
                        "categoria": row.get("categoria", "Altro"),
                        "unita": row.get("unita", "pz"),
                        "fornitore": fornitore,
                        "codice_fornitore": codice_fornitore,
                        "prezzo_unitario": pulisci_numero(row.get("prezzo_unitario", 0)),
                        "ultima_quantita": pulisci_numero(row.get("ultima_quantita", 0)),
                        "ultimo_importo": pulisci_numero(row.get("ultimo_importo", 0)),
                        "iva": str(row.get("iva", "")).strip(),
                        "scorta_minima": pulisci_numero(row.get("scorta_minima", 0)),
                        "attivo": "si",
                    }

                    existing = pd.DataFrame()

                    # 1) Primo controllo: stesso codice interno
                    if codice:
                        existing = referenze[referenze["match_codice"] == str(codice)]

                    # 2) Secondo controllo: stesso codice fornitore
                    if existing.empty and codice_fornitore:
                        existing = referenze[
                            referenze["match_codice_fornitore"] == codice_fornitore
                        ]

                    # 3) Terzo controllo: stesso nome normalizzato
                    if existing.empty:
                        existing = referenze[
                            referenze["match_nome"] == match_nome
                        ]

                    try:
                        if not existing.empty:
                            existing_id = existing.iloc[0]["id"]
                            update_row("referenze", existing_id, valori)
                            aggiornate += 1
                        else:
                            insert_rows("referenze", [valori])
                            inserite += 1

                            # Aggiorna anche il dataframe locale per evitare duplicati nello stesso batch
                            nuova_locale = valori.copy()
                            nuova_locale["id"] = None
                            nuova_locale["match_nome"] = match_nome
                            nuova_locale["match_codice_fornitore"] = codice_fornitore
                            nuova_locale["match_codice"] = str(codice)
                            referenze = pd.concat([referenze, pd.DataFrame([nuova_locale])], ignore_index=True)

                    except Exception as e:
                        errori.append(f"{nome}: {e}")

                if errori:
                    st.warning(
                        f"Import completato parzialmente. Inserite: {inserite}. "
                        f"Aggiornate: {aggiornate}. Saltate: {saltate}. Errori: {len(errori)}"
                    )
                    with st.expander("Dettaglio errori"):
                        for err in errori:
                            st.write(err)
                else:
                    st.success(
                        f"Salvato. Nuove referenze: {inserite}. "
                        f"Aggiornate: {aggiornate}. Saltate: {saltate}."
                    )


# =========================
# ANAGRAFICA
# =========================

elif menu == "Anagrafica referenze":
    st.subheader("Anagrafica referenze")

    tab1, tab2, tab3 = st.tabs(["Aggiungi manualmente", "Tabella referenze", "Elimina refusi"])

    with tab1:
        with st.form("form_referenza"):
            referenza = st.text_input("Referenza")
            tipo = st.selectbox("Tipo", TIPI)
            categoria = st.selectbox("Categoria", CATEGORIE)
            unita = st.selectbox("Unità", UNITA)
            fornitore = st.text_input("Fornitore")
            codice_fornitore = st.text_input("Codice fornitore")
            prezzo = st.number_input("Prezzo unitario", min_value=0.0, step=0.01)
            scorta_minima = st.number_input("Scorta minima", min_value=0.0, step=0.1)

            salva = st.form_submit_button("Aggiungi referenza")

            if salva:
                referenze = fetch_table("referenze")
                nome = pulisci_testo(referenza)

                if not nome:
                    st.error("Inserisci una referenza.")
                else:
                    codice = genera_codice(nome, referenze)

                    insert_rows("referenze", [{
                        "codice": codice,
                        "referenza": nome,
                        "tipo": tipo,
                        "categoria": categoria,
                        "unita": unita,
                        "fornitore": fornitore,
                        "codice_fornitore": codice_fornitore,
                        "prezzo_unitario": prezzo,
                        "ultima_quantita": 0.0,
                        "ultimo_importo": 0.0,
                        "iva": "",
                        "scorta_minima": scorta_minima,
                        "attivo": "si",
                    }])
                    st.success("Referenza aggiunta.")

    with tab2:
        referenze = fetch_table("referenze")

        if referenze.empty:
            st.info("Nessuna referenza presente.")
        else:
            filtro = st.text_input("Cerca")

            view = referenze.copy()

            if filtro:
                view = view[
                    view.astype(str).apply(
                        lambda row: row.str.lower().str.contains(filtro.lower()).any(),
                        axis=1,
                    )
                ]

            edited = st.data_editor(
                view,
                use_container_width=True,
                num_rows="dynamic",
                column_config={
                    "tipo": st.column_config.SelectboxColumn("tipo", options=TIPI),
                    "categoria": st.column_config.SelectboxColumn("categoria", options=CATEGORIE),
                    "unita": st.column_config.SelectboxColumn("unita", options=UNITA),
                },
                disabled=["id", "created_at"],
                key="referenze_editor",
            )

            if st.button("Salva anagrafica"):
                for _, row in edited.iterrows():
                    if "id" not in row or pd.isna(row["id"]):
                        continue

                    update_row("referenze", row["id"], {
                        "codice": str(row.get("codice", "")),
                        "referenza": str(row.get("referenza", "")),
                        "tipo": str(row.get("tipo", "Food")),
                        "categoria": str(row.get("categoria", "Altro")),
                        "unita": str(row.get("unita", "pz")),
                        "fornitore": str(row.get("fornitore", "")),
                        "codice_fornitore": str(row.get("codice_fornitore", "")),
                        "prezzo_unitario": pulisci_numero(row.get("prezzo_unitario", 0)),
                        "ultima_quantita": pulisci_numero(row.get("ultima_quantita", 0)),
                        "ultimo_importo": pulisci_numero(row.get("ultimo_importo", 0)),
                        "iva": str(row.get("iva", "")),
                        "scorta_minima": pulisci_numero(row.get("scorta_minima", 0)),
                        "attivo": str(row.get("attivo", "si")),
                    })

                st.success("Anagrafica salvata.")

    with tab3:
        st.warning("Elimina solo refusi. La cancellazione rimuove anche inventari e trasferimenti collegati.")

        referenze = fetch_table("referenze")

        if referenze.empty:
            st.info("Nessuna referenza da eliminare.")
        else:
            cerca = st.text_input("Cerca referenza da eliminare")
            view = referenze.copy()

            if cerca:
                view = view[
                    view.astype(str).apply(
                        lambda row: row.str.lower().str.contains(cerca.lower()).any(),
                        axis=1,
                    )
                ]

            view = view.copy()
            view.insert(0, "elimina", False)

            edited_del = st.data_editor(
                view,
                use_container_width=True,
                hide_index=True,
                disabled=[c for c in view.columns if c != "elimina"],
                column_config={
                    "elimina": st.column_config.CheckboxColumn("elimina")
                },
                key="elimina_refusi_editor",
            )

            ids = edited_del[edited_del["elimina"] == True]["id"].tolist()

            conferma = st.checkbox("Confermo eliminazione definitiva")

            if st.button("Elimina referenze selezionate"):
                if not ids:
                    st.warning("Non hai selezionato nessuna referenza.")
                elif not conferma:
                    st.warning("Devi confermare.")
                else:
                    delete_references_by_ids(ids)
                    st.success("Referenze eliminate.")


# =========================
# INVENTARIO MENSILE
# =========================

elif menu == "Inventario mensile":
    st.subheader("Inventario mensile")

    referenze = fetch_table("referenze")
    inventari = fetch_table("inventari")

    if referenze.empty:
        st.info("Prima inserisci o importa le referenze.")
    else:
        data_inventario = st.date_input("Data inventario", value=date.today())
        data_str = data_inventario.strftime("%Y-%m-%d")
        mese = data_inventario.month
        anno = data_inventario.year

        st.caption(f"Inventario del mese: {mese:02d}/{anno}")

        attive = referenze[referenze["attivo"].astype(str).str.lower() != "no"].copy()
        attive = attive.sort_values(["tipo", "categoria", "referenza"])

        store_list = stores_visibili()
        store_tabs = st.tabs(store_list)

        edited_tables = []

        def render_store_inventory(punto_vendita, store_key):
            st.markdown(f"### {punto_vendita}")

            base = attive[["codice", "referenza", "tipo", "categoria", "unita"]].copy()
            base.insert(0, "punto_vendita", punto_vendita)
            base["quantita"] = 0.0
            base["note"] = ""

            if not inventari.empty:
                esistente = inventari[
                    (inventari["data_inventario"].astype(str) == data_str)
                    & (inventari["punto_vendita"].astype(str) == punto_vendita)
                ].copy()
            else:
                esistente = pd.DataFrame()

            if not esistente.empty:
                base = base.drop(columns=["quantita", "note"]).merge(
                    esistente[["codice", "quantita", "note"]],
                    on="codice",
                    how="left",
                )
                base["quantita"] = pd.to_numeric(base["quantita"], errors="coerce").fillna(0)
                base["note"] = base["note"].fillna("")

            tab_food, tab_no_food = st.tabs(["Food", "No Food"])

            def editor_inventario(tipo_nome, key_suffix):
                tabella = base[base["tipo"].astype(str).str.lower() == tipo_nome.lower()].copy()

                if tabella.empty:
                    st.info(f"Nessuna referenza {tipo_nome} presente per {punto_vendita}.")
                    return pd.DataFrame(columns=base.columns)

                filtro_categoria = st.multiselect(
                    f"Filtra categoria {tipo_nome}",
                    sorted(tabella["categoria"].dropna().unique().tolist()),
                    key=f"filtro_{store_key}_{key_suffix}_{data_str}",
                )

                if filtro_categoria:
                    tabella = tabella[tabella["categoria"].isin(filtro_categoria)]

                return st.data_editor(
                    tabella,
                    use_container_width=True,
                    hide_index=True,
                    disabled=["punto_vendita", "codice", "referenza", "tipo", "categoria", "unita"],
                    column_config={
                        "quantita": st.column_config.NumberColumn(
                            "Quantità",
                            min_value=0.0,
                            step=0.1,
                        )
                    },
                    key=f"inventario_{store_key}_{key_suffix}_{data_str}",
                )

            with tab_food:
                edited_tables.append(editor_inventario("Food", "food"))

            with tab_no_food:
                edited_tables.append(editor_inventario("No Food", "no_food"))

        for tab, store in zip(store_tabs, store_list):
            with tab:
                key_store = store.lower().replace(" ", "_")
                render_store_inventory(store, key_store)

        if st.button("Salva inventario mensile"):
            edited = pd.concat(edited_tables, ignore_index=True)

            if edited.empty:
                st.warning("Non ci sono righe da salvare.")
            else:
                # cancella e riscrive solo le righe interessate
                for _, row in edited.iterrows():
                    sb().table("inventari").delete() \
                        .eq("data_inventario", data_str) \
                        .eq("punto_vendita", str(row["punto_vendita"])) \
                        .eq("codice", str(row["codice"])) \
                        .execute()

                rows = []
                for _, row in edited.iterrows():
                    rows.append({
                        "data_inventario": data_str,
                        "mese": mese,
                        "anno": anno,
                        "punto_vendita": str(row["punto_vendita"]),
                        "codice": str(row["codice"]),
                        "referenza": str(row["referenza"]),
                        "tipo": str(row["tipo"]),
                        "categoria": str(row["categoria"]),
                        "unita": str(row["unita"]),
                        "quantita": pulisci_numero(row.get("quantita", 0)),
                        "note": str(row.get("note", "")),
                    })

                insert_rows("inventari", rows)
                st.success("Inventario mensile salvato.")

        st.divider()
        st.subheader("Inventari salvati")

        inventari = fetch_table("inventari")

        if inventari.empty:
            st.info("Nessun inventario salvato.")
        else:
            chiavi = (
                inventari[["data_inventario", "punto_vendita"]]
                .drop_duplicates()
                .sort_values(["data_inventario", "punto_vendita"], ascending=[False, True])
            )

            labels = []
            keys = []

            for _, r in chiavi.iterrows():
                d = str(r["data_inventario"])
                store = str(r["punto_vendita"])
                label = f"{d[5:7]}/{d[:4]} - {d[8:10]} - {store}"
                labels.append(label)
                keys.append((d, store))

            tabs = st.tabs(labels)

            for tab, (data_salvata, store_salvato), label in zip(tabs, keys, labels):
                with tab:
                    df_inv = inventari[
                        (inventari["data_inventario"].astype(str) == data_salvata)
                        & (inventari["punto_vendita"].astype(str) == store_salvato)
                    ].copy()

                    st.markdown(f"### Inventario {label}")

                    food_tab, nofood_tab = st.tabs(["Food", "No Food"])

                    with food_tab:
                        df_food = df_inv[df_inv["tipo"].astype(str).str.lower() == "food"].copy()
                        st.dataframe(df_food, use_container_width=True)

                    with nofood_tab:
                        df_nofood = df_inv[df_inv["tipo"].astype(str).str.lower() == "no food"].copy()
                        st.dataframe(df_nofood, use_container_width=True)

                    conferma = st.checkbox(
                        f"Confermo eliminazione inventario {label}",
                        key=f"conferma_elimina_{data_salvata}_{store_salvato}",
                    )

                    if st.button(
                        f"Elimina inventario {label}",
                        key=f"btn_elimina_{data_salvata}_{store_salvato}",
                    ):
                        if not conferma:
                            st.warning("Spunta la conferma prima.")
                        else:
                            delete_inventory(data_salvata, store_salvato)
                            st.success("Inventario eliminato. Ricarica la pagina.")


# =========================
# TRASFERIMENTI
# =========================

elif menu == "Trasferimenti merci":
    st.subheader("Trasferimenti merci tra punti vendita")

    referenze = fetch_table("referenze")

    if referenze.empty:
        st.info("Prima inserisci o importa le referenze.")
    else:
        attive = referenze[referenze["attivo"].astype(str).str.lower() != "no"].copy()
        attive = attive.sort_values(["tipo", "categoria", "referenza"])

        with st.form("form_trasferimento"):
            data_trasferimento = st.date_input("Data trasferimento", value=date.today())

            col1, col2 = st.columns(2)
            with col1:
                visible_stores = stores_visibili()
                da_options = PUNTI_VENDITA if visible_stores == PUNTI_VENDITA else visible_stores
                da_store = st.selectbox("Da", da_options, index=0)
            with col2:
                a_store_options = [p for p in PUNTI_VENDITA if p != da_store]
                a_store = st.selectbox("A", a_store_options, index=0)

            filtro_tipo = st.selectbox("Tipo", ["Tutti"] + TIPI)
            filtro_categoria = st.selectbox("Categoria", ["Tutte"] + sorted(attive["categoria"].dropna().unique().tolist()))

            rf = attive.copy()

            if filtro_tipo != "Tutti":
                rf = rf[rf["tipo"] == filtro_tipo]

            if filtro_categoria != "Tutte":
                rf = rf[rf["categoria"] == filtro_categoria]

            referenza_sel = st.selectbox("Referenza", rf["referenza"].tolist())
            riga = rf[rf["referenza"] == referenza_sel].iloc[0]

            quantita = st.number_input("Quantità trasferita", min_value=0.0, step=0.1)
            st.text_input("Unità", value=str(riga["unita"]), disabled=True)
            note = st.text_input("Note")

            salva = st.form_submit_button("Registra trasferimento")

            if salva:
                if quantita <= 0:
                    st.error("Inserisci una quantità maggiore di zero.")
                else:
                    insert_rows("trasferimenti", [{
                        "data_trasferimento": data_trasferimento.strftime("%Y-%m-%d"),
                        "da_punto_vendita": da_store,
                        "a_punto_vendita": a_store,
                        "codice": str(riga["codice"]),
                        "referenza": str(riga["referenza"]),
                        "tipo": str(riga["tipo"]),
                        "categoria": str(riga["categoria"]),
                        "unita": str(riga["unita"]),
                        "quantita": quantita,
                        "note": note,
                    }])
                    st.success("Trasferimento registrato.")

        st.divider()
        st.subheader("Storico trasferimenti")

        trasferimenti = fetch_table("trasferimenti")

        if trasferimenti.empty:
            st.info("Nessun trasferimento registrato.")
        else:
            st.dataframe(
                trasferimenti.sort_values(["data_trasferimento", "referenza"], ascending=[False, True]),
                use_container_width=True,
            )


# =========================
# EXPORT
# =========================

elif menu == "Export":
    st.subheader("Export")

    referenze = fetch_table("referenze")
    inventari = fetch_table("inventari")
    trasferimenti = fetch_table("trasferimenti")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.download_button(
            "Scarica referenze CSV",
            referenze.to_csv(index=False).encode("utf-8"),
            file_name="tup_referenze.csv",
            mime="text/csv",
        )

    with col2:
        st.download_button(
            "Scarica inventari CSV",
            inventari.to_csv(index=False).encode("utf-8"),
            file_name="tup_inventari.csv",
            mime="text/csv",
        )

    with col3:
        st.download_button(
            "Scarica trasferimenti CSV",
            trasferimenti.to_csv(index=False).encode("utf-8"),
            file_name="tup_trasferimenti.csv",
            mime="text/csv",
        )
