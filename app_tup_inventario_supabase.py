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

st.set_page_config(page_title="T'up Burger - Inventario Online", layout="wide")

CATEGORIE = [
    "Carne", "Pane", "Formaggi", "Salumi", "Verdure", "Salse", "Fritti",
    "Packaging", "Bevande", "Pulizia", "Altro",
]
UNITA = ["kg", "g", "pz", "conf", "lt", "ml", "CT", "TA", "CF", "NR"]
TIPI = ["Food", "No Food"]
PUNTI_VENDITA = ["De Cosmi", "Via Roma"]

UTENTI = {
    "Admin": {"password": "tupadmin", "store": "Tutti"},
    "De Cosmi": {"password": "decosmi", "store": "De Cosmi"},
    "Via Roma": {"password": "viaroma", "store": "Via Roma"},
}

NUOVO_PRODOTTO = "➕ CREA NUOVO PRODOTTO INTERNO"

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
        # Le nuove tabelle potrebbero non esistere ancora: non blocchiamo l'app.
        if table in ["fornitori_referenze", "acquisti_fatture"]:
            return pd.DataFrame()
        st.error(f"Errore lettura tabella {table}: {e}")
        return pd.DataFrame()


def insert_rows(table, rows):
    if rows:
        sb().table(table).insert(rows).execute()


def update_row(table, row_id, values):
    sb().table(table).update(values).eq("id", row_id).execute()


def delete_rows_by_ids(table, ids):
    if ids:
        sb().table(table).delete().in_("id", ids).execute()


def delete_inventory(data_inventario, punto_vendita):
    sb().table("inventari").delete().eq("data_inventario", data_inventario).eq("punto_vendita", punto_vendita).execute()


