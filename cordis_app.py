"""CORDIS · collecte des livrables (DMP & données de recherche) d'une université.

Installation : python -m pip install streamlit pandas requests
Lancement    : python -m streamlit run cordis_app.py
Fichiers     : cordis_app.py + system_prompt.md (même dossier)

La logique (extraction, téléchargement, rapport) ne dépend pas de Streamlit :
`import cordis_app` fonctionne sans l'interface, ce qui permet de la tester.
"""
from __future__ import annotations

import copy
import hashlib
import html as htmllib
import itertools
import json
import os
import re
import time
import unicodedata
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import pandas as pd
import requests

APP_DIR = Path(__file__).parent
CACHE = APP_DIR / "cache"
CONFIG_FILE = APP_DIR / "config.json"
PROMPT_FILE = APP_DIR / "system_prompt.md"
UA = {"User-Agent": "cordis-harvester/1.2 (usage académique)"}
MIN_DELAY = 1.0  # le prompt impose 1 requête/seconde au maximum

# Les livrables sont publiés dans une archive CSV DISTINCTE de celle des projets (data.europa.eu) :
# lire seulement l'archive « projets » donne 0 livrable pour tous les projets.
#  - Horizon Europe : nom d'archive confirmé (cordis-HORIZONprojectDeliverables-csv.zip).
#  - Horizon 2020   : nom déduit du même schéma, NON vérifié. S'il échoue, l'app l'indique ; collez alors
#                     l'URL exacte (ou un chemin local .zip) dans « Sources CORDIS ».
#  - FP7            : pas de CSV de livrables (CORDIS les récupère dynamiquement via OpenAIRE).
_BASE = "https://cordis.europa.eu/data/"
SOURCES = {
    "Horizon Europe": {"projects": _BASE + "cordis-HORIZONprojects-csv.zip",
                       "deliverables": _BASE + "cordis-HORIZONprojectDeliverables-csv.zip"},
    "Horizon 2020": {"projects": _BASE + "cordis-h2020projects-csv.zip",
                     "deliverables": _BASE + "cordis-h2020projectDeliverables-csv.zip"},
    "FP7": {"projects": _BASE + "cordis-fp7projects-csv.zip", "deliverables": ""},
}
NO_DELIVERABLES = {"FP7"}
DEFAULTS = dict(
    university="", pics="", role="coordinator",
    programmes=["Horizon Europe", "Horizon 2020", "FP7"],
    filter_mode="all",  # "all" : tout récupérer (filtrage ensuite) · "regex" : filtre DMP / données
    # Les regex filtrent (mode regex) et classent de façon indicative ; elles sont appliquées sans
    # casse ni accents (texte et motif normalisés).
    regex_dmp=r"data management plan|\bdmp\b|plan de gestion de donnees",
    regex_data=r"data ?set|research data|open research data|donnees de la recherche",
    use_description=False, cache_days=7, outdir="downloads", max_mb=50, delay=1.0,
    respect_robots=True, sources=SOURCES,
)

# Statuts (valeurs identiques à celles du prompt système)
ST_PENDING = "à télécharger"
ST_NO_URL = "publié sans URL"
ST_OK = "fichier récupéré"
ST_LANDING = "page HTML d'atterrissage"
ST_DEAD = "lien mort"
ST_BAD_URL = "URL invalide"
ST_RESTRICTED = "accès restreint (non contourné)"
ST_TOO_LARGE = "fichier trop volumineux"
ST_NETWORK = "erreur réseau / timeout"
ST_ROBOTS = "bloqué par robots.txt"
ST_LOCAL = "erreur locale d'écriture"

