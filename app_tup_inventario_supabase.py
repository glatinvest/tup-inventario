import streamlit as st
import pandas as pd
from datetime import date
import re
import tempfile
import io
import zipfile
from html import escape
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
UNITA = ["kg", "g", "pz", "conf", "lt", "ml", "CT", "cartone", "TA", "CF", "NR"]
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


def fetch_inventory_draft(data_inventario, punto_vendita):
    try:
        res = (sb().table("inventari_bozze").select("*")
               .eq("data_inventario", data_inventario)
               .eq("punto_vendita", punto_vendita).execute())
        return pd.DataFrame(res.data)
    except Exception:
        return pd.DataFrame()


def save_inventory_draft(rows):
    if not rows:
        return
    sb().table("inventari_bozze").upsert(
        rows, on_conflict="data_inventario,punto_vendita,codice"
    ).execute()


def delete_inventory_draft(data_inventario, punto_vendita):
    try:
        (sb().table("inventari_bozze").delete()
         .eq("data_inventario", data_inventario)
         .eq("punto_vendita", punto_vendita).execute())
    except Exception:
        pass


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



def rileva_pezzi_cartone(descrizione):
    """Prova a capire da descrizioni tipo P/50, PZ 24, X 12, 12PZ quanti pezzi contiene un cartone/confezione."""
    s = str(descrizione).upper().replace('\'', ' ').replace('°', ' ')
    patterns = [
        r"(?:P\s*/\s*|PZ\s*/\s*|PZ\s+|PZ\.|CONF\s+|P\s+)\s*(\d{1,4})\b",
        r"\b(\d{1,4})\s*(?:PZ|PCS|PEZZI)\b",
        r"\bX\s*(\d{1,4})\b",
        r"\b(\d{1,4})\s*X\b",
    ]
    for pat in patterns:
        m = re.search(pat, s)
        if m:
            n = pulisci_numero(m.group(1))
            if 1 < n <= 1000:
                return float(n)
    return 0.0


def prezzo_singolo_da_cartone(prezzo, pezzi):
    prezzo = pulisci_numero(prezzo); pezzi = pulisci_numero(pezzi)
    return prezzo / pezzi if prezzo > 0 and pezzi > 0 else 0.0


def prepara_df_excel(df):
    out = df.copy() if df is not None else pd.DataFrame()
    if out.empty:
        return out
    for c in out.columns:
        if 'created_at' in c:
            out[c] = out[c].astype(str)
    return out