def delete_references_by_ids(ids):
    if not ids:
        return
    referenze = fetch_table("referenze")
    codici = referenze[referenze["id"].isin(ids)]["codice"].astype(str).tolist() if not referenze.empty else []
    if codici:
        sb().table("inventari").delete().in_("codice", codici).execute()
        sb().table("trasferimenti").delete().in_("codice", codici).execute()
        try:
            sb().table("fornitori_referenze").delete().in_("codice_interno", codici).execute()
            sb().table("acquisti_fatture").delete().in_("codice_interno", codici).execute()
        except Exception:
            pass
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
    s = str(valore).strip().replace("€", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0


def genera_codice(nome, df):
    base = "".join([c for c in str(nome).upper() if c.isalnum()])[:8] or "REF"
    codice = base
    i = 1
    existing = df["codice"].astype(str).values if not df.empty and "codice" in df.columns else []
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
    return PUNTI_VENDITA if store == "Tutti" else [store]


def ultimo_inventario_per_store(inventari, store):
    if inventari.empty:
        return pd.DataFrame(), ""
    df = inventari[inventari["punto_vendita"].astype(str) == store].copy()
    if df.empty:
        return pd.DataFrame(), ""
    data_ultima = sorted(df["data_inventario"].astype(str).unique().tolist())[-1]
    return df[df["data_inventario"].astype(str) == data_ultima].copy(), data_ultima



def dedup_inventari_latest(df):
    """Tiene una sola riga per data/punto vendita/codice, usando la riga più recente per id/created_at."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    for col in ["data_inventario", "punto_vendita", "codice"]:
        if col not in out.columns:
            return out
    if "id" in out.columns:
        out["_sort_id"] = pd.to_numeric(out["id"], errors="coerce").fillna(0)
    else:
        out["_sort_id"] = 0
    if "created_at" in out.columns:
        out["_sort_created"] = out["created_at"].astype(str)
    else:
        out["_sort_created"] = ""
    out = out.sort_values(["data_inventario", "punto_vendita", "codice", "_sort_created", "_sort_id"])
    out = out.drop_duplicates(subset=["data_inventario", "punto_vendita", "codice"], keep="last")
    return out.drop(columns=["_sort_id", "_sort_created"], errors="ignore")

def label_ref(row):
    return f'{row.get("referenza", "")} | {row.get("codice", "")}'


def trova_prodotto_interno(nome, fornitore, codice_fornitore, referenze, alias):
    """Restituisce codice interno se trova un'associazione già salvata o un nome uguale."""
    cf = str(codice_fornitore or "").strip()
    forn = str(fornitore or "").strip().lower()

    if not alias.empty:
        a = alias.copy()
        for col in ["fornitore", "codice_fornitore", "descrizione_fornitore", "codice_interno"]:
            if col not in a.columns:
                a[col] = ""
        hit = a[(a["fornitore"].astype(str).str.lower() == forn) & (a["codice_fornitore"].astype(str).str.strip() == cf)] if cf else pd.DataFrame()
        if not hit.empty:
            return str(hit.iloc[0]["codice_interno"])
        nm = nome_match(nome)
        a["match_descr"] = a["descrizione_fornitore"].apply(nome_match)
        hit = a[(a["fornitore"].astype(str).str.lower() == forn) & (a["match_descr"] == nm)]
        if not hit.empty:
            return str(hit.iloc[0]["codice_interno"])

    if not referenze.empty and "referenza" in referenze.columns:
        r = referenze.copy()
        r["match_nome"] = r["referenza"].apply(nome_match)
        hit = r[r["match_nome"] == nome_match(nome)]
        if not hit.empty:
            return str(hit.iloc[0]["codice"])
    return ""


def upsert_alias(codice_interno, fornitore, codice_fornitore, descrizione, unita, prezzo):
    alias = fetch_table("fornitori_referenze")
    fornitore = str(fornitore or "").strip()
    codice_fornitore = str(codice_fornitore or "").strip()
    descrizione = str(descrizione or "").strip()
    valori = {
        "codice_interno": codice_interno,
        "fornitore": fornitore,
        "codice_fornitore": codice_fornitore,
        "descrizione_fornitore": descrizione,
        "unita": unita,
        "ultimo_prezzo": pulisci_numero(prezzo),
        "ultima_data_acquisto": date.today().strftime("%Y-%m-%d"),
    }
    if alias.empty:
        insert_rows("fornitori_referenze", [valori])
        return
    for col in valori.keys():
        if col not in alias.columns:
            alias[col] = ""
    hit = pd.DataFrame()
    if codice_fornitore:
        hit = alias[(alias["fornitore"].astype(str) == fornitore) & (alias["codice_fornitore"].astype(str) == codice_fornitore)]
    if hit.empty:
        hit = alias[(alias["fornitore"].astype(str) == fornitore) & (alias["descrizione_fornitore"].apply(nome_match) == nome_match(descrizione))]
    if hit.empty:
        insert_rows("fornitori_referenze", [valori])
    else:
        update_row("fornitori_referenze", hit.iloc[0]["id"], valori)


def registra_acquisto(data_acquisto, fornitore, codice_interno, referenza_interna, descrizione_fornitore, codice_fornitore, quantita, unita, prezzo, importo, iva, nome_file=""):
    insert_rows("acquisti_fatture", [{
        "data_acquisto": str(data_acquisto),
        "mese": pd.to_datetime(str(data_acquisto)).month,
        "anno": pd.to_datetime(str(data_acquisto)).year,
        "fornitore": str(fornitore),
        "codice_interno": str(codice_interno),
        "referenza_interna": str(referenza_interna),
        "descrizione_fornitore": str(descrizione_fornitore),
        "codice_fornitore": str(codice_fornitore),
        "quantita": pulisci_numero(quantita),
        "unita": str(unita),
        "prezzo_unitario": pulisci_numero(prezzo),
        "importo": pulisci_numero(importo),
        "iva": str(iva),
        "nome_file": str(nome_file),
    }])


def aggiorna_prezzo_medio_mese(codice_interno, mese, anno):
    acquisti = fetch_table("acquisti_fatture")
    referenze = fetch_table("referenze")
    if acquisti.empty or referenze.empty:
        return
    df = acquisti[
        (acquisti["codice_interno"].astype(str) == str(codice_interno))
        & (pd.to_numeric(acquisti["mese"], errors="coerce") == int(mese))
        & (pd.to_numeric(acquisti["anno"], errors="coerce") == int(anno))
    ].copy()
    if df.empty:
        return
    df["quantita"] = pd.to_numeric(df["quantita"], errors="coerce").fillna(0)
    df["importo"] = pd.to_numeric(df["importo"], errors="coerce").fillna(0)
    qta = df["quantita"].sum()
    imp = df["importo"].sum()
    if qta <= 0 or imp <= 0:
        return
    prezzo_medio = imp / qta
    hit = referenze[referenze["codice"].astype(str) == str(codice_interno)]
    if not hit.empty:
        update_row("referenze", hit.iloc[0]["id"], {"prezzo_unitario": prezzo_medio})



def aggiorna_prezzi_medi_per_codice(codice_interno):
    acquisti = fetch_table("acquisti_fatture")
    if acquisti.empty:
        return
    df = acquisti[acquisti["codice_interno"].astype(str) == str(codice_interno)].copy()
    if df.empty:
        return
    for _, r in df[["mese", "anno"]].drop_duplicates().iterrows():
        try:
            aggiorna_prezzo_medio_mese(codice_interno, int(r["mese"]), int(r["anno"]))
        except Exception:
            pass


def aggiorna_alias_e_acquisti(alias_id, nuovo_codice, nuova_referenza):
    """Collega un alias fornitore a un prodotto interno e riallinea gli acquisti già registrati."""
    alias = fetch_table("fornitori_referenze")
    if alias.empty:
        return 0
    hit = alias[alias["id"].astype(str) == str(alias_id)]
    if hit.empty:
        return 0
    r = hit.iloc[0]
    fornitore = str(r.get("fornitore", ""))
    codice_fornitore = str(r.get("codice_fornitore", ""))
    descrizione = str(r.get("descrizione_fornitore", ""))
    update_row("fornitori_referenze", alias_id, {"codice_interno": str(nuovo_codice)})

    q = sb().table("acquisti_fatture").update({
        "codice_interno": str(nuovo_codice),
        "referenza_interna": str(nuova_referenza),
    }).eq("fornitore", fornitore)
    if codice_fornitore:
        q = q.eq("codice_fornitore", codice_fornitore)
    else:
        q = q.eq("descrizione_fornitore", descrizione)
    q.execute()
    aggiorna_prezzi_medi_per_codice(nuovo_codice)
    return 1


def accorpa_prodotti(codice_da, codice_a):
    """Sposta inventari, trasferimenti, acquisti e alias da un prodotto duplicato a quello principale."""
    codice_da = str(codice_da)
    codice_a = str(codice_a)
    if not codice_da or not codice_a or codice_da == codice_a:
        raise ValueError("Scegli due prodotti diversi.")

    referenze = fetch_table("referenze")
    if referenze.empty:
        raise ValueError("Nessuna referenza trovata.")
    src = referenze[referenze["codice"].astype(str) == codice_da]
    dst = referenze[referenze["codice"].astype(str) == codice_a]
    if src.empty or dst.empty:
        raise ValueError("Prodotto di origine o destinazione non trovato.")
    src = src.iloc[0]
    dst = dst.iloc[0]
    nuova_referenza = str(dst.get("referenza", ""))
    nuovo_tipo = str(dst.get("tipo", "Food"))
    nuova_categoria = str(dst.get("categoria", "Altro"))
    nuova_unita = str(dst.get("unita", "pz"))

    # Creo un alias anche per la vecchia referenza, così resta memoria del collegamento.
    try:
        upsert_alias(
            codice_a,
            src.get("fornitore", ""),
            src.get("codice_fornitore", ""),
            src.get("referenza", ""),
            src.get("unita", nuova_unita),
            src.get("prezzo_unitario", 0),
        )
    except Exception:
        pass

    # Riallineo tutti i movimenti storici.
    sb().table("inventari").update({
        "codice": codice_a, "referenza": nuova_referenza,
        "tipo": nuovo_tipo, "categoria": nuova_categoria, "unita": nuova_unita,
    }).eq("codice", codice_da).execute()

    sb().table("trasferimenti").update({
        "codice": codice_a, "referenza": nuova_referenza,
        "tipo": nuovo_tipo, "categoria": nuova_categoria, "unita": nuova_unita,
    }).eq("codice", codice_da).execute()

    try:
        sb().table("acquisti_fatture").update({
            "codice_interno": codice_a,
            "referenza_interna": nuova_referenza,
        }).eq("codice_interno", codice_da).execute()
    except Exception:
        pass

    try:
        sb().table("fornitori_referenze").update({"codice_interno": codice_a}).eq("codice_interno", codice_da).execute()
    except Exception:
        pass

    # Non cancelliamo: lo disattiviamo, così non lo vedi in inventario ma resta audit.
    update_row("referenze", src["id"], {"attivo": "no", "fornitore": "ACCORPATO", "codice_fornitore": codice_a})
    aggiorna_prezzi_medi_per_codice(codice_a)
    return nuova_referenza

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
                for table in page.extract_tables():
                    for row in table:
                        celle = [str(c).strip() for c in row if c is not None and str(c).strip()]
                        if celle:
                            testo += " | ".join(celle) + "\n"
            except Exception:
                pass
    return testo


def estrai_fornitore(testo, nome_file):
    righe = [r.strip() for r in testo.splitlines() if r.strip()]
    parole_da_evitare = ["fattura", "documento", "partita iva", "codice fiscale", "cliente", "destinatario", "spett.le", "totale", "iban", "pagamento", "t'up", "tup"]
    for i, r in enumerate(righe):
        if r.strip().lower() == "fornitore" and i + 1 < len(righe):
            return righe[i + 1].strip()[:100]
    for r in righe[:80]:
        rl = r.lower()
        if len(r) < 3 or any(p in rl for p in parole_da_evitare):
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
    def strip_ns(tag): return tag.split("}", 1)[-1] if "}" in tag else tag
    def all_by_name(node, name): return [el for el in node.iter() if strip_ns(el.tag) == name]
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
    for det in all_by_name(root, "DettaglioLinee"):
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
        if any(x in descrizione.lower() for x in ["ordine cliente", "preventivo", "spese di trasporto", "trasporto"]):
            continue
        risultati.append({
            "importa": True, "referenza": descrizione[:120], "tipo": tipo_default_da_fornitore(fornitore),
            "categoria": "Altro", "unita": normalizza_unita(unita), "fornitore": fornitore,
            "codice_fornitore": codice_fornitore, "prezzo_unitario": prezzo,
            "ultima_quantita": quantita, "ultimo_importo": importo,
            "iva": str(iva).replace(".", ","), "attivo": "si", "nome_file": nome_file,
        })
    return risultati


def estrai_referenze_pdf(testo, fornitore, nome_file=""):
    righe_raw = [r.strip() for r in testo.splitlines() if r and r.strip()]
    risultati = []
    righe = []
    dentro_prodotti = False
    stop_section = ["metodo di pagamento", "riepilogo iva", "calcolo fattura", "regime fiscale", "dati aggiuntivi", "documenti correlati", "allegati", "causale documento", "dati trasporto", "totale documento", "netto a pagare"]
    for r in righe_raw:
        rl = r.lower()
        if "prodotti e servizi" in rl or "dettaglio linee" in rl:
            dentro_prodotti = True
            continue
        if dentro_prodotti and any(x in rl for x in stop_section):
            break
        if dentro_prodotti and not rl.startswith("nr descrizione"):
            righe.append(r)
    if not righe:
        righe = righe_raw
    product_re = re.compile(r"^(\d{1,6})\s+(.+?)\s+(\d+(?:[.,]\d+)?)\s+([A-Za-zÀ-ÿ.]+)\s+(\d+(?:[.,]\d{1,6})?)\s*€\s+(?:-|\d+(?:[.,]\d{1,2})?\s*%)?\s*(\d+(?:[.,]\d{1,6})?)\s*€\s+(\d+(?:[.,]\d{1,2})?)\s*%")
    codice_re = re.compile(r"Cod\.valore:\s*([A-Za-z0-9_-]+)")
    corrente = None
    for r in righe:
        r = re.sub(r"\s+", " ", r).strip()
        rl = r.lower()
        if not r or rl.startswith("nr descrizione") or rl.startswith("copia analogica") or rl.startswith("fattura nr"):
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
            if any(x in descr_low for x in ["ordine cliente", "preventivo", "spese di trasporto", "trasporto", "spesa accessoria"]):
                corrente = None
                continue
            prezzo = pulisci_numero(m.group(5)); importo = pulisci_numero(m.group(6))
            if prezzo <= 0 and importo <= 0 and not any(x in descr_low for x in ["dispenser", "campione", "omaggio", "kit"]):
                corrente = None
                continue
            if len(re.findall(r"[a-zA-ZàèéìòùÀÈÉÌÒÙ]", descrizione)) < 3:
                corrente = None
                continue
            corrente = {
                "importa": True, "referenza": descrizione, "tipo": tipo_default_da_fornitore(fornitore),
                "categoria": "Altro", "unita": normalizza_unita(m.group(4)), "fornitore": fornitore,
                "codice_fornitore": "", "prezzo_unitario": prezzo, "ultima_quantita": pulisci_numero(m.group(3)),
                "ultimo_importo": importo, "iva": str(m.group(7)).replace(".", ","), "attivo": "si", "nome_file": nome_file,
            }
            continue
        if corrente is not None:
            if any(x in rl for x in ["tipo dato", "riferimento testo", "riferimento numero", "cod.tipo", "cod.valore", "metodo di pagamento", "regime fiscale"]):
                continue
            if len(re.findall(r"[a-zA-ZàèéìòùÀÈÉÌÒÙ]", r)) >= 2 and not re.match(r"^\d+\s+", r):
                corrente["referenza"] = (corrente["referenza"] + " " + r)[:120]
    if corrente is not None:
        risultati.append(corrente)
    puliti, visti = [], set()
    for item in risultati:
        key = (nome_match(item.get("referenza", "")), str(item.get("codice_fornitore", "")), str(item.get("fornitore", "")))
        if key not in visti:
            visti.add(key); puliti.append(item)
    return puliti

# =========================
# LOGIN
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
    menu = st.radio("Menu", ["Dashboard", "Import fatture", "Anagrafica referenze", "Inventario mensile", "Trasferimenti merci", "Acquisti e prezzi medi", "Export"])

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
        for col in ["prezzo_unitario", "scorta_minima"]:
            if col not in ref.columns:
                ref[col] = 0.0
            ref[col] = pd.to_numeric(ref[col], errors="coerce").fillna(0)
        totale_generale = 0
        for store in stores_visibili():
            st.markdown(f"### {store}")
            ultimo, data_ultima = ultimo_inventario_per_store(inventari, store)
            if ultimo.empty:
                st.info(f"Nessun inventario salvato per {store}.")
                continue
            ultimo["quantita"] = pd.to_numeric(ultimo["quantita"], errors="coerce").fillna(0)
            valorizzato = ultimo.merge(ref[["codice", "prezzo_unitario", "scorta_minima"]], on="codice", how="left")
            valorizzato["prezzo_unitario"] = pd.to_numeric(valorizzato["prezzo_unitario"], errors="coerce").fillna(0)
            valorizzato["scorta_minima"] = pd.to_numeric(valorizzato["scorta_minima"], errors="coerce").fillna(0)
            valorizzato["valore"] = valorizzato["quantita"] * valorizzato["prezzo_unitario"]
            valore_store = valorizzato["valore"].sum(); totale_generale += valore_store
            food_valore = valorizzato[valorizzato["tipo"].astype(str).str.lower() == "food"]["valore"].sum()
            nofood_valore = valorizzato[valorizzato["tipo"].astype(str).str.lower() == "no food"]["valore"].sum()
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Ultimo inventario", data_ultima)
            col2.metric("Valore totale", euro(valore_store))
            col3.metric("Food", euro(food_valore))
            col4.metric("No Food", euro(nofood_valore))
            sotto_scorta = valorizzato[(valorizzato["scorta_minima"] > 0) & (valorizzato["quantita"] <= valorizzato["scorta_minima"])].copy()
            if not sotto_scorta.empty:
                st.warning(f"Referenze sotto scorta: {len(sotto_scorta)}")
                st.dataframe(sotto_scorta[["referenza", "tipo", "categoria", "unita", "quantita", "scorta_minima", "prezzo_unitario", "valore"]], use_container_width=True)
            with st.expander(f"Dettaglio valorizzato {store}"):
                st.dataframe(valorizzato[["referenza", "tipo", "categoria", "unita", "quantita", "prezzo_unitario", "valore", "note"]].sort_values(["tipo", "categoria", "referenza"]), use_container_width=True)
        st.divider(); st.metric("Valore magazzino totale visibile", euro(totale_generale))
        if not trasferimenti.empty:
            st.subheader("Ultimi trasferimenti")
            view_tr = trasferimenti.copy()
            visibili = stores_visibili()
            if visibili != PUNTI_VENDITA:
                store = visibili[0]
                view_tr = view_tr[(view_tr["da_punto_vendita"] == store) | (view_tr["a_punto_vendita"] == store)]
            st.dataframe(view_tr.sort_values("data_trasferimento", ascending=False).head(20), use_container_width=True) if not view_tr.empty else st.info("Nessun trasferimento per questo punto vendita.")

# =========================
# IMPORT FATTURE
# =========================

elif menu == "Import fatture":
    st.subheader("Import fatture con accorpamento prodotti")
    st.info("Le righe fattura vengono collegate a un prodotto interno unico. Lo stesso prodotto può avere più fornitori e più codici fornitore.")
    data_fattura = st.date_input("Data fattura / data acquisto", value=date.today())
    files = st.file_uploader("Carica fatture PDF o XML", type=["pdf", "xml"], accept_multiple_files=True)
    if files:
        righe = []
        for f in files:
            try:
                file_bytes = f.read(); nome_file = f.name.lower()
                if nome_file.endswith(".xml"):
                    righe_file = estrai_referenze_xml(file_bytes, f.name)
                else:
                    testo = estrai_testo_pdf(file_bytes)
                    fornitore = estrai_fornitore(testo, f.name)
                    righe_file = estrai_referenze_pdf(testo, fornitore, f.name)
                righe.extend(righe_file)
                if not righe_file:
                    st.warning(f"Nessuna referenza trovata in {f.name}")
            except Exception as e:
                st.warning(f"Errore lettura {f.name}: {e}")
        if not righe:
            st.warning("Non ho trovato referenze leggibili.")
        else:
            referenze = fetch_table("referenze")
            alias = fetch_table("fornitori_referenze")
            df = pd.DataFrame(righe)
            df["match"] = df["referenza"].apply(nome_match) + "|" + df["fornitore"].astype(str).str.lower() + "|" + df["codice_fornitore"].astype(str)
            df = df.drop_duplicates("match").drop(columns=["match"])
            if referenze.empty:
                ref_labels = []
                label_to_codice = {}
            else:
                ref_labels = [label_ref(r) for _, r in referenze.sort_values("referenza").iterrows()]
                label_to_codice = {label_ref(r): str(r.get("codice", "")) for _, r in referenze.iterrows()}
                codice_to_label = {str(r.get("codice", "")): label_ref(r) for _, r in referenze.iterrows()}
                df["prodotto_interno"] = df.apply(lambda r: codice_to_label.get(trova_prodotto_interno(r["referenza"], r["fornitore"], r.get("codice_fornitore", ""), referenze, alias), NUOVO_PRODOTTO), axis=1)
            if "prodotto_interno" not in df.columns:
                df["prodotto_interno"] = NUOVO_PRODOTTO
            if "scorta_minima" not in df.columns:
                df["scorta_minima"] = 0.0
            colonne = ["importa", "prodotto_interno", "referenza", "tipo", "categoria", "unita", "fornitore", "codice_fornitore", "prezzo_unitario", "ultima_quantita", "ultimo_importo", "iva", "scorta_minima", "attivo", "nome_file"]
            df = df[[c for c in colonne if c in df.columns]]
            st.success(f"Righe prodotto trovate: {len(df)}")
            df_edit = st.data_editor(
                df, use_container_width=True, num_rows="dynamic",
                column_config={
                    "importa": st.column_config.CheckboxColumn("importa"),
                    "prodotto_interno": st.column_config.SelectboxColumn("prodotto interno", options=[NUOVO_PRODOTTO] + ref_labels),
                    "tipo": st.column_config.SelectboxColumn("tipo", options=TIPI),
                    "categoria": st.column_config.SelectboxColumn("categoria", options=CATEGORIE),
                    "unita": st.column_config.SelectboxColumn("unita", options=UNITA),
                }, key="fatture_accorpate_editor")
            if st.button("Salva fatture e aggiorna prezzi medi"):
                referenze = fetch_table("referenze")
                df_da_salvare = df_edit[df_edit["importa"] == True].copy() if "importa" in df_edit.columns else df_edit.copy()
                inserite = aggiornate = acquisti = saltate = 0
                errori = []
                if referenze.empty:
                    referenze = pd.DataFrame(columns=["id", "codice", "referenza"])
                for _, row in df_da_salvare.iterrows():
                    try:
                        nome_fattura = pulisci_testo(row.get("referenza", ""))
                        if not nome_fattura:
                            saltate += 1; continue
                        scelta = str(row.get("prodotto_interno", NUOVO_PRODOTTO))
                        if scelta == NUOVO_PRODOTTO:
                            codice_interno = genera_codice(nome_fattura, referenze)
                            referenza_interna = nome_fattura
                            valori_ref = {
                                "codice": codice_interno, "referenza": referenza_interna,
                                "tipo": row.get("tipo", "Food"), "categoria": row.get("categoria", "Altro"),
                                "unita": row.get("unita", "pz"),
                                "fornitore": "MULTI" if row.get("fornitore", "") else "",
                                "codice_fornitore": "", "prezzo_unitario": pulisci_numero(row.get("prezzo_unitario", 0)),
                                "ultima_quantita": pulisci_numero(row.get("ultima_quantita", 0)),
                                "ultimo_importo": pulisci_numero(row.get("ultimo_importo", 0)),
                                "iva": str(row.get("iva", "")).strip(), "scorta_minima": pulisci_numero(row.get("scorta_minima", 0)),
                                "attivo": "si",
                            }
                            insert_rows("referenze", [valori_ref]); inserite += 1
                            nuova_locale = valori_ref.copy(); nuova_locale["id"] = None
                            referenze = pd.concat([referenze, pd.DataFrame([nuova_locale])], ignore_index=True)
                        else:
                            codice_interno = label_to_codice.get(scelta, "")
                            hit = referenze[referenze["codice"].astype(str) == codice_interno]
                            if hit.empty:
                                saltate += 1; continue
                            referenza_interna = str(hit.iloc[0]["referenza"])
                            # aggiorno solo classificazione e ultimi dati, non cambio il nome interno
                            update_row("referenze", hit.iloc[0]["id"], {
                                "tipo": row.get("tipo", hit.iloc[0].get("tipo", "Food")),
                                "categoria": row.get("categoria", hit.iloc[0].get("categoria", "Altro")),
                                "unita": row.get("unita", hit.iloc[0].get("unita", "pz")),
                                "ultima_quantita": pulisci_numero(row.get("ultima_quantita", 0)),
                                "ultimo_importo": pulisci_numero(row.get("ultimo_importo", 0)),
                                "iva": str(row.get("iva", "")).strip(),
                                "attivo": str(row.get("attivo", "si")),
                            }); aggiornate += 1
                        upsert_alias(codice_interno, row.get("fornitore", ""), row.get("codice_fornitore", ""), nome_fattura, row.get("unita", "pz"), row.get("prezzo_unitario", 0))
                        registra_acquisto(data_fattura, row.get("fornitore", ""), codice_interno, referenza_interna, nome_fattura, row.get("codice_fornitore", ""), row.get("ultima_quantita", 0), row.get("unita", "pz"), row.get("prezzo_unitario", 0), row.get("ultimo_importo", 0), row.get("iva", ""), row.get("nome_file", ""))
                        acquisti += 1
                        aggiorna_prezzo_medio_mese(codice_interno, data_fattura.month, data_fattura.year)
                    except Exception as e:
                        errori.append(f"{row.get('referenza', '')}: {e}")
                if errori:
                    st.warning(f"Import parziale. Nuovi prodotti: {inserite}. Prodotti aggiornati: {aggiornate}. Acquisti registrati: {acquisti}. Saltate: {saltate}. Errori: {len(errori)}")
                    with st.expander("Dettaglio errori"):
                        for err in errori: st.write(err)
                else:
                    st.success(f"Salvato. Nuovi prodotti: {inserite}. Prodotti aggiornati: {aggiornate}. Acquisti registrati: {acquisti}. Saltate: {saltate}.")

# =========================
# ANAGRAFICA
# =========================

elif menu == "Anagrafica referenze":
    st.subheader("Anagrafica prodotti interni")
    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "Aggiungi prodotto", "Tabella prodotti", "Alias fornitori",
        "Associa manualmente", "Accorpa prodotti", "Elimina refusi"
    ])

    with tab1:
        with st.form("form_referenza"):
            referenza = st.text_input("Prodotto interno")
            tipo = st.selectbox("Tipo", TIPI)
            categoria = st.selectbox("Categoria", CATEGORIE)
            unita = st.selectbox("Unità", UNITA)
            prezzo = st.number_input("Prezzo unitario iniziale", min_value=0.0, step=0.01)
            scorta_minima = st.number_input("Scorta minima", min_value=0.0, step=0.1)
            salva = st.form_submit_button("Aggiungi prodotto")
            if salva:
                referenze = fetch_table("referenze"); nome = pulisci_testo(referenza)
                if not nome:
                    st.error("Inserisci un prodotto.")
                else:
                    codice = genera_codice(nome, referenze)
                    insert_rows("referenze", [{"codice": codice, "referenza": nome, "tipo": tipo, "categoria": categoria, "unita": unita, "fornitore": "MULTI", "codice_fornitore": "", "prezzo_unitario": prezzo, "ultima_quantita": 0.0, "ultimo_importo": 0.0, "iva": "", "scorta_minima": scorta_minima, "attivo": "si"}])
                    st.success("Prodotto interno aggiunto.")
                    st.rerun()

    with tab2:
        referenze = fetch_table("referenze")
        if referenze.empty:
            st.info("Nessun prodotto presente.")
        else:
            filtro = st.text_input("Cerca", key="cerca_ref")
            view = referenze.copy()
            if filtro:
                view = view[view.astype(str).apply(lambda row: row.str.lower().str.contains(filtro.lower()).any(), axis=1)]
            edited = st.data_editor(view, use_container_width=True, num_rows="dynamic", column_config={"tipo": st.column_config.SelectboxColumn("tipo", options=TIPI), "categoria": st.column_config.SelectboxColumn("categoria", options=CATEGORIE), "unita": st.column_config.SelectboxColumn("unita", options=UNITA)}, disabled=["id", "created_at"], key="referenze_editor")
            if st.button("Salva anagrafica"):
                for _, row in edited.iterrows():
                    if "id" not in row or pd.isna(row["id"]):
                        continue
                    update_row("referenze", row["id"], {"codice": str(row.get("codice", "")), "referenza": str(row.get("referenza", "")), "tipo": str(row.get("tipo", "Food")), "categoria": str(row.get("categoria", "Altro")), "unita": str(row.get("unita", "pz")), "fornitore": str(row.get("fornitore", "MULTI")), "codice_fornitore": str(row.get("codice_fornitore", "")), "prezzo_unitario": pulisci_numero(row.get("prezzo_unitario", 0)), "ultima_quantita": pulisci_numero(row.get("ultima_quantita", 0)), "ultimo_importo": pulisci_numero(row.get("ultimo_importo", 0)), "iva": str(row.get("iva", "")), "scorta_minima": pulisci_numero(row.get("scorta_minima", 0)), "attivo": str(row.get("attivo", "si"))})
                st.success("Anagrafica salvata.")

    with tab3:
        alias = fetch_table("fornitori_referenze")
        st.caption("Qui vedi come le descrizioni/codici dei fornitori vengono collegati ai prodotti interni.")
        if alias.empty:
            st.info("Nessun alias fornitore ancora registrato.")
        else:
            st.dataframe(alias.sort_values(["fornitore", "descrizione_fornitore"]), use_container_width=True)

    with tab4:
        st.markdown("### Associa manualmente alias fornitore → prodotto interno")
        st.info("Usa questa scheda quando una riga fattura è finita sul prodotto sbagliato. Cambi il prodotto interno e il sistema riallinea anche gli acquisti già registrati per quel codice fornitore.")
        referenze = fetch_table("referenze")
        alias = fetch_table("fornitori_referenze")
        if referenze.empty or alias.empty:
            st.info("Servono prodotti interni e almeno un alias fornitore registrato.")
        else:
            ref_attive = referenze[referenze["attivo"].astype(str).str.lower() != "no"].copy() if "attivo" in referenze.columns else referenze.copy()
            ref_attive = ref_attive.sort_values("referenza")
            label_to_codice = {label_ref(r): str(r.get("codice", "")) for _, r in ref_attive.iterrows()}
            codice_to_label = {str(r.get("codice", "")): label_ref(r) for _, r in ref_attive.iterrows()}
            codice_to_nome = {str(r.get("codice", "")): str(r.get("referenza", "")) for _, r in ref_attive.iterrows()}
            options = list(label_to_codice.keys())

            cerca_alias = st.text_input("Cerca alias / fornitore", key="cerca_alias_associa")
            work = alias.copy()
            for col in ["id", "codice_interno", "fornitore", "codice_fornitore", "descrizione_fornitore", "unita", "ultimo_prezzo"]:
                if col not in work.columns:
                    work[col] = ""
            work["prodotto_interno"] = work["codice_interno"].astype(str).map(codice_to_label).fillna("")
            if cerca_alias:
                work = work[work.astype(str).apply(lambda row: row.str.lower().str.contains(cerca_alias.lower()).any(), axis=1)]
            work = work[["id", "fornitore", "codice_fornitore", "descrizione_fornitore", "unita", "ultimo_prezzo", "prodotto_interno"]].sort_values(["fornitore", "descrizione_fornitore"])
            edited_alias = st.data_editor(
                work,
                use_container_width=True,
                hide_index=True,
                disabled=["id", "fornitore", "codice_fornitore", "descrizione_fornitore", "unita", "ultimo_prezzo"],
                column_config={"prodotto_interno": st.column_config.SelectboxColumn("prodotto interno corretto", options=options)},
                key="associa_alias_editor",
            )
            if st.button("Salva associazioni manuali"):
                modifiche = 0
                errori = []
                originale = work.set_index("id")["prodotto_interno"].to_dict()
                for _, row in edited_alias.iterrows():
                    try:
                        aid = row.get("id")
                        nuovo_label = str(row.get("prodotto_interno", ""))
                        if not aid or not nuovo_label or nuovo_label == str(originale.get(aid, "")):
                            continue
                        nuovo_codice = label_to_codice.get(nuovo_label, "")
                        nuova_ref = codice_to_nome.get(nuovo_codice, "")
                        if nuovo_codice:
                            aggiorna_alias_e_acquisti(aid, nuovo_codice, nuova_ref)
                            modifiche += 1
                    except Exception as e:
                        errori.append(str(e))
                if errori:
                    st.warning("Alcune associazioni non sono state salvate: " + " | ".join(errori[:5]))
                st.success(f"Associazioni aggiornate: {modifiche}")
                if modifiche:
                    st.rerun()

    with tab5:
        st.markdown("### Accorpa prodotti duplicati")
        st.warning("Questa funzione sposta inventari, trasferimenti, acquisti e alias dal prodotto duplicato al prodotto principale. Il duplicato viene disattivato, non cancellato.")
        referenze = fetch_table("referenze")
        if referenze.empty or len(referenze) < 2:
            st.info("Servono almeno due prodotti per fare un accorpamento.")
        else:
            ref_sorted = referenze.sort_values("referenza").copy()
            labels_all = [label_ref(r) for _, r in ref_sorted.iterrows()]
            label_to_codice_all = {label_ref(r): str(r.get("codice", "")) for _, r in ref_sorted.iterrows()}
            col_a, col_b = st.columns(2)
            with col_a:
                da_label = st.selectbox("Prodotto duplicato da accorpare", labels_all, key="merge_da")
            with col_b:
                a_label = st.selectbox("Prodotto principale da mantenere", labels_all, key="merge_a")
            conferma_merge = st.checkbox("Confermo che voglio accorpare questi due prodotti", key="conferma_merge")
            if st.button("Accorpa prodotti"):
                codice_da = label_to_codice_all.get(da_label, "")
                codice_a = label_to_codice_all.get(a_label, "")
                if codice_da == codice_a:
                    st.error("Hai selezionato lo stesso prodotto due volte.")
                elif not conferma_merge:
                    st.error("Devi confermare l'accorpamento.")
                else:
                    try:
                        nome_finale = accorpa_prodotti(codice_da, codice_a)
                        st.success(f"Accorpamento completato. Tutto è stato spostato su: {nome_finale}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Errore accorpamento: {e}")

    with tab6:
        st.warning("Elimina solo refusi. La cancellazione rimuove anche inventari, trasferimenti, alias e acquisti collegati.")
        referenze = fetch_table("referenze")
        if referenze.empty:
            st.info("Nessun prodotto da eliminare.")
        else:
            cerca = st.text_input("Cerca prodotto da eliminare")
            view = referenze.copy()
            if cerca:
                view = view[view.astype(str).apply(lambda row: row.str.lower().str.contains(cerca.lower()).any(), axis=1)]
            view.insert(0, "elimina", False)
            edited_del = st.data_editor(view, use_container_width=True, hide_index=True, disabled=[c for c in view.columns if c != "elimina"], column_config={"elimina": st.column_config.CheckboxColumn("elimina")}, key="elimina_refusi_editor")
            ids = edited_del[edited_del["elimina"] == True]["id"].tolist()
            conferma = st.checkbox("Confermo eliminazione definitiva")
            if st.button("Elimina prodotti selezionati"):
                if not ids: st.warning("Non hai selezionato nessun prodotto.")
                elif not conferma: st.warning("Devi confermare.")
                else:
                    delete_references_by_ids(ids); st.success("Prodotti eliminati.")

