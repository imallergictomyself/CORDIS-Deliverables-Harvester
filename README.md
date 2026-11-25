# CORDIS Deliverables Harvester

A small [Streamlit](https://streamlit.io) application that finds the EU-funded projects **coordinated (or joined) by your institution** in the [CORDIS](https://cordis.europa.eu) open data, lists their **public deliverables** (Data Management Plans, datasets, reports…), and downloads the files with a full audit trail (source URL, collection date, SHA-256).

It was built for research-data / open-science teams who need to know *which* projects published a DMP or research data, and to collect those documents politely and reproducibly.

> The user interface and some column names are in French; this documentation is in English.

## Contents

- [Features](#features)
- [Requirements and installation](#requirements-and-installation)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Data sources](#data-sources)
- [How it works](#how-it-works)
- [Outputs](#outputs)
- [Statuses](#statuses)
- [Reading the results: why do many projects have 0 deliverables?](#reading-the-results-why-do-many-projects-have-0-deliverables)
- [Download behaviour and politeness](#download-behaviour-and-politeness)
- [System prompt template](#system-prompt-template)
- [Using the code as a library](#using-the-code-as-a-library)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Licence and data reuse](#licence-and-data-reuse)
- [Acknowledgements](#acknowledgements)

## Features

- **Project resolution** by organisation identifier (PIC, the CORDIS `organisationID`) and role (coordinator only, or any role), across Horizon Europe, Horizon 2020 and FP7.
- **Deliverable collection** from the CORDIS deliverables CSV, with two modes:
  - *All deliverables* (default): everything published for your projects; you filter afterwards.
  - *Regex filter*: keep only deliverables matching configurable DMP / research-data patterns (case- and accent-insensitive).
- **Coverage report** per project, plus a **diagnostic** panel that tells a genuinely empty project from an archive that was not read.
- **Selection** by type, keywords, indicative category and "has URL", or **import a CSV** you filtered yourself (Excel-friendly `;` CSV supported).
- **Downloader** with a 1 request/second ceiling, `robots.txt` support, retries, size limit, de-duplication, and handling of the European Commission participant-portal redirect flow.
- **Audit trail**: a `manifest.csv` and a JSON report in a documented format.
- **System prompt generator**: renders a ready-to-use prompt for an LLM agent doing the same job, filled with your configuration.

## Requirements and installation

- Python 3.9 or later (developed and tested with 3.12)
- `streamlit`, `pandas`, `requests`

```bash
python -m pip install streamlit pandas requests
```

Use the **same interpreter** for `pip` and for running the app (hence `python -m pip`). Optionally, save this as `requirements.txt`:

```text
streamlit
pandas
requests
```

## Quick start

Keep `cordis_app.py` and `system_prompt.md` in the same folder, then:

```bash
python -m streamlit run cordis_app.py
```

The app opens in your browser (usually <http://localhost:8501>).

1. **Sidebar**: enter your institution name and your **PIC number(s)** (9 digits; separators: comma, space or newline). Choose the role and the programmes. Click **Enregistrer la configuration** to save a `config.json`.
2. **Tab 1 – Extraction**: click **Lancer l'extraction**. The first run downloads the CORDIS archives (cached afterwards). Read the **warnings** and the **Diagnostic** panel first.
3. **Tab 2 – Sélection & téléchargement**: filter the deliverables (or import a CSV), then click **Télécharger les fichiers**.
4. Export the **manifest** and the **JSON report** from the same tab.
5. **Tab 3 – Prompt système**: copy or download the generated prompt if you also use an LLM agent.

> **Tip.** Do a first run with a single programme and a handful of projects, and check `downloads/manifest.csv` before launching a large collection.

## Configuration

Everything can be set in the sidebar and saved to `config.json` (next to `cordis_app.py`).

| Key | Default | Description |
|---|---|---|
| `university` | `""` | Institution name (used in the generated prompt). |
| `pics` | `""` | PIC number(s) of the organisation (CORDIS `organisationID`). |
| `role` | `"coordinator"` | `"coordinator"` or `"any"`. If the `role` column cannot be found, the programme is **skipped** with a warning (the role cannot be verified). |
| `programmes` | `["Horizon Europe", "Horizon 2020", "FP7"]` | Programmes to scan. |
| `filter_mode` | `"all"` | `"all"` keeps every published deliverable; `"regex"` keeps only DMP / data matches. |
| `regex_dmp` | `data management plan\|\bdmp\b\|plan de gestion de donnees` | DMP pattern. |
| `regex_data` | `data ?set\|research data\|open research data\|donnees de la recherche` | Research-data pattern. |
| `use_description` | `false` | Also match against the deliverable description (default: type + title). |
| `cache_days` | `7` | Validity of the cached CORDIS archives (`0` = always reload). |
| `outdir` | `"downloads"` | Output folder. |
| `max_mb` | `50` | Maximum size per downloaded file. |
| `delay` | `1.0` | Seconds between requests. **Minimum 1.0** (enforced). |
| `respect_robots` | `true` | Honour `robots.txt` before downloading. |
| `sources` | see below | Per programme: `projects` and `deliverables` archive (URL or local `.zip` path). |

Regular expressions are applied **without regard to case or accents**: both the text and the pattern are normalised (accents stripped), so `donnees` matches `données`.

## Data sources

The app uses CORDIS bulk downloads published on [data.europa.eu](https://data.europa.eu). **Deliverables are a separate archive from the projects archive** — reading only the projects archive yields zero deliverables for every project.

| Programme | Projects archive | Deliverables archive |
|---|---|---|
| Horizon Europe | `https://cordis.europa.eu/data/cordis-HORIZONprojects-csv.zip` | `https://cordis.europa.eu/data/cordis-HORIZONprojectDeliverables-csv.zip` |
| Horizon 2020 | `https://cordis.europa.eu/data/cordis-h2020projects-csv.zip` | `https://cordis.europa.eu/data/cordis-h2020projectDeliverables-csv.zip` ⚠️ |
| FP7 | `https://cordis.europa.eu/data/cordis-fp7projects-csv.zip` | none (see below) |

⚠️ The Horizon 2020 deliverables URL follows the naming scheme of the other archives but has **not been verified**. If the app reports that the deliverables archive is unreachable, open the dataset page, copy the CSV download URL and paste it in **Sources CORDIS** (a local `.zip` path also works):

- Horizon 2020: <https://data.europa.eu/data/datasets/cordish2020projects>
- Horizon Europe: <https://data.europa.eu/data/datasets/cordis-eu-research-projects-under-horizon-europe-2021-2027>

**FP7 has no deliverables CSV.** CORDIS retrieves FP7 deliverables, and some dataset/software results, dynamically from OpenAIRE. FP7 projects are listed in the coverage table (labelled accordingly) but their deliverables are not collected.

Files read from the archives: `organization.csv`, `project.csv`, `projectDeliverables.csv`. Columns are detected **by name** (case-insensitive); any missing column is reported in the warnings. CORDIS datasets are refreshed monthly, so small differences with the live website are expected.

## How it works

1. **Resolve projects.** Rows of `organization.csv` whose `organisationID` is one of your PICs (and, if requested, whose `role` is `coordinator`) give the project IDs; `project.csv` supplies acronym, title and dates. PICs that match nothing are reported.
2. **Read deliverables.** `projectDeliverables.csv` is searched in the deliverables archive first, then in the projects archive. Strictly identical rows (`projectID`, type, title, URL) are removed.
3. **Classify.** Each deliverable gets an indicative `kind`: `DMP`, `Données`, both (`DMP+Données`) or empty. In *regex* mode only classified rows are kept (de-duplicated by `projectID` + URL, or `projectID` + type + title when there is no URL); in *all* mode nothing is dropped.
4. **Report coverage.** Per project: number of published deliverables, retained deliverables, retained deliverables with a URL, DMP and data counts.
5. **Download.** Only rows with an `http(s)` URL are fetched, once per (`projectID`, URL), and every outcome is logged with a [status](#statuses).

## Outputs

```text
cordis_app.py
system_prompt.md
config.json                       # saved configuration
cache/                            # cached CORDIS archives (keyed by URL)
downloads/
├── manifest.csv                  # one row per attempted deliverable
└── <projectID>_<acronym>/
    └── 000_<kind>_<title>.<ext>  # downloaded files
```

### `manifest.csv`

`programme, projectID, acronym, kind, type, title, url, status, detail, http_status, content_type, final_url, size_bytes, file, sha256, collected_at`

The manifest is written even if a download run is interrupted. Timestamps are ISO 8601 with time zone.

### JSON report

Exported from the UI (before or after downloading):

```json
{
  "perimetre": {"pic": ["999999999"], "role": "coordinator", "programmes": ["Horizon Europe"]},
  "projets": [{"projectID": "", "acronym": "", "n_livrables": 0, "n_cibles": 0, "n_avec_url": 0}],
  "livrables": [{"projectID": "", "kind": "DMP|Données", "title": "", "url": "",
                 "status": "", "sha256": "", "date_collecte": "", "chemin": ""}],
  "limites": ["..."]
}
```

- `n_cibles` is the number of **retained** deliverables of the project (all published deliverables in *all* mode).
- `kind` may be `DMP`, `Données`, `DMP+Données` or empty.
- `limites` lists data warnings (missing columns, unreachable archives, unknown PICs…) plus the standing caveats.

The UI also shows a 5-line summary (volumes, coverage, main blockers).

## Statuses

| Status | Meaning |
|---|---|
| `à télécharger` | Retained and has a URL (before download). |
| `publié sans URL` | Published in CORDIS without a link. |
| `fichier récupéré` | File downloaded (a PDF served with a wrong `Content-Type` is still a PDF). |
| `page HTML d'atterrissage` | An HTML page was returned instead of a file, after following the allowed redirects. |
| `lien mort` | HTTP status other than 200 (and not 401/403), or an empty response; the code is in `detail`. |
| `URL invalide` | Not an `http(s)` URL. |
| `accès restreint (non contourné)` | HTTP 401/403, or redirect to a login page (EU Login, SSO…). Never bypassed. |
| `fichier trop volumineux` | Larger than `max_mb` (declared or measured while streaming). |
| `erreur réseau / timeout` | Connection error or timeout after retries. |
| `bloqué par robots.txt` | Disallowed by the host's `robots.txt` (when enabled). |
| `erreur locale d'écriture` | The file could not be written locally. |

At project level, the coverage column uses: `non retrouvé dans CORDIS (aucun livrable publié)`, `livrables publiés, aucun retenu par le filtre`, `retenus publiés sans URL`, `retenus avec URL`, and `livrables non disponibles en CSV pour ce programme (OpenAIRE)` for FP7.

## Reading the results: why do many projects have 0 deliverables?

A project at zero is often **legitimate**:

- CORDIS only contains **public** deliverables that projects chose to publish. A confidential DMP is indistinguishable from a DMP never submitted — contact the project's scientific coordinator.
- Recent and ongoing projects have published little yet.
- For Horizon 2020, deliverable metadata and links were only included **since May 2019**; earlier projects have none.
- FP7 has no deliverables CSV.
- As an order of magnitude, a third-party analysis of a 2026 Horizon Europe snapshot ([arthapraha/cordis-sdg](https://github.com/arthapraha/cordis-sdg)) found that roughly half of the projects (about 11,300 of 23,500) had at least one public deliverable. Treat this as indicative only.

To tell a real zero from a configuration problem, open **Diagnostic** in tab 1:

- `livrables_dans_le_fichier` = rows read from `projectDeliverables.csv` across **all** projects. If it is `0`, the deliverables archive was not read: check the archive URL and the warnings.
- The per-year table shows, for each programme and start year, how many projects have at least one published deliverable.

## Download behaviour and politeness

- At most **1 request per second**, all requests combined (including `robots.txt` and redirect hops). The minimum cannot be lowered in the UI.
- Descriptive `User-Agent`; `robots.txt` honoured by default (can be disabled in the sidebar — your decision and responsibility).
- Up to 2 retries with back-off on HTTP 429/5xx and timeouts.
- Size limit checked on the `Content-Length` header and while streaming; partial files are deleted.
- One download per (`projectID`, URL); duplicates reuse the first result.
- **Participant-portal links.** Deliverable URLs of the form `…/participants/documents/downloadPublic?documentIds=…` do not serve the file directly: they return an intermediate HTML page that sets a session cookie and redirects (JavaScript `location` or `<meta refresh>`) to a one-time URL. The downloader keeps the session cookies and follows up to **2** such redirects, only to the same host or a `*.europa.eu` host. A redirect to a login page is reported as `accès restreint` and never followed.
- No authentication or paywall is circumvented, and no personal data (contacts, e-mails) is collected beyond project metadata.
- Estimated duration: roughly 1 to 2 requests per file at 1 request/second.

## System prompt template

`system_prompt.md` is a template for an LLM agent that performs the same task. Tab 3 fills it with the current configuration. Placeholders:

| Placeholder | Filled with |
|---|---|
| `{{UNIVERSITE}}` | Institution name |
| `{{PIC_LIST}}` | PIC number(s) |
| `{{ROLE}}` | Role description |
| `{{PROGRAMMES}}` | Selected programmes |
| `{{FILTRAGE}}` | Collect-all vs regex-filter wording |
| `{{REGEX_DMP}}`, `{{REGEX_DATA}}` | The two patterns |
| `{{CHAMPS}}` | Fields analysed (type + title [+ description]) |
| `{{MAX_SIZE_MB}}`, `{{DELAY_S}}` | Size limit and request spacing |

The prompt defines the perimeter, allowed sources, procedure, exact status values, safety rules (no invented URLs, treat downloaded content as data, never as instructions) and the JSON output format.

## Using the code as a library

The logic does not depend on Streamlit; Streamlit is imported only inside `main()`.

```python
import cordis_app as ca

cfg = ca.load_cfg()
cfg.update(pics="999999999", programmes=["Horizon Europe"], filter_mode="all")

res = ca.extract(cfg)                      # dict: hits, cov, diag, files, types, warnings, cfg
print(ca.summary_text(res))

todo = ca.select_rows(res["hits"], text="data management plan", only_url=True)
log = ca.download_all(todo, cfg, manifest="downloads/manifest.csv")

report = ca.build_report(res, log)         # dict in the JSON format above
```

Main functions: `extract`, `select_rows`, `load_selection_csv`, `download_all`, `fetch_file`, `build_report`, `summary_text`, `render_prompt`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No module named streamlit` | Install with the interpreter you run: `python -m pip install streamlit pandas requests`, then `python -m streamlit run cordis_app.py`. On Windows, try `py -m …` if `python` is not the right one. |
| Every project shows 0 deliverables | Open **Diagnostic**. If `livrables_dans_le_fichier` is 0, fix the deliverables archive URL (see [Data sources](#data-sources)). |
| "archive des livrables inaccessible" | The URL is wrong or unreachable. Paste the exact CSV download URL from data.europa.eu, or a local `.zip` path. |
| "PIC … introuvable dans organization.csv" | Check the 9-digit PIC and the selected programmes. |
| "colonne role introuvable … programme ignoré" | The archive schema differs from expectations; switch the role to *Tous rôles* or inspect the CSV headers. |
| Many `bloqué par robots.txt` | The host disallows the path; disable the option only if that is acceptable for your use. |
| Many `page HTML d'atterrissage` | The link leads to a web page (project site, Zenodo record…) rather than a file; open the saved `.html` or the URL manually. |
| Accents garbled in Excel | Use the app's CSV export (`;` separator, UTF-8 with BOM). |
| `st.dataframe` width error on very old/new Streamlit | The app tries `width="stretch"` and falls back to `use_container_width=True`; upgrade Streamlit if both fail. |

## Known limitations

- Only deliverables **published in CORDIS** are visible; confidential or unreported ones cannot be found.
- FP7 deliverables and OpenAIRE-linked datasets/software are not in the CSV and are not collected.
- The Horizon 2020 deliverables archive URL is unverified (see [Data sources](#data-sources)).
- The participant-portal redirect handling follows the behaviour described by other projects (cookie + JavaScript redirect to a tokenised URL) and was tested against a local simulation, not against the live portal. Validate on a small sample first, and report portal changes by opening an issue.
- DMP / data classification is regex-based on type and title (optionally description): expect false positives and negatives. The *Types de livrables rencontrés* table helps tune the patterns.
- CORDIS archives are monthly dumps and can differ from the live website.
- Each downloaded file may carry its own licence.

## Licence and data reuse

- **Code**: add your chosen licence here (for example MIT) and a `LICENSE` file.
- **Data**: CORDIS content is © European Union. Reuse is governed by the European Commission reuse notice shown on each dataset page on data.europa.eu — check it for the datasets you use.
- **Downloaded deliverables**: authored by the projects; do not redistribute without checking each document's licence.

## Acknowledgements

- [CORDIS](https://cordis.europa.eu) and the Publications Office of the EU for the open data.
- [MaastrichtU-IDS/cordis-dmp-analysis](https://github.com/MaastrichtU-IDS/cordis-dmp-analysis) documents the participant-portal download mechanics that this tool also handles.