EXT = {
    "application/pdf": ".pdf", "application/zip": ".zip", "text/csv": ".csv",
    "text/plain": ".txt", "application/json": ".json", "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}
HTML_TYPES = {"text/html", "application/xhtml+xml"}
RETRY_CODES = {429, 500, 502, 503, 504}
LOGIN_RE = re.compile(r"login|signin|sign-in|sso|shibboleth|ecas|eulogin|/cas/", re.I)
# Le portail EC (downloadPublic?documentIds=…) répond par une page HTML intermédiaire : cookie de session
# + redirection JavaScript (ou meta refresh) vers une URL à jeton. On rejoue ce flux (2 sauts maximum).
MAX_HOPS = 2
META_REFRESH_RE = re.compile(r"""<meta[^>]+http-equiv\s*=\s*["']?refresh["']?[^>]*content\s*=\s*["'][^"']*?url\s*=\s*([^"'>\s]+)""", re.I)
JS_REDIRECT_RE = re.compile(
    r"""(?:window\.|document\.|top\.|self\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']"""
    r"""|location\.(?:replace|assign)\(\s*["']([^"']+)["']\s*\)""", re.I)

# Colonnes lues (minuscules) : on évite de charger les gros champs inutiles (objective, etc.)
OID_NAMES = ("organisationID", "organizationID")
ORG_COLS = {"projectid", "organisationid", "organizationid", "role"}
PROJ_COLS = {"id", "acronym", "title", "startdate", "enddate"}
DLV_COLS = {"projectid", "deliverabletype", "type", "title", "description", "url"}

HIT_COLS = ["programme", "projectID", "acronym", "kind", "type", "title", "url",
            "status", "project_title", "start", "end", "description"]
COV_COLS = ["programme", "projectID", "acronym", "project_title", "start", "end",
            "n_livrables", "n_cibles", "n_avec_url", "n_dmp", "n_donnees", "couverture"]
MANIFEST_COLS = ["programme", "projectID", "acronym", "kind", "type", "title", "url", "status",
                 "detail", "http_status", "content_type", "final_url", "size_bytes", "file",
                 "sha256", "collected_at"]


# ───────────────────────── Configuration ─────────────────────────
def _norm_source(value, default):
    """Accepte l'ancien format (simple URL) ; une archive « livrables » vide reprend la valeur par défaut."""
    if isinstance(value, str):
        value = {"projects": value}
    if not isinstance(value, dict):
        return dict(default)
    return {"projects": value.get("projects") or default["projects"],
            "deliverables": value.get("deliverables") or default["deliverables"]}


def load_cfg():
    cfg = copy.deepcopy(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    cfg["sources"] = {p: _norm_source(cfg.get("sources", {}).get(p), SOURCES[p]) for p in SOURCES}
    cfg["programmes"] = [p for p in cfg["programmes"] if p in SOURCES]
    cfg["delay"] = max(MIN_DELAY, float(cfg["delay"]))
    if cfg.get("filter_mode") not in ("all", "regex"):
        cfg["filter_mode"] = "all"
    return cfg


def save_cfg(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_pics(text):
    return {p for p in re.split(r"[,\s;]+", text or "") if p}


# ───────────────────────── Texte & classification ─────────────────────────
def norm(text):
    """Retire les accents (la casse est gérée par re.I)."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def compile_regex(cfg):
    for key in ("regex_dmp", "regex_data"):
        if not cfg[key].strip():
            raise ValueError(f"{key} est vide (elle correspondrait à tous les livrables)")
    return re.compile(norm(cfg["regex_dmp"]), re.I), re.compile(norm(cfg["regex_data"]), re.I)


def classify(text, dmp_re, data_re):
    t, kinds = norm(text), []
    if dmp_re.search(t):
        kinds.append("DMP")
    if data_re.search(t):
        kinds.append("Données")
    return "+".join(kinds)


# ───────────────────────── Accès aux données CORDIS ─────────────────────────
def get_archive(label, src, max_age_days):
    """Retourne le chemin d'un zip : fichier local, ou URL téléchargée puis mise en cache."""
    src = src.strip()
    if not re.match(r"https?://", src, re.I):
        p = Path(src).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"Fichier local introuvable : {src}")
        return p
    CACHE.mkdir(exist_ok=True)
    # La clé de cache inclut l'URL : changer d'URL ne doit jamais resservir l'ancienne archive
    path = CACHE / (re.sub(r"\W+", "_", label) + "_" + hashlib.sha1(src.encode()).hexdigest()[:8] + ".zip")
    fresh = path.exists() and time.time() - path.stat().st_mtime < max_age_days * 86400
    if fresh and zipfile.is_zipfile(path):
        return path
    tmp = path.with_suffix(".part")
    with requests.get(src, headers=UA, stream=True, timeout=(15, 120)) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    if not zipfile.is_zipfile(tmp):
        tmp.unlink(missing_ok=True)
        raise zipfile.BadZipFile(f"{src} n'est pas une archive zip valide")
    os.replace(tmp, path)
    return path


def read_table(zpath, filename, wanted, ctx, warns, fuzzy=None, quiet=False):
    """Lit un CSV de l'archive (DataFrame vide s'il est absent) ; anomalies consignées dans warns."""
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        match = next((n for n in names if Path(n).name.lower() == filename.lower()), None)
        if not match and fuzzy:
            match = next((n for n in names if n.lower().endswith(".csv")
                          and fuzzy.lower() in Path(n).name.lower()), None)
        if not match:
            if not quiet:
                warns.append(f"{ctx} : {filename} absent de l'archive {Path(zpath).name}")
            return pd.DataFrame()
        kw = dict(sep=";", dtype=str, keep_default_na=False, encoding="utf-8-sig",
                  usecols=lambda c: c.strip().lower() in wanted)
        try:
            with z.open(match) as f:
                return pd.read_csv(f, **kw)
        except pd.errors.ParserError:
            warns.append(f"{ctx} : lignes mal formées ignorées dans {match}")
            with z.open(match) as f:
                return pd.read_csv(f, on_bad_lines="skip", **kw)
        except pd.errors.EmptyDataError:
            warns.append(f"{ctx} : {match} ne contient aucune colonne attendue")
            return pd.DataFrame()


def col(df, names):
    """Retrouve une colonne par nom, sans tenir compte de la casse (schémas H2020 / HE)."""
    if isinstance(names, str):
        names = (names,)
    low = {c.strip().lower(): c for c in df.columns}
    return next((low[n.lower()] for n in names if n.lower() in low), None)


def s(df, names, warns=None, ctx=""):
    """Colonne sous forme de Series de chaînes ; signale (warns) toute colonne introuvable."""
    if isinstance(names, str):
        names = (names,)
    c = col(df, names)
    if c:
        return df[c].astype(str)
    if warns is not None:
        warns.append(f"{ctx} : colonne introuvable ({' / '.join(names)})")
    return pd.Series("", index=df.index, dtype=str)


def coverage_label(n_liv, n_cib, n_url, no_dlv=False):
    if no_dlv:
        return "livrables non disponibles en CSV pour ce programme (OpenAIRE)"
    if n_liv == 0:
        return "non retrouvé dans CORDIS (aucun livrable publié)"
    if n_cib == 0:
        return "livrables publiés, aucun retenu par le filtre"
    if n_url == 0:
        return "retenus publiés sans URL"
    return "retenus avec URL"


def _dedupe_hits(h):
    """Dédoublonne par _key en fusionnant les « kind » (un même lien peut être DMP et Données)."""
    if h.empty:
        return h
    agg = {c: "first" for c in h.columns if c not in ("_key", "kind")}
    agg["kind"] = lambda k: "+".join(sorted({x for v in k for x in v.split("+") if x}))
    return h.groupby("_key", sort=False, as_index=False).agg(agg)


def _concat(parts, cols):
    parts = [p for p in parts if not p.empty]
    return pd.concat(parts, ignore_index=True).reindex(columns=cols) if parts else pd.DataFrame(columns=cols)


def _read_deliverables(prog, src, zp, cfg, warns):
    """Cherche projectDeliverables.csv : archive « livrables » d'abord, puis archive projets."""
    candidates = []
    dsrc = src.get("deliverables", "").strip()
    if dsrc:
        try:
            candidates.append(get_archive(prog + "_livrables", dsrc, cfg["cache_days"]))
        except (requests.RequestException, zipfile.BadZipFile, OSError) as e:
            warns.append(f"{prog} : archive des livrables inaccessible ({type(e).__name__} : {str(e)[:140]}). "
                         "Vérifiez l'URL dans « Sources CORDIS » (page data.europa.eu du programme).")
    candidates.append(zp)
    for cz in candidates:
        try:
            df = read_table(cz, "projectDeliverables.csv", DLV_COLS, prog, warns, fuzzy="deliverable", quiet=True)
        except zipfile.BadZipFile:
            warns.append(f"{prog} : {Path(cz).name} n'est pas une archive zip valide")
            continue
        if not df.empty:
            return df, Path(cz).name
    warns.append(f"{prog} : aucun livrable lu (projectDeliverables.csv introuvable ou vide) — "
                 "tous les projets apparaîtront à 0 livrable. Renseignez l'archive des livrables.")
    return pd.DataFrame(), ""


def extract(cfg):
    """Retourne dict(hits, cov, types, files, diag, warnings, cfg, generated_at)."""
    warns = []
    pics = parse_pics(cfg["pics"])
    dmp_re, data_re = compile_regex(cfg)
    mode = cfg.get("filter_mode", "all")
    warns += [f"PIC « {p} » : format inattendu (9 chiffres attendus)"
              for p in sorted(pics) if not re.fullmatch(r"\d{9}", p)]
    hit_parts, cov_parts, type_parts, files, seen = [], [], [], [], set()

    for prog in cfg["programmes"]:
        src, ctx = cfg["sources"][prog], prog
        no_dlv = prog in NO_DELIVERABLES
        zp = get_archive(prog, src["projects"], cfg["cache_days"])
        org = read_table(zp, "organization.csv", ORG_COLS, ctx, warns)
        proj = read_table(zp, "project.csv", PROJ_COLS, ctx, warns)
        dlv, dlv_origin = (pd.DataFrame(), "—") if no_dlv else _read_deliverables(prog, src, zp, cfg, warns)
        if org.empty or proj.empty:
            continue

        # 1) Projets : PIC + rôle (jamais de repli silencieux si le rôle est invérifiable)
        c_oid, c_pid = col(org, OID_NAMES), col(org, "projectID")
        if not c_oid or not c_pid:
            warns.append(f"{ctx} : organization.csv sans organisationID/projectID — programme ignoré")
            continue
        oid = org[c_oid].str.strip()
        mask = oid.isin(pics)
        seen |= set(oid[mask])
        sel = org[mask]
        if cfg["role"] == "coordinator":
            c_role = col(org, "role")
            if not c_role:
                warns.append(f"{ctx} : colonne role introuvable — rôle non vérifiable, programme ignoré")
                continue
            sel = sel[sel[c_role].str.strip().str.lower() == "coordinator"]
        ids = set(sel[c_pid].str.strip())
        if not ids:
            files.append({"programme": prog, "projets": 0, "livrables_dans_le_fichier": len(dlv),
                          "archive_livrables": dlv_origin})
            continue

        pid = s(proj, "id", warns, ctx).str.strip()
        m = pid.isin(ids)
        pr = proj[m]
        p = pd.DataFrame({
            "programme": prog, "projectID": pid[m],
            "acronym": s(pr, "acronym", warns, ctx), "project_title": s(pr, "title", warns, ctx),
            "start": s(pr, "startDate", warns, ctx), "end": s(pr, "endDate", warns, ctx),
        }).reset_index(drop=True)
        missing = ids - set(p["projectID"])
        if missing:
            warns.append(f"{ctx} : {len(missing)} projet(s) présents dans organization.csv mais absents de "
                         f"project.csv (ex. {sorted(missing)[0]})")

        # 2) Livrables : lecture, dédoublonnage des lignes identiques, classification indicative
        if dlv.empty:
            d = pd.DataFrame({c: pd.Series(dtype=str) for c in ("projectID", "type", "title", "description", "url")})
        else:
            d = pd.DataFrame({
                "projectID": s(dlv, "projectID", warns, ctx).str.strip(),
                "type": s(dlv, ("deliverableType", "type"), warns, ctx).str.strip(),
                "title": s(dlv, "title", warns, ctx).str.strip(),
                "description": s(dlv, "description", warns, ctx),
                "url": s(dlv, "url", warns, ctx).str.strip(),
            })
        d = (d[d["projectID"].isin(ids)]
             .drop_duplicates(["projectID", "type", "title", "url"]).reset_index(drop=True))
        t = d["type"].replace("", "(vide)").value_counts().rename_axis("type").reset_index(name="n")
        t.insert(0, "programme", prog)
        type_parts.append(t)

        txt = d["type"] + " " + d["title"]
        if cfg["use_description"]:
            txt = txt + " " + d["description"]
        d["kind"] = pd.Series([classify(x, dmp_re, data_re) for x in txt], index=d.index, dtype="object")
        d["_key"] = (d["projectID"] + "|" + d["url"]).where(
            d["url"] != "", d["projectID"] + "|" + d["type"] + "|" + d["title"])
        kinds = d["kind"].astype(str)
        # Mode « all » : toutes les lignes sont conservées (un même fichier n'est téléchargé qu'une fois,
        # au téléchargement). Mode « regex » : seulement les livrables DMP/Données, dédoublonnés.
        hit = d.copy() if mode == "all" else _dedupe_hits(d[kinds != ""].copy())

        # 3) Couverture par projet
        cov = p.copy()
        series = (("n_livrables", d.groupby("projectID").size()),
                  ("n_cibles", hit.groupby("projectID").size()),
                  ("n_avec_url", hit[hit["url"] != ""].groupby("projectID").size()),
                  ("n_dmp", d[kinds.str.contains("DMP", regex=False)].groupby("projectID").size()),
                  ("n_donnees", d[kinds.str.contains("Données", regex=False)].groupby("projectID").size()))
        for name, ser in series:
            cov[name] = cov["projectID"].map(ser).fillna(0).astype(int)
        cov["couverture"] = [coverage_label(a, b, c, no_dlv)
                             for a, b, c in zip(cov["n_livrables"], cov["n_cibles"], cov["n_avec_url"])]
        cov_parts.append(cov)
        files.append({"programme": prog, "projets": len(cov), "livrables_dans_le_fichier": len(dlv),
                      "archive_livrables": dlv_origin})

        h = hit.drop(columns=["_key"]).merge(p, on="projectID", how="left")
        h["status"] = [ST_PENDING if u else ST_NO_URL for u in h["url"]]
        hit_parts.append(h)

    for pic in sorted(pics - seen):
        warns.append(f"PIC {pic} : introuvable dans organization.csv (programmes sélectionnés)")

    hits = _concat(hit_parts, HIT_COLS)
    if not hits.empty:
        hits = hits.sort_values(["programme", "projectID", "type", "title"], ignore_index=True)
    cov_all = _concat(cov_parts, COV_COLS)
    diag = pd.DataFrame(columns=["programme", "année_début", "projets", "avec_livrable", "part_%"])
    if not cov_all.empty:
        g = cov_all.assign(année_début=cov_all["start"].str[:4].replace("", "?")).groupby(
            ["programme", "année_début"]).agg(projets=("projectID", "size"),
                                              avec_livrable=("n_livrables", lambda x: int((x > 0).sum())))
        g["part_%"] = (100 * g["avec_livrable"] / g["projets"]).round(0).astype(int)
        diag = g.reset_index()
    return {
        "hits": hits, "cov": cov_all, "diag": diag,
        "types": _concat(type_parts, ["programme", "type", "n"]),
        "files": pd.DataFrame(files), "warnings": list(dict.fromkeys(warns)), "cfg": copy.deepcopy(cfg),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


# ───────────────────────── Sélection des livrables à télécharger ─────────────────────────
def select_rows(hits, types=None, text="", only_url=False, cats=None):
    """Filtre la table des livrables : types, mots (tous requis, sans accents), URL, catégorie indicative."""
    df = hits
    if types is not None:
        df = df[df["type"].isin(types)]
    terms = norm(text or "").lower().split()
    if terms:
        hay = (df["type"] + " " + df["title"] + " " + df["description"]).map(lambda x: norm(x).lower())
        df = df[hay.map(lambda x: all(w in x for w in terms))]
    if only_url:
        df = df[df["url"] != ""]
    if cats is not None:
        def keep(kind):
            parts = [x for x in kind.split("+") if x] or ["Autre"]
            return any(x in cats for x in parts)
        df = df[df["kind"].map(keep)]
    return df


def load_selection_csv(f):
    """Relit un CSV exporté par l'app (et filtré par vos soins, séparateur , ou ; détecté)."""
    df = pd.read_csv(f, sep=None, engine="python", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    for c in ("projectID", "url"):
        if c not in df.columns:
            raise ValueError(f"colonne « {c} » absente du CSV")
    for c in HIT_COLS:
        if c not in df.columns:
            df[c] = ""
    df["url"] = df["url"].str.strip()
    df["status"] = [ST_PENDING if u else ST_NO_URL for u in df["url"]]
    return df[HIT_COLS]


def to_excel_csv(df):
    """CSV pour Excel (séparateur ; et BOM UTF-8 : accents corrects en version française)."""
    return df.to_csv(index=False, sep=";").encode("utf-8-sig")


# ───────────────────────── Téléchargement ─────────────────────────
class _TooLarge(Exception):
    pass


class Throttle:
    """Garantit un intervalle minimal entre deux requêtes (toutes requêtes confondues)."""

    def __init__(self, delay):
        self.delay, self._last = max(float(delay), MIN_DELAY), float("-inf")

    def wait(self):
        gap = self.delay - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()


class Robots:
    def __init__(self, session, throttle):
        self.session, self.throttle, self._cache = session, throttle, {}

    def allowed(self, url):
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        rp = self._cache.get(base)
        if rp is None:
            lines = []
            try:
                self.throttle.wait()
                r = self.session.get(base + "/robots.txt", timeout=15)
                if r.status_code == 200:
                    lines = r.text.splitlines()
            except requests.RequestException:
                pass  # robots.txt injoignable : on considère l'accès autorisé
            rp = RobotFileParser()
            rp.parse(lines)
            self._cache[base] = rp
        return rp.can_fetch(UA["User-Agent"], url)


def safe(text, n=60):
    return re.sub(r"[^\w.-]+", "_", text)[:n].strip("_") or "x"


def _result(status, **kw):
    base = dict(status=status, detail="", http_status="", content_type="", final_url="",
                size_bytes=0, file="", sha256="")
    base.update(kw)
    return base


def _get(session, url, throttle, retries=2):
    err = None
    for attempt in range(retries + 1):
        throttle.wait()
        try:
            r = session.get(url, stream=True, timeout=(10, 60), allow_redirects=True)
            if r.status_code not in RETRY_CODES or attempt == retries:
                return r
            r.close()
        except requests.RequestException as e:
            err = e
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    raise err or requests.RequestException("échec après plusieurs tentatives")


def _looks_html(first):
    head = first[:2048].lower()
    return b"<!doctype html" in head or b"<html" in head


def _guess_ext(first, ctype, final_url, is_html):
    if first.startswith(b"%PDF"):
        return ".pdf"
    if is_html:
        return ".html"
    suffix = Path(urlparse(final_url).path).suffix.lower()
    return EXT.get(ctype) or (suffix if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix) else ".bin")


def _find_redirect(body, base_url):
    """URL de redirection JavaScript / meta refresh d'une page intermédiaire, ou None."""
    text = body.decode("utf-8", "ignore")
    m = META_REFRESH_RE.search(text) or JS_REDIRECT_RE.search(text)
    if not m:
        return None
    raw = next((g for g in m.groups() if g), "")
    raw = raw.replace("\\/", "/").replace("\\u0026", "&").replace("\\x26", "&")
    raw = htmllib.unescape(raw).strip()
    if not raw or raw.startswith(("#", "javascript:")):
        return None
    return urljoin(base_url, raw)


def _hop_allowed(nxt, origin_url, current_url):
    """On ne suit une redirection que vers http(s) du même hôte ou d'un sous-domaine europa.eu."""
    p = urlparse(nxt)
    host = (p.hostname or "").lower()
    ok_hosts = {(urlparse(origin_url).hostname or "").lower(), (urlparse(current_url).hostname or "").lower()}
    return p.scheme in ("http", "https") and bool(host) and (host in ok_hosts or host.endswith(".europa.eu"))


def _write(path, parts, max_bytes):
    h, size = hashlib.sha256(), 0
    with open(path, "wb") as f:
        for chunk in parts:
            size += len(chunk)
            if size > max_bytes:
                raise _TooLarge()
            f.write(chunk)
            h.update(chunk)
    return size, h.hexdigest()


def fetch_file(session, url, folder, stem, max_mb, throttle, robots=None):
    """Télécharge un livrable et retourne un dict de résultat (statut, hash, chemin, …).

    Gère la page intermédiaire du portail EC (cookie + redirection JS) : jusqu'à MAX_HOPS sauts."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return _result(ST_BAD_URL)
    if robots is not None and not robots.allowed(url):
        return _result(ST_ROBOTS)
    max_bytes = int(max_mb * 1e6)
    cur, hops, path, meta = url, 0, None, {}
    while True:
        try:
            r = _get(session, cur, throttle)
        except requests.RequestException as e:
            return _result(ST_NETWORK, detail=type(e).__name__, **meta)
        try:
            with r:
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                meta = dict(http_status=r.status_code, content_type=ctype, final_url=r.url)
                if r.status_code in (401, 403):
                    return _result(ST_RESTRICTED, detail=f"HTTP {r.status_code}", **meta)
                if r.status_code != 200:
                    return _result(ST_DEAD, detail=f"HTTP {r.status_code}", **meta)
                declared = r.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    return _result(ST_TOO_LARGE, detail=f"{int(declared) / 1e6:.1f} Mo annoncés", **meta)
                chunks = r.iter_content(1 << 16)
                first = next(chunks, b"")
                if not first:
                    return _result(ST_DEAD, detail="réponse vide", **meta)
                # Un PDF servi avec un mauvais Content-Type reste un PDF
                is_html = not first.startswith(b"%PDF") and (ctype in HTML_TYPES or _looks_html(first))
                via = f" (après {hops} redirection(s) JS)" if hops else ""
                if is_html:
                    body = b"".join(itertools.chain([first], itertools.islice(chunks, 3)))  # ≤ 256 Ko
                    nxt = _find_redirect(body, r.url) if hops < MAX_HOPS else None
                    if nxt and nxt != cur and _hop_allowed(nxt, url, r.url):
                        if LOGIN_RE.search(nxt):
                            return _result(ST_RESTRICTED, detail="redirection vers une page de connexion", **meta)
                        cur, hops = nxt, hops + 1
                        continue  # on quitte le with (réponse fermée) et on suit la redirection
                    if (r.history or hops) and LOGIN_RE.search(r.url):
                        return _result(ST_RESTRICTED, detail="redirection vers une page de connexion", **meta)
                    folder.mkdir(parents=True, exist_ok=True)
                    path = folder / f"{stem}.html"
                    size, sha = _write(path, [body], max_bytes)
                    return _result(ST_LANDING, detail="HTML reçu à la place du fichier" + via,
                                   size_bytes=size, file=str(path), sha256=sha, **meta)
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"{stem}{_guess_ext(first, ctype, r.url, False)}"
                size, sha = _write(path, itertools.chain([first], chunks), max_bytes)
                return _result(ST_OK, detail=via.strip(), size_bytes=size, file=str(path), sha256=sha, **meta)
        except _TooLarge:
            out = _result(ST_TOO_LARGE, detail=f"> {max_mb} Mo", **meta)
        except requests.RequestException as e:
            out = _result(ST_NETWORK, detail=type(e).__name__, **meta)
        except OSError as e:
            out = _result(ST_LOCAL, detail=str(e)[:120], **meta)
        if path is not None:
            path.unlink(missing_ok=True)
        return out


def download_all(df, cfg, progress=None, manifest=None):
    """Télécharge les livrables de df (≤ 1 requête/s). Un même (projectID, URL) n'est téléchargé qu'une fois.
    Le manifeste est écrit même en cas d'arrêt."""
    out = Path(cfg["outdir"])
    session = requests.Session()  # conserve les cookies de session du portail EC
    session.headers.update(UA)
    throttle = Throttle(cfg["delay"])
    robots = Robots(session, throttle) if cfg.get("respect_robots", True) else None
    rows, log, done = df.to_dict("records"), [], {}
    try:
        for i, r in enumerate(rows):
            res = _result(ST_NO_URL)
            if r["url"]:
                key = (r["projectID"], r["url"])
                if key in done:
                    res = {**done[key], "detail": "même URL qu'un livrable précédent (non retéléchargé)"}
                else:
                    res = fetch_file(session, r["url"], out / safe(f"{r['projectID']}_{r['acronym']}"),
                                     f"{i:03d}_" + safe(f"{r['kind'] or 'livrable'}_{r['title']}"),
                                     cfg["max_mb"], throttle, robots)
                    done[key] = res
            log.append({**r, **res, "collected_at": datetime.now().astimezone().isoformat(timespec="seconds")})
            if progress:
                progress((i + 1) / len(rows))
    finally:
        if manifest and log:
            Path(manifest).parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(log).reindex(columns=MANIFEST_COLS).to_csv(manifest, index=False)
    return pd.DataFrame(log).reindex(columns=MANIFEST_COLS)


# ───────────────────────── Rapport (format de sortie du prompt) ─────────────────────────
def summary_text(res, log=None):
    cfg, cov, hits = res["cfg"], res["cov"], res["hits"]
    kinds = hits["kind"].astype(str)
    n_ret, n_url = len(hits), int((hits["url"] != "").sum())
    pct = f"{100 * n_url / n_ret:.0f} %" if n_ret else "n/a"
    role = "coordinateur" if cfg["role"] == "coordinator" else "tous rôles"
    mode = "tous les livrables" if cfg.get("filter_mode", "all") == "all" else "filtre regex"
    avec = int((cov["n_livrables"] > 0).sum())
    lines = [
        f"{len(cov)} projet(s) ({role}) · programmes : {', '.join(cfg['programmes'])}.",
        f"{int(cov['n_livrables'].sum())} livrables publiés dans CORDIS ; {n_ret} retenus ({mode}), dont "
        f"DMP : {int(kinds.str.contains('DMP', regex=False).sum())} · "
        f"Données : {int(kinds.str.contains('Données', regex=False).sum())} (classification indicative).",
        f"Couverture : {avec}/{len(cov)} projets avec ≥ 1 livrable publié ; {n_url}/{n_ret} retenus avec URL ({pct}).",
    ]
    if log is not None and len(log):
        counts = Counter(log["status"])
        blocages = ", ".join(f"{v} × {k}" for k, v in counts.most_common()
                             if k not in (ST_OK, ST_NO_URL)) or "aucun"
        lines.append(f"Téléchargement : {counts.get(ST_OK, 0)}/{n_url} fichiers récupérés ; blocages : {blocages}.")
    else:
        lines.append("Téléchargement : non lancé.")
    lines.append(f"{len(res['warnings'])} avertissement(s) sur les données — voir « limites ».")
    return "\n".join(lines)


def build_report(res, log=None):
    cfg, cov, hits = res["cfg"], res["cov"], res["hits"]
    src = log if log is not None else hits
    limites = list(res["warnings"]) + [
        "Seuls les livrables publics publiés dans CORDIS figurent dans les CSV : un projet à 0 livrable est "
        "normal pour un projet récent ou, en H2020, antérieur à mai 2019 ; un DMP confidentiel est "
        "indiscernable d'un DMP absent (contacter le coordinateur scientifique du projet).",
        "Les livrables FP7 et certains jeux de données sont référencés dynamiquement via OpenAIRE sur les "
        "pages CORDIS : ils ne figurent pas dans les CSV.",
        "Classification DMP / Données par regex sur type + titre" + (" + description" if cfg["use_description"] else "")
        + (" : filtre appliqué." if cfg.get("filter_mode") == "regex" else " : indicative, aucun livrable écarté."),
        "Les CSV CORDIS sont produits mensuellement : écarts possibles avec le site.",
        "Chaque fichier peut avoir sa propre licence : ne pas redistribuer sans vérification.",
    ]
    return {
        "perimetre": {"pic": sorted(parse_pics(cfg["pics"])), "role": cfg["role"],
                      "programmes": cfg["programmes"]},
        "projets": [{"projectID": r.projectID, "acronym": r.acronym, "n_livrables": int(r.n_livrables),
                     "n_cibles": int(r.n_cibles), "n_avec_url": int(r.n_avec_url)}
                    for r in cov.itertuples()],
        "livrables": [{"projectID": r["projectID"], "kind": r["kind"], "title": r["title"], "url": r["url"],
                       "status": r["status"], "sha256": r.get("sha256", "") or "",
                       "date_collecte": r.get("collected_at", "") or "", "chemin": r.get("file", "") or ""}
                      for r in src.to_dict("records")],
        "limites": limites,
    }


# ───────────────────────── Prompt système ─────────────────────────
def render_prompt(cfg):
    regex_mode = cfg.get("filter_mode") == "regex"
    values = {
        "UNIVERSITE": cfg["university"] or "(à renseigner)",
        "PIC_LIST": ", ".join(sorted(parse_pics(cfg["pics"]))) or "(à renseigner)",
        "ROLE": "coordinateur uniquement" if cfg["role"] == "coordinator" else "tous rôles",
        "PROGRAMMES": ", ".join(cfg["programmes"]),
        "REGEX_DMP": cfg["regex_dmp"], "REGEX_DATA": cfg["regex_data"],
        "FILTRAGE": ("seuls les livrables correspondant aux regex ci-dessus sont retenus" if regex_mode else
                     "aucun filtre : tous les livrables publiés des projets retenus sont collectés ; "
                     "la classification DMP / Données (regex ci-dessus) est indicative et n'écarte rien"),
        "CHAMPS": "type + titre + description" if cfg["use_description"] else "type + titre (description non analysée)",
        "MAX_SIZE_MB": str(cfg["max_mb"]), "DELAY_S": f"{max(MIN_DELAY, float(cfg['delay'])):g}",
    }
    text = PROMPT_FILE.read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


# ───────────────────────── Interface Streamlit ─────────────────────────
def main():
    import streamlit as st

    def show(df):
        try:
            st.dataframe(df, width="stretch")
        except Exception:  # anciennes versions de Streamlit
            st.dataframe(df, use_container_width=True)

    st.set_page_config(page_title="CORDIS · Livrables", layout="wide")
    cfg = load_cfg()
    st.title("CORDIS · Collecte des livrables des projets")

    with st.sidebar:
        st.header("Configuration")
        cfg["university"] = st.text_input("Université / structure", cfg["university"])
        cfg["pics"] = st.text_area(
            "Identifiant(s) organisation (PIC)", cfg["pics"],
            help="Numéro PIC à 9 chiffres (champ organisationID de CORDIS). "
                 "Séparateurs : virgule, espace, retour ligne.")
        roles = ["coordinator", "any"]
        cfg["role"] = st.radio(
            "Rôle dans le projet", roles, index=roles.index(cfg["role"]),
            format_func=lambda r: "Coordinateur uniquement" if r == "coordinator" else "Tous rôles")
        cfg["programmes"] = st.multiselect(
            "Programmes", list(SOURCES), cfg["programmes"],
            help="FP7 : projets listés, mais CORDIS n'a pas de CSV de livrables pour ce programme.")

        modes = ["all", "regex"]
        cfg["filter_mode"] = st.radio(
            "Livrables retenus", modes, index=modes.index(cfg["filter_mode"]),
            format_func=lambda m: "Tous (je filtre ensuite)" if m == "all" else "Filtrer par regex DMP / données")
        with st.expander("Regex DMP / données"):
            st.caption("Sans casse ni accents (« donnees » trouve « données »). En mode « Tous », elles ne "
                       "servent qu'à la colonne indicative `kind`.")
            cfg["regex_dmp"] = st.text_input("Regex DMP", cfg["regex_dmp"])
            cfg["regex_data"] = st.text_input("Regex données de recherche", cfg["regex_data"])
            cfg["use_description"] = st.checkbox("Chercher aussi dans la description", cfg["use_description"])
        with st.expander("Téléchargement & cache"):
            cfg["outdir"] = st.text_input("Dossier de sortie", cfg["outdir"])
            cfg["max_mb"] = st.number_input("Taille max / fichier (Mo)", 1, 500, int(cfg["max_mb"]))
            cfg["delay"] = st.number_input("Pause entre requêtes (s, min. 1)", MIN_DELAY, 10.0,
                                           max(MIN_DELAY, float(cfg["delay"])), 0.5)
            cfg["respect_robots"] = st.checkbox("Respecter robots.txt", cfg["respect_robots"])
            cfg["cache_days"] = st.number_input("Validité du cache CORDIS (jours, 0 = recharger)",
                                                0, 90, int(cfg["cache_days"]))
        with st.expander("Sources CORDIS (URL ou chemin local .zip)"):
            st.caption("Les livrables sont une archive distincte de celle des projets. Si une URL échoue, copiez "
                       "l'URL de téléchargement CSV depuis la page data.europa.eu du programme.")
            for prog in SOURCES:
                cur = cfg["sources"][prog]
                st.markdown(f"**{prog}**")
                cur["projects"] = st.text_input("Archive projets / organisations", cur["projects"],
                                                key=f"src_p_{prog}")
                if prog not in NO_DELIVERABLES:
                    cur["deliverables"] = st.text_input("Archive des livrables", cur["deliverables"],
                                                        key=f"src_d_{prog}")
        if st.button("💾 Enregistrer la configuration"):
            save_cfg(cfg)
            st.success("config.json enregistré")

    tab1, tab2, tab3 = st.tabs(["1 · Extraction", "2 · Sélection & téléchargement", "3 · Prompt système"])

    with tab1:
        st.caption("Source : CORDIS Open Data (CSV). Seuls les livrables publics **publiés** dans CORDIS sont "
                   "visibles : zéro livrable est normal pour beaucoup de projets (voir le diagnostic).")
        if st.button("🔎 Lancer l'extraction", type="primary"):
            try:
                compile_regex(cfg)
            except (re.error, ValueError) as e:
                st.error(f"Expression régulière invalide : {e}")
                st.stop()
            if not parse_pics(cfg["pics"]) or not cfg["programmes"]:
                st.error("Renseignez au moins un PIC et un programme.")
                st.stop()
            with st.spinner("Téléchargement et lecture des fichiers CORDIS…"):
                try:
                    st.session_state["res"] = extract(cfg)
                    st.session_state.pop("log", None)
                except (requests.RequestException, zipfile.BadZipFile, OSError) as e:
                    st.error(f"Accès ou lecture CORDIS impossible : {e}")
                    st.stop()

        res = st.session_state.get("res")
        if res:
            hits, cov = res["hits"], res["cov"]
            if res["warnings"]:
                with st.expander(f"⚠️ {len(res['warnings'])} avertissement(s) sur les données", expanded=True):
                    for w in res["warnings"]:
                        st.markdown(f"- {w}")
            if cov.empty:
                st.warning("Aucun projet trouvé pour ces PIC / rôle / programmes.")
            else:
                a, b, c, d = st.columns(4)
                a.metric("Projets", len(cov))
                b.metric("avec ≥ 1 livrable publié", int((cov["n_livrables"] > 0).sum()))
                c.metric("Livrables retenus", len(hits))
                d.metric("avec URL", int((hits["url"] != "").sum()))
                st.text(summary_text(res))
                with st.expander("🔍 Diagnostic : pourquoi des projets à 0 livrable ?", expanded=True):
                    st.caption("« livrables_dans_le_fichier » = lignes lues dans projectDeliverables.csv (tous projets "
                               "confondus). À 0, le fichier n'a pas été lu : vérifiez l'archive des livrables. "
                               "Sinon les zéros sont réels : projets récents, ou antérieurs à mai 2019 pour H2020.")
                    show(res["files"])
                    show(res["diag"])
                st.subheader("Couverture par projet")
                show(cov)
                st.subheader("Livrables retenus")
                show(hits)
                with st.expander("Types de livrables rencontrés"):
                    show(res["types"])
                x, y = st.columns(2)
                x.download_button("Exporter les livrables (CSV pour Excel)", to_excel_csv(hits),
                                  "livrables_cordis.csv", "text/csv")
                y.download_button("Rapport JSON (avant téléchargement)",
                                  json.dumps(build_report(res), ensure_ascii=False, indent=2),
                                  "rapport_cordis.json", "application/json")

    with tab2:
        res = st.session_state.get("res")
        source = st.radio("Livrables à télécharger",
                          ["Résultat de l'extraction (filtres ci-dessous)", "CSV filtré que j'importe"],
                          horizontal=True)
        todo = None
        if source.startswith("Résultat"):
            if not res or res["hits"].empty:
                st.info("Lancez d'abord l'extraction (onglet 1).")
            else:
                hits = res["hits"]
                all_types = sorted(hits["type"].unique())
                types = st.multiselect("Types de livrable", all_types, all_types)
                cats = st.multiselect("Catégorie indicative", ["DMP", "Données", "Autre"], ["DMP", "Données", "Autre"])
                text = st.text_input("Mots dans type / titre / description (tous requis, sans accents)")
                only_url = st.checkbox("Seulement ceux avec URL", True)
                todo = select_rows(hits, types, text, only_url, cats)
        else:
            up = st.file_uploader("CSV (colonnes projectID et url au minimum ; export de l'onglet 1)", type=["csv"])
            if up is not None:
                try:
                    todo = load_selection_csv(up)
                except (ValueError, pd.errors.ParserError, pd.errors.EmptyDataError) as e:
                    st.error(f"CSV illisible : {e}")

        if todo is not None:
            n_url = int((todo["url"] != "").sum())
            lo = n_url * max(MIN_DELAY, float(cfg["delay"])) / 60
            st.write(f"{len(todo)} livrable(s) sélectionné(s), dont {n_url} avec URL — durée estimée "
                     f"{lo:.0f} à {2 * lo:.0f} min (1 à 2 requêtes par fichier).")
            st.caption("Aucun contournement d'authentification ou de paywall. Chaque fichier peut avoir sa "
                       "propre licence : ne pas le redistribuer sans vérification.")
            show(todo[["programme", "projectID", "acronym", "kind", "type", "title", "url"]])
            if st.button("⬇️ Télécharger les fichiers") and len(todo):
                bar = st.progress(0.0)
                manifest = Path(cfg["outdir"]) / "manifest.csv"
                st.session_state["log"] = download_all(todo, cfg, bar.progress, manifest)

        log = st.session_state.get("log")
        if log is not None and res:
            st.success(f"Terminé · manifeste : {Path(cfg['outdir']) / 'manifest.csv'}")
            st.text(summary_text(res, log))
            show(log["status"].value_counts().rename_axis("statut").reset_index(name="n"))
            show(log)
            x, y = st.columns(2)
            x.download_button("Manifeste (CSV pour Excel)", to_excel_csv(log), "manifest.csv", "text/csv")
            y.download_button("Rapport JSON", json.dumps(build_report(res, log), ensure_ascii=False, indent=2),
                              "rapport_cordis.json", "application/json")

    with tab3:
        if PROMPT_FILE.exists():
            prompt = render_prompt(cfg)
            st.text_area("Prompt système généré (reflète la configuration courante)", prompt, height=520)
            st.download_button("Télécharger (.md)", prompt, "system_prompt_cordis.md")
        else:
            st.error("system_prompt.md est introuvable à côté de cordis_app.py.")


if __name__ == "__main__":
    main()