# =========================
# INVENTARIO MENSILE
# =========================

elif menu == "Inventario mensile":
    st.subheader("Inventario mensile")
    referenze = fetch_table("referenze"); inventari = dedup_inventari_latest(fetch_table("inventari"))
    if referenze.empty:
        st.info("Prima inserisci o importa i prodotti.")
    else:
        data_inventario = st.date_input("Data inventario", value=date.today())
        data_str = data_inventario.strftime("%Y-%m-%d"); mese = data_inventario.month; anno = data_inventario.year
        st.caption(f"Inventario del mese: {mese:02d}/{anno}")
        st.info("Quando salvi, l’inventario di quel giorno e punto vendita viene sostituito, non duplicato.")
        attive = referenze[referenze["attivo"].astype(str).str.lower() != "no"].copy().sort_values(["tipo", "categoria", "referenza"])
        attive = attive.drop_duplicates(subset=["codice"], keep="last")
        store_tabs = st.tabs(stores_visibili())
        edited_tables = []
        for tab, store in zip(store_tabs, stores_visibili()):
            with tab:
                st.markdown(f"### {store}")
                base = attive[["codice", "referenza", "tipo", "categoria", "unita"]].copy()
                base.insert(0, "punto_vendita", store); base["quantita"] = 0.0; base["note"] = ""
                esistente = inventari[(inventari["data_inventario"].astype(str) == data_str) & (inventari["punto_vendita"].astype(str) == store)].copy() if not inventari.empty else pd.DataFrame()
                if not esistente.empty:
                    base = base.drop(columns=["quantita", "note"]).merge(esistente[["codice", "quantita", "note"]], on="codice", how="left")
                    base["quantita"] = pd.to_numeric(base["quantita"], errors="coerce").fillna(0); base["note"] = base["note"].fillna("")
                filtro_tipo = st.selectbox("Tipo", ["Tutti"] + TIPI, key=f"tipo_inv_{store}")
                filtro_categoria = st.multiselect("Categoria", sorted(attive["categoria"].dropna().unique().tolist()), key=f"cat_inv_{store}")
                tabella = base.copy()
                if filtro_tipo != "Tutti": tabella = tabella[tabella["tipo"] == filtro_tipo]
                if filtro_categoria: tabella = tabella[tabella["categoria"].isin(filtro_categoria)]
                edited = st.data_editor(tabella, use_container_width=True, hide_index=True, disabled=["punto_vendita", "codice", "referenza", "tipo", "categoria", "unita"], column_config={"quantita": st.column_config.NumberColumn("Quantità", min_value=0.0, step=0.1)}, key=f"inventario_{data_str}_{store}")
                edited_tables.append(edited)
        if st.button("Salva inventario mensile"):
            edited_all = pd.concat(edited_tables, ignore_index=True) if edited_tables else pd.DataFrame()
            rows = []
            for _, row in edited_all.iterrows():
                rows.append({"data_inventario": data_str, "mese": mese, "anno": anno, "punto_vendita": str(row["punto_vendita"]), "codice": str(row["codice"]), "referenza": str(row["referenza"]), "tipo": str(row["tipo"]), "categoria": str(row["categoria"]), "unita": str(row["unita"]), "quantita": pulisci_numero(row.get("quantita", 0)), "note": str(row.get("note", ""))})
            if rows:
                for store in stores_visibili():
                    delete_inventory(data_str, store)
                insert_rows("inventari", rows)
                st.success("Inventario mensile salvato.")
        st.divider(); st.subheader("Inventari salvati")
        inventari = dedup_inventari_latest(fetch_table("inventari"))
        if inventari.empty:
            st.info("Nessun inventario salvato.")
        else:
            chiavi = inventari[["data_inventario", "punto_vendita"]].drop_duplicates().sort_values(["data_inventario", "punto_vendita"], ascending=[False, True])
            for _, k in chiavi.iterrows():
                with st.expander(f"{k['data_inventario']} - {k['punto_vendita']}"):
                    df_inv = inventari[(inventari["data_inventario"].astype(str) == str(k["data_inventario"])) & (inventari["punto_vendita"].astype(str) == str(k["punto_vendita"]))]
                    st.dataframe(df_inv.sort_values(["tipo", "categoria", "referenza"]), use_container_width=True)