def _xlsx_col(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell_xml(row, col, value):
    ref = f"{_xlsx_col(col)}{row}"
    if pd.isna(value):
        return f'<c r="{ref}"/>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    txt = escape(str(value))
    return f'<c r="{ref}" t="inlineStr"><is><t>{txt}</t></is></c>'


def _sheet_xml(df):
    df = prepara_df_excel(df)
    rows = []
    cols = list(df.columns)
    rows.append('<row r="1">' + ''.join(_cell_xml(1, i + 1, c) for i, c in enumerate(cols)) + '</row>')
    for r_idx, (_, row) in enumerate(df.iterrows(), start=2):
        rows.append(f'<row r="{r_idx}">' + ''.join(_cell_xml(r_idx, c_idx + 1, row.get(c, "")) for c_idx, c in enumerate(cols)) + '</row>')
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + ''.join(rows) + '</sheetData></worksheet>'


def excel_bytes(sheets):
    # Writer XLSX minimale senza dipendenze esterne: evita errori se openpyxl non è installato su Streamlit Cloud.
    bio = io.BytesIO()
    sheet_items = list(sheets.items()) or [("Foglio1", pd.DataFrame())]
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' + ''.join([f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheet_items)+1)]) + '</Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + ''.join([f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheet_items)+1)]) + '</Relationships>')
        sheets_xml = ''.join([f'<sheet name="{escape(str(name)[:31])}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _) in enumerate(sheet_items, start=1)])
        z.writestr("xl/workbook.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{sheets_xml}</sheets></workbook>')
        for i, (_, df) in enumerate(sheet_items, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(df))
    return bio.getvalue()


def aggiungi_prezzi_referenze(df, referenze):
    out = df.copy()
    if out.empty:
        return out

    # Pulizia colonne nate da merge precedenti (_x/_y) e preservazione del prezzo manuale salvato.
    prezzo_riga = pd.Series(0, index=out.index, dtype="float64")
    for c in ["prezzo_unitario", "prezzo_unitario_x", "prezzo_unitario_y"]:
        if c in out.columns:
            vals = pd.to_numeric(out[c], errors="coerce").fillna(0)
            prezzo_riga = prezzo_riga.where(prezzo_riga > 0, vals)
    out = out.drop(columns=["prezzo_unitario_x", "prezzo_unitario_y", "prezzo_unitario_pz", "pezzi_per_cartone", "prezzo_applicato", "valore"], errors="ignore")
    out["prezzo_unitario"] = prezzo_riga

    if referenze is not None and not referenze.empty and "codice" in out.columns:
        ref = referenze.copy()
        for c in ["prezzo_unitario", "prezzo_unitario_pz", "pezzi_per_cartone"]:
            if c not in ref.columns:
                ref[c] = 0
        ref = ref[["codice", "prezzo_unitario", "prezzo_unitario_pz", "pezzi_per_cartone"]].drop_duplicates("codice", keep="last")
        ref = ref.rename(columns={"prezzo_unitario": "prezzo_unitario_anagrafica"})
        out = out.merge(ref, on="codice", how="left")
    else:
        out["prezzo_unitario_anagrafica"] = 0
        out["prezzo_unitario_pz"] = 0
        out["pezzi_per_cartone"] = 0

    for c in ["quantita", "prezzo_unitario", "prezzo_unitario_anagrafica", "prezzo_unitario_pz", "pezzi_per_cartone"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0)

    # Se il prezzo manuale della riga è zero, propongo quello dell'anagrafica.
    out["prezzo_unitario"] = out["prezzo_unitario"].where(out["prezzo_unitario"] > 0, out["prezzo_unitario_anagrafica"])
    out["prezzo_applicato"] = out.apply(lambda r: r.get("prezzo_unitario_pz", 0) if str(r.get("unita", "")).lower() in ["pz", "nr"] and r.get("prezzo_unitario_pz", 0) > 0 else r.get("prezzo_unitario", 0), axis=1)
    out["valore"] = pd.to_numeric(out.get("quantita", 0), errors="coerce").fillna(0) * pd.to_numeric(out.get("prezzo_unitario", 0), errors="coerce").fillna(0)
    out = out.drop(columns=["prezzo_unitario_anagrafica"], errors="ignore")
    return out

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


def registra_acquisto(data_acquisto, fornitore, codice_interno, referenza_interna, descrizione_fornitore, codice_fornitore, quantita, unita, prezzo, importo, iva, nome_file="", pezzi_per_cartone=0, prezzo_unitario_pz=0):
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
        "pezzi_per_cartone": pulisci_numero(pezzi_per_cartone),
        "prezzo_unitario_pz": pulisci_numero(prezzo_unitario_pz),
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
    MENU_OPTIONS = ["Dashboard", "Import fatture", "Anagrafica referenze", "Inventario mensile", "Trasferimenti merci", "Acquisti e prezzi medi", "Export"]
    pagina_url = st.query_params.get("pagina", "Dashboard")
    if pagina_url not in MENU_OPTIONS:
        pagina_url = "Dashboard"
    menu = st.radio("Menu", MENU_OPTIONS, index=MENU_OPTIONS.index(pagina_url), key="menu_principale")
    if st.query_params.get("pagina") != menu:
        st.query_params["pagina"] = menu

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
            # Merge robusto: rinomino i prezzi anagrafica prima del merge per evitare
            # colonne duplicate o tipi misti che possono mandare in errore Pandas.
            ref_merge = ref[["codice", "prezzo_unitario", "scorta_minima"]].copy()
            ref_merge = ref_merge.rename(columns={
                "prezzo_unitario": "prezzo_unitario_anagrafica",
                "scorta_minima": "scorta_minima_anagrafica"
            })
            valorizzato = ultimo.merge(ref_merge, on="codice", how="left")

            # Prezzo: prima uso quello salvato nell'inventario, se presente e > 0;
            # altrimenti uso quello dell'anagrafica.
            if "prezzo_unitario" in valorizzato.columns:
                prezzo_inv = pd.to_numeric(valorizzato["prezzo_unitario"], errors="coerce").fillna(0)
            else:
                prezzo_inv = pd.Series(0, index=valorizzato.index, dtype="float64")
            prezzo_ana = pd.to_numeric(valorizzato.get("prezzo_unitario_anagrafica", 0), errors="coerce")
            if not isinstance(prezzo_ana, pd.Series):
                prezzo_ana = pd.Series(0, index=valorizzato.index, dtype="float64")
            prezzo_ana = prezzo_ana.fillna(0)
            valorizzato["prezzo_unitario"] = prezzo_inv.where(prezzo_inv > 0, prezzo_ana).astype(float)

            # Scorta minima: stessa logica, ma se manca uso zero.
            if "scorta_minima" in valorizzato.columns:
                scorta_inv = pd.to_numeric(valorizzato["scorta_minima"], errors="coerce").fillna(0)
            else:
                scorta_inv = pd.Series(0, index=valorizzato.index, dtype="float64")
            scorta_ana = pd.to_numeric(valorizzato.get("scorta_minima_anagrafica", 0), errors="coerce")
            if not isinstance(scorta_ana, pd.Series):
                scorta_ana = pd.Series(0, index=valorizzato.index, dtype="float64")
            scorta_ana = scorta_ana.fillna(0)
            valorizzato["scorta_minima"] = scorta_inv.where(scorta_inv > 0, scorta_ana).astype(float)
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
            df["pezzi_per_cartone"] = df["referenza"].apply(rileva_pezzi_cartone)
            df["prezzo_unitario_pz"] = df.apply(lambda r: prezzo_singolo_da_cartone(r.get("prezzo_unitario", 0), r.get("pezzi_per_cartone", 0)), axis=1)
            colonne = ["importa", "prodotto_interno", "referenza", "tipo", "categoria", "unita", "fornitore", "codice_fornitore", "prezzo_unitario", "pezzi_per_cartone", "prezzo_unitario_pz", "ultima_quantita", "ultimo_importo", "iva", "scorta_minima", "attivo", "nome_file"]
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
                    "pezzi_per_cartone": st.column_config.NumberColumn("pezzi per cartone", min_value=0.0, step=1.0, help="Se il prezzo è del cartone/confezione, indica quanti pezzi contiene. Se l’app lo capisce dalla descrizione lo precompila."),
                    "prezzo_unitario_pz": st.column_config.NumberColumn("prezzo singolo pz", min_value=0.0, step=0.001),
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
                                "pezzi_per_cartone": pulisci_numero(row.get("pezzi_per_cartone", 0)),
                                "prezzo_unitario_pz": pulisci_numero(row.get("prezzo_unitario_pz", 0)) or prezzo_singolo_da_cartone(row.get("prezzo_unitario", 0), row.get("pezzi_per_cartone", 0)),
                                "ultima_quantita": pulisci_numero(row.get("ultima_quantita", 0)),
                                "ultimo_importo": pulisci_numero(row.get("ultimo_importo", 0)),
                                "pezzi_per_cartone": pulisci_numero(row.get("pezzi_per_cartone", hit.iloc[0].get("pezzi_per_cartone", 0))),
                                "prezzo_unitario_pz": pulisci_numero(row.get("prezzo_unitario_pz", 0)) or prezzo_singolo_da_cartone(row.get("prezzo_unitario", 0), row.get("pezzi_per_cartone", hit.iloc[0].get("pezzi_per_cartone", 0))),
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
                        registra_acquisto(data_fattura, row.get("fornitore", ""), codice_interno, referenza_interna, nome_fattura, row.get("codice_fornitore", ""), row.get("ultima_quantita", 0), row.get("unita", "pz"), row.get("prezzo_unitario", 0), row.get("ultimo_importo", 0), row.get("iva", ""), row.get("nome_file", ""), row.get("pezzi_per_cartone", 0), row.get("prezzo_unitario_pz", 0) or prezzo_singolo_da_cartone(row.get("prezzo_unitario", 0), row.get("pezzi_per_cartone", 0)))
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
            pezzi_per_cartone = st.number_input("Pezzi per cartone/confezione", min_value=0.0, step=1.0)
            prezzo_unitario_pz = prezzo_singolo_da_cartone(prezzo, pezzi_per_cartone)
            if prezzo_unitario_pz:
                st.caption(f"Prezzo singolo pezzo calcolato: € {prezzo_unitario_pz:.3f}")
            scorta_minima = st.number_input("Scorta minima", min_value=0.0, step=0.1)
            salva = st.form_submit_button("Aggiungi prodotto")
            if salva:
                referenze = fetch_table("referenze"); nome = pulisci_testo(referenza)
                if not nome:
                    st.error("Inserisci un prodotto.")
                else:
                    codice = genera_codice(nome, referenze)
                    insert_rows("referenze", [{"codice": codice, "referenza": nome, "tipo": tipo, "categoria": categoria, "unita": unita, "fornitore": "MULTI", "codice_fornitore": "", "prezzo_unitario": prezzo, "pezzi_per_cartone": pezzi_per_cartone, "prezzo_unitario_pz": prezzo_unitario_pz, "ultima_quantita": 0.0, "ultimo_importo": 0.0, "iva": "", "scorta_minima": scorta_minima, "attivo": "si"}])
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
                    update_row("referenze", row["id"], {"codice": str(row.get("codice", "")), "referenza": str(row.get("referenza", "")), "tipo": str(row.get("tipo", "Food")), "categoria": str(row.get("categoria", "Altro")), "unita": str(row.get("unita", "pz")), "fornitore": str(row.get("fornitore", "MULTI")), "codice_fornitore": str(row.get("codice_fornitore", "")), "prezzo_unitario": pulisci_numero(row.get("prezzo_unitario", 0)), "pezzi_per_cartone": pulisci_numero(row.get("pezzi_per_cartone", 0)), "prezzo_unitario_pz": pulisci_numero(row.get("prezzo_unitario_pz", 0)) or prezzo_singolo_da_cartone(row.get("prezzo_unitario", 0), row.get("pezzi_per_cartone", 0)), "ultima_quantita": pulisci_numero(row.get("ultima_quantita", 0)), "ultimo_importo": pulisci_numero(row.get("ultimo_importo", 0)), "iva": str(row.get("iva", "")), "scorta_minima": pulisci_numero(row.get("scorta_minima", 0)), "attivo": str(row.get("attivo", "si"))})
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
                disabled=["id", "fornitore", "codice_fornitore", "descrizione_fornitore"],
                column_config={
                    "unita": st.column_config.SelectboxColumn("unità", options=UNITA),
                    "ultimo_prezzo": st.column_config.NumberColumn("ultimo prezzo", min_value=0.0, step=0.001),
                    "prodotto_interno": st.column_config.SelectboxColumn("prodotto interno corretto", options=options),
                },
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
                        if not aid:
                            continue
                        vecchio_label = str(originale.get(aid, ""))
                        nuovo_codice = label_to_codice.get(nuovo_label, "") if nuovo_label else ""
                        nuova_ref = codice_to_nome.get(nuovo_codice, "") if nuovo_codice else ""
                        payload_alias = {
                            "unita": str(row.get("unita", "pz")),
                            "ultimo_prezzo": pulisci_numero(row.get("ultimo_prezzo", 0)),
                        }
                        update_row("fornitori_referenze", aid, payload_alias)
                        if nuovo_codice and nuovo_label != vecchio_label:
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
        st.info("Le modifiche vengono salvate automaticamente come bozza. Se il telefono ricarica la pagina, ritrovi i dati inseriti.")
        attive = referenze[referenze["attivo"].astype(str).str.lower() != "no"].copy().sort_values(["tipo", "categoria", "referenza"])
        attive = attive.drop_duplicates(subset=["codice"], keep="last")
        store_tabs = st.tabs(stores_visibili())
        edited_tables = []
        draft_status = []
        for tab, store in zip(store_tabs, stores_visibili()):
            with tab:
                st.markdown(f"### {store}")
                base = attive[["codice", "referenza", "tipo", "categoria", "unita", "prezzo_unitario"]].copy()
                base.insert(0, "punto_vendita", store); base["quantita"] = 0.0; base["importo"] = 0.0; base["note"] = ""

                # Priorità: bozza automatica > inventario definitivo già salvato > valori anagrafica.
                bozza = fetch_inventory_draft(data_str, store)
                esistente = inventari[(inventari["data_inventario"].astype(str) == data_str) & (inventari["punto_vendita"].astype(str) == store)].copy() if not inventari.empty else pd.DataFrame()
                sorgente = bozza if not bozza.empty else esistente
                if not bozza.empty:
                    st.caption("✅ Bozza automatica ripristinata")
                if not sorgente.empty:
                    cols = [c for c in ["codice", "quantita", "note", "unita", "prezzo_unitario", "importo"] if c in sorgente.columns]
                    small = sorgente[cols].copy().drop_duplicates("codice", keep="last")
                    if "unita" in small.columns:
                        small = small.rename(columns={"unita": "unita_salvata"})
                    base = base.drop(columns=["quantita", "note", "prezzo_unitario", "importo"], errors="ignore").merge(small, on="codice", how="left")
                    base["quantita"] = pd.to_numeric(base.get("quantita", 0), errors="coerce").fillna(0)
                    base["note"] = base.get("note", "").fillna("")
                    base["prezzo_unitario"] = pd.to_numeric(base.get("prezzo_unitario", 0), errors="coerce").fillna(0)
                    base["importo"] = pd.to_numeric(base.get("importo", 0), errors="coerce").fillna(0)
                    if "unita_salvata" in base.columns:
                        base["unita"] = base["unita_salvata"].fillna(base["unita"])
                        base = base.drop(columns=["unita_salvata"], errors="ignore")

                filtro_tipo = st.selectbox("Tipo", ["Tutti"] + TIPI, key=f"tipo_inv_{store}")
                filtro_categoria = st.multiselect("Categoria", sorted(attive["categoria"].dropna().unique().tolist()), key=f"cat_inv_{store}")
                tabella = base.copy()
                if filtro_tipo != "Tutti": tabella = tabella[tabella["tipo"] == filtro_tipo]
                if filtro_categoria: tabella = tabella[tabella["categoria"].isin(filtro_categoria)]
                edited = st.data_editor(
                    tabella,
                    use_container_width=True,
                    hide_index=True,
                    disabled=["punto_vendita", "codice", "referenza", "tipo", "categoria"],
                    column_config={
                        "unita": st.column_config.SelectboxColumn("Unità", options=UNITA),
                        "quantita": st.column_config.NumberColumn("Quantità", min_value=0.0, step=0.1),
                        "prezzo_unitario": st.column_config.NumberColumn("Prezzo unitario", min_value=0.0, step=0.001),
                        "importo": st.column_config.NumberColumn("Importo", min_value=0.0, step=0.01),
                    },
                    key=f"inventario_{data_str}_{store}",
                )
                edited_tables.append(edited)

                # Autosalvataggio persistente: eseguito a ogni modifica del data editor.
                draft_rows = []
                for _, row in edited.iterrows():
                    q = pulisci_numero(row.get("quantita", 0))
                    p = pulisci_numero(row.get("prezzo_unitario", 0))
                    imp = pulisci_numero(row.get("importo", 0)) or (q * p)
                    draft_rows.append({
                        "data_inventario": data_str, "mese": mese, "anno": anno,
                        "punto_vendita": store, "codice": str(row.get("codice", "")),
                        "referenza": str(row.get("referenza", "")), "tipo": str(row.get("tipo", "Food")),
                        "categoria": str(row.get("categoria", "Altro")), "unita": str(row.get("unita", "pz")),
                        "quantita": q, "prezzo_unitario": p, "importo": imp,
                        "note": str(row.get("note", "")), "updated_at": pd.Timestamp.utcnow().isoformat()
                    })
                try:
                    save_inventory_draft(draft_rows)
                    draft_status.append(True)
                except Exception as e:
                    draft_status.append(False)
                    st.warning(f"Bozza non salvata per {store}: {e}")

        if draft_status and all(draft_status):
            st.caption("💾 Bozza aggiornata automaticamente su Supabase")

        col_salva, col_svuota = st.columns(2)
        with col_salva:
            salva_definitivo = st.button("Salva inventario definitivo", type="primary")
        with col_svuota:
            elimina_bozza = st.button("Azzera bozze del giorno")

        if elimina_bozza:
            for store in stores_visibili():
                delete_inventory_draft(data_str, store)
            st.success("Bozze eliminate.")
            st.rerun()

        if salva_definitivo:
            edited_all = pd.concat(edited_tables, ignore_index=True) if edited_tables else pd.DataFrame()
            rows = []
            for _, row in edited_all.iterrows():
                q = pulisci_numero(row.get("quantita", 0)); p = pulisci_numero(row.get("prezzo_unitario", 0))
                rows.append({"data_inventario": data_str, "mese": mese, "anno": anno, "punto_vendita": str(row["punto_vendita"]), "codice": str(row["codice"]), "referenza": str(row["referenza"]), "tipo": str(row["tipo"]), "categoria": str(row["categoria"]), "unita": str(row["unita"]), "quantita": q, "prezzo_unitario": p, "importo": pulisci_numero(row.get("importo", 0)) or (q * p), "note": str(row.get("note", ""))})
            if rows:
                for store in stores_visibili():
                    delete_inventory(data_str, store)
                insert_rows("inventari", rows)
                for store in stores_visibili():
                    delete_inventory_draft(data_str, store)
                st.success("Inventario definitivo salvato. La bozza è stata rimossa.")
                st.rerun()

        st.divider(); st.subheader("Inventari salvati")
        inventari = dedup_inventari_latest(fetch_table("inventari"))
        if inventari.empty:
            st.info("Nessun inventario salvato.")
        else:
            chiavi = inventari[["data_inventario", "punto_vendita"]].drop_duplicates().sort_values(["data_inventario", "punto_vendita"], ascending=[False, True])
            for _, k in chiavi.iterrows():
                with st.expander(f"{k['data_inventario']} - {k['punto_vendita']}"):
                    df_inv = inventari[(inventari["data_inventario"].astype(str) == str(k["data_inventario"])) & (inventari["punto_vendita"].astype(str) == str(k["punto_vendita"]))]
                    df_inv_export = aggiungi_prezzi_referenze(df_inv.sort_values(["tipo", "categoria", "referenza"]), fetch_table("referenze"))
                    st.dataframe(df_inv_export, use_container_width=True)
                    st.download_button("Scarica inventario Excel", excel_bytes({"Inventario": df_inv_export}), f"tup_inventario_{k['data_inventario']}_{k['punto_vendita']}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=f"dl_inv_{k['data_inventario']}_{k['punto_vendita']}")

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
            unita_default = str(riga.get("unita", "pz"))
            unita_index = UNITA.index(unita_default) if unita_default in UNITA else 0
            unita_trasferimento = st.selectbox("Unità trasferimento", UNITA, index=unita_index)
            prezzo_cartone = pulisci_numero(riga.get("prezzo_unitario", 0))
            pezzi_cartone = pulisci_numero(riga.get("pezzi_per_cartone", 0)) if "pezzi_per_cartone" in riga.index else 0
            prezzo_pz = pulisci_numero(riga.get("prezzo_unitario_pz", 0)) if "prezzo_unitario_pz" in riga.index else 0
            prezzo_default = prezzo_pz if unita_trasferimento in ["pz", "NR"] and prezzo_pz > 0 else prezzo_cartone
            prezzo_trasferimento = st.number_input("Prezzo unitario trasferimento", min_value=0.0, step=0.001, value=float(prezzo_default))
            quantita = st.number_input("Quantità", min_value=0.0, step=0.1)
            note = st.text_input("Note")
            salva = st.form_submit_button("Salva trasferimento")
            if salva:
                if quantita <= 0: st.error("Inserisci una quantità maggiore di zero.")
                else:
                    insert_rows("trasferimenti", [{"data_trasferimento": data_trasferimento.strftime("%Y-%m-%d"), "da_punto_vendita": da_store, "a_punto_vendita": a_store, "codice": str(riga["codice"]), "referenza": str(riga["referenza"]), "tipo": str(riga["tipo"]), "categoria": str(riga["categoria"]), "unita": str(unita_trasferimento), "quantita": quantita, "prezzo_unitario": prezzo_trasferimento, "importo": quantita * prezzo_trasferimento, "note": note}])
                    st.success("Trasferimento salvato.")
        trasferimenti = fetch_table("trasferimenti")
        if not trasferimenti.empty:
            trasferimenti["data_trasferimento"] = pd.to_datetime(trasferimenti["data_trasferimento"], errors="coerce")
            trasferimenti["mese"] = trasferimenti["data_trasferimento"].dt.month
            trasferimenti["anno"] = trasferimenti["data_trasferimento"].dt.year
            anni_tr = sorted(trasferimenti["anno"].dropna().astype(int).unique().tolist(), reverse=True)
            colm1, colm2 = st.columns(2)
            with colm1:
                anno_tr = st.selectbox("Anno scheda trasferimenti", anni_tr, index=0)
            with colm2:
                mese_tr = st.selectbox("Mese scheda trasferimenti", list(range(1, 13)), index=date.today().month - 1)
            mese_df = trasferimenti[(trasferimenti["anno"] == anno_tr) & (trasferimenti["mese"] == mese_tr)].copy()
            referenze_now = fetch_table("referenze")
            mese_df = aggiungi_prezzi_referenze(mese_df.drop(columns=["prezzo_unitario_y", "prezzo_unitario_x"], errors="ignore"), referenze_now) if not mese_df.empty else mese_df
            st.markdown(f"### Scheda trasferimenti {mese_tr:02d}/{anno_tr}")
            mese_view = mese_df.sort_values("data_trasferimento", ascending=False).copy()
            edited_tr = st.data_editor(
                mese_view,
                use_container_width=True,
                hide_index=True,
                disabled=[c for c in mese_view.columns if c not in ["unita", "prezzo_unitario", "quantita", "importo", "note"]],
                column_config={
                    "unita": st.column_config.SelectboxColumn("Unità", options=UNITA),
                    "prezzo_unitario": st.column_config.NumberColumn("Prezzo unitario", min_value=0.0, step=0.001),
                    "quantita": st.column_config.NumberColumn("Quantità", min_value=0.0, step=0.1),
                    "importo": st.column_config.NumberColumn("Importo", min_value=0.0, step=0.01),
                },
                key=f"trasferimenti_mese_editor_{anno_tr}_{mese_tr}",
            )
            if st.button("Salva modifiche scheda trasferimenti", key=f"save_tr_{anno_tr}_{mese_tr}"):
                salvati = 0
                for _, rr in edited_tr.iterrows():
                    if "id" in rr and not pd.isna(rr.get("id")):
                        q = pulisci_numero(rr.get("quantita", 0)); pr = pulisci_numero(rr.get("prezzo_unitario", 0))
                        imp = pulisci_numero(rr.get("importo", 0)) or q * pr
                        update_row("trasferimenti", rr["id"], {"unita": str(rr.get("unita", "pz")), "quantita": q, "prezzo_unitario": pr, "importo": imp, "note": str(rr.get("note", ""))})
                        salvati += 1
                st.success(f"Scheda trasferimenti aggiornata: {salvati} righe")
                st.rerun()
            st.download_button(
                "Scarica scheda trasferimenti mensile Excel",
                excel_bytes({f"Trasferimenti {mese_tr:02d}-{anno_tr}": edited_tr}),
                f"tup_trasferimenti_{anno_tr}_{mese_tr:02d}.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

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
    referenze = fetch_table("referenze"); inventari = dedup_inventari_latest(fetch_table("inventari")); alias = fetch_table("fornitori_referenze"); acquisti = fetch_table("acquisti_fatture"); trasferimenti = fetch_table("trasferimenti")
    col1, col2 = st.columns(2)
    with col1:
        st.download_button("Scarica prodotti interni CSV", referenze.to_csv(index=False).encode("utf-8"), "tup_prodotti_interni.csv", "text/csv")
        st.download_button("Scarica alias fornitori CSV", alias.to_csv(index=False).encode("utf-8"), "tup_alias_fornitori.csv", "text/csv")
    with col2:
        inv_xlsx = aggiungi_prezzi_referenze(inventari, referenze)
        tr_xlsx = aggiungi_prezzi_referenze(trasferimenti, referenze)
        st.download_button("Scarica inventari Excel", excel_bytes({"Inventari": inv_xlsx}), "tup_inventari.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.download_button("Scarica trasferimenti Excel", excel_bytes({"Trasferimenti": tr_xlsx}), "tup_trasferimenti.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.download_button("Scarica acquisti fatture CSV", acquisti.to_csv(index=False).encode("utf-8"), "tup_acquisti_fatture.csv", "text/csv")