# =========================
# TRASFERIMENTI
# =========================

elif menu == "Trasferimenti merci":
    st.subheader("Trasferimenti merci tra punti vendita")
    referenze = fetch_table("referenze")
    if referenze.empty:
        st.info("Prima inserisci o importa i prodotti.")
    else:
        attive = referenze[referenze["attivo"].astype(str).str.lower() != "no"].copy().sort_values(["tipo", "categoria", "referenza"])
        attive = attive.drop_duplicates(subset=["codice"], keep="last")
        with st.form("form_trasferimento"):
            data_trasferimento = st.date_input("Data trasferimento", value=date.today())
            col1, col2 = st.columns(2)
            with col1:
                da_options = PUNTI_VENDITA if stores_visibili() == PUNTI_VENDITA else stores_visibili()
                da_store = st.selectbox("Da", da_options, index=0)
            with col2:
                a_store = st.selectbox("A", [p for p in PUNTI_VENDITA if p != da_store], index=0)
            filtro_tipo = st.selectbox("Tipo", ["Tutti"] + TIPI)
            filtro_categoria = st.selectbox("Categoria", ["Tutte"] + sorted(attive["categoria"].dropna().unique().tolist()))
            rf = attive.copy()
            if filtro_tipo != "Tutti": rf = rf[rf["tipo"] == filtro_tipo]
            if filtro_categoria != "Tutte": rf = rf[rf["categoria"] == filtro_categoria]
            referenza_sel = st.selectbox("Prodotto", rf["referenza"].tolist())
            riga = rf[rf["referenza"] == referenza_sel].iloc[0]
            quantita = st.number_input("Quantità", min_value=0.0, step=0.1)
            note = st.text_input("Note")
            salva = st.form_submit_button("Salva trasferimento")
            if salva:
                if quantita <= 0: st.error("Inserisci una quantità maggiore di zero.")
                else:
                    insert_rows("trasferimenti", [{"data_trasferimento": data_trasferimento.strftime("%Y-%m-%d"), "da_punto_vendita": da_store, "a_punto_vendita": a_store, "codice": str(riga["codice"]), "referenza": str(riga["referenza"]), "tipo": str(riga["tipo"]), "categoria": str(riga["categoria"]), "unita": str(riga["unita"]), "quantita": quantita, "note": note}])
                    st.success("Trasferimento salvato.")
        trasferimenti = fetch_table("trasferimenti")
        if not trasferimenti.empty:
            st.dataframe(trasferimenti.sort_values("data_trasferimento", ascending=False), use_container_width=True)

# =========================
# ACQUISTI E PREZZI MEDI
# =========================

elif menu == "Acquisti e prezzi medi":
    st.subheader("Acquisti e prezzi medi")
    acquisti = fetch_table("acquisti_fatture")
    if acquisti.empty:
        st.info("Nessun acquisto registrato. Carica prima le fatture.")
    else:
        acquisti["data_acquisto"] = pd.to_datetime(acquisti["data_acquisto"], errors="coerce")
        anni = sorted(pd.to_numeric(acquisti["anno"], errors="coerce").dropna().astype(int).unique().tolist(), reverse=True)
        anno_sel = st.selectbox("Anno", anni, index=0)
        mese_sel = st.selectbox("Mese", list(range(1, 13)), index=date.today().month - 1)
        df = acquisti[(pd.to_numeric(acquisti["anno"], errors="coerce") == anno_sel) & (pd.to_numeric(acquisti["mese"], errors="coerce") == mese_sel)].copy()
        if df.empty:
            st.info("Nessun acquisto per il periodo selezionato.")
        else:
            for col in ["quantita", "importo", "prezzo_unitario"]:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
            riepilogo = df.groupby(["codice_interno", "referenza_interna", "unita"], as_index=False).agg(quantita_totale=("quantita", "sum"), importo_totale=("importo", "sum"))
            riepilogo["prezzo_medio"] = riepilogo.apply(lambda r: r["importo_totale"] / r["quantita_totale"] if r["quantita_totale"] else 0, axis=1)
            st.markdown("### Prezzo medio mensile per prodotto interno")
            st.dataframe(riepilogo.sort_values("referenza_interna"), use_container_width=True)
            st.markdown("### Dettaglio righe fattura")
            st.dataframe(df.sort_values(["data_acquisto", "fornitore", "referenza_interna"]), use_container_width=True)

# =========================
# EXPORT
# =========================

elif menu == "Export":
    st.subheader("Export")
    referenze = fetch_table("referenze"); inventari = dedup_inventari_latest(fetch_table("inventari")); alias = fetch_table("fornitori_referenze"); acquisti = fetch_table("acquisti_fatture")
    col1, col2 = st.columns(2)
    with col1:
        st.download_button("Scarica prodotti interni CSV", referenze.to_csv(index=False).encode("utf-8"), "tup_prodotti_interni.csv", "text/csv")
        st.download_button("Scarica alias fornitori CSV", alias.to_csv(index=False).encode("utf-8"), "tup_alias_fornitori.csv", "text/csv")
    with col2:
        st.download_button("Scarica inventari CSV", inventari.to_csv(index=False).encode("utf-8"), "tup_inventari.csv", "text/csv")
        st.download_button("Scarica acquisti fatture CSV", acquisti.to_csv(index=False).encode("utf-8"), "tup_acquisti_fatture.csv", "text/csv")

