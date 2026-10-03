# papr

`papr` is a small command-line tool for scholarly papers. It resolves incomplete
references, finds the best available PDF, optionally uses institutional access,
and exports the result to PDF, Markdown, BibLaTeX/BibTeX, CSL-JSON, plain text,
or JSON.

The project stays deliberately small: **resolve → fetch → process → export**.

It works without a bibliography manager, but it can also use local **Zotero**,
**Zolit**, and **zolit-sbs** data as high-priority resolvers.

## Quick start

```bash
uv tool install git+https://github.com/rossant/papr.git

papr "Jenny 2006"
papr "Jenny 2006 abusive head trauma" -f pdf,md
papr 10.1542/peds.2006-1172 -f pdf,md,bib
papr refs.txt -f pdf,md
papr refs.bib -f pdf
papr article.pdf -f md,bib,csl
```

By default, files are written to `~/Downloads`.

## Local-first reference resolution

For an ordinary text query, `papr` now searches local sources first:

1. Zotero local library
2. Zolit local database
3. zolit-sbs structured seed publications
4. Crossref
5. OpenAlex
6. PubMed for explicit PMIDs

A short query such as:

```bash
papr "Jenny 2006"
```

can therefore resolve from your own library even though author + year alone would
be weak as an internet search.

If exactly one strong local author/year match exists, it is accepted directly.
If several equally plausible local items exist, `papr` keeps the query ambiguous
and asks you to choose interactively.

If the selected local record already has an attached PDF, that local PDF is used
before any OA, publisher, or UCL request.

The same applies to DOI input: when a DOI is present in Zotero or Zolit and has a
known local PDF, `papr` uses that PDF.

## Zotero integration

Preferred access is Zotero's local Web API at:

```text
http://127.0.0.1:23119/api
```

In Zotero, enable:

```text
Settings → Advanced → Allow other applications on this computer to communicate with Zotero
```

Read access needs no Zotero API key.

If the local API is unavailable, `papr` can fall back to a read-only SQLite
snapshot. Standard data directories are detected automatically:

```text
~/Zotero
~/Library/Application Support/Zotero
```

The SQLite fallback is read-only and uses SQLite's backup mechanism rather than
modifying `zotero.sqlite`.

## Zolit integration

`papr` understands the actual Zolit schema used by `rossant/zolit`:

```text
items
attachments
zotero_json
creators_json
doi
year
```

It reads `zolit.sqlite` read-only and can reuse attachment paths already imported
from Zotero.

Detection supports:

- an explicit `zolit_db`;
- an explicit `zolit_repo`;
- Zolit config files in its standard locations;
- the sibling `zolit` checkout inferred from the `zolit-sbs` installation
  symlink under `~/.zolit/domains/sbs`.

## zolit-sbs integration

`zolit-sbs` is not treated as a complete bibliography. Its
`domains/sbs/key-publications.md` is deliberately a seed guide.

`papr` uses entries that contain enough structured information (explicit title
plus year) as local seed candidates. If such a seed has no DOI or local PDF,
`papr` expands the external query using its known author, year, and title.

The domain is detected either from an explicit `zolit_sbs_repo` or the standard
installed path:

```text
~/.zolit/domains/sbs
```

## Inspect configured sources

```bash
papr sources
```

Example:

```text
✓ zotero       local API, 3842 top-level items
✓ zolit        /path/to/zolit/data.local/zolit.sqlite, 3842 items
✓ zolit-sbs    /path/to/zolit-sbs/domains/sbs, 2 structured seeds
✓ crossref     remote
✓ openalex     remote
✓ ucl          saved browser profile
✓ mistral      key configured
```

## Control local vs remote resolution

```bash
papr "Jenny 2006" --local-only
papr "Jenny 2006" --no-local
papr resolve "Jenny 2006" --local-only
```

The default is local first, then remote only when needed.

## Reference resolution

`papr` accepts DOI, PMID, free-form citations, `.txt` reference lists,
BibTeX/BibLaTeX, CSL-JSON, and local PDFs.

```bash
papr resolve "Jenny 2006"
papr resolve "Jenny 2006 abusive head trauma"
papr resolve 10.1542/peds.2006-1172 --json
```

Results from multiple sources are deduplicated by DOI or normalized title/year
and ranked using title, author, and year similarity.

Search results must meet the configured confidence threshold even when only one
candidate is found. A verified DOI or PMID match is accepted directly. Use
`--verbose` with retrieval or `papr resolve` to see resolver and download failures.

## PDF acquisition

The acquisition order is:

1. PDF already attached locally through Zotero/Zolit
2. cached PDF
3. open-access PDF URL returned by OpenAlex
4. Unpaywall, when an email is configured
5. PubMed Central, when a PMCID is known
6. publisher/DOI landing page and `citation_pdf_url`
7. UCL authenticated access, when configured

Downloaded PDFs are validated and cached under the platform cache directory.
Use `--refresh` to ignore the cache and `--oa-only` to prohibit institutional
fallback.

## UCL / EZproxy

Install the optional browser support:

```bash
uv tool install "papr[ucl] @ git+https://github.com/rossant/papr.git"
```

Authenticate once:

```bash
papr login ucl
```

`papr` opens Chrome/Chromium with a persistent profile. Complete UCL SSO/MFA in
the browser, return to the terminal, and press Enter. No UCL password is stored.

If Chrome/Chromium is unavailable:

```bash
playwright install chromium
```

Remove the saved browser profile with:

```bash
papr logout ucl
```

Some publisher pages require visible Chrome to render their PDF controls:

```bash
papr DOI --show-browser
```

## OCR and Markdown

```bash
papr paper.pdf -f md --processor mistral
papr paper.pdf -f md --processor native
```

The default backend is `auto`: use Mistral OCR when `MISTRAL_API_KEY` exists,
otherwise use native PDF text extraction through `pypdf`.

Set the Mistral key via:

```bash
export MISTRAL_API_KEY=...
```

or the platform config directory's `secrets.env`. OCR results are cached so the
same PDF is not processed twice.
Alternatively, put the raw key in the platform config directory's `mistral.key`,
or point `MISTRAL_API_KEY_FILE` to an existing key file. An explicit
`MISTRAL_API_KEY` takes precedence.

The processing cache distinguishes native extraction from Mistral OCR and its
configured model. Adding a Mistral key therefore allows `auto` to process a PDF
with OCR even if native text was previously cached.

## Output formats

```bash
papr REF -f pdf
papr REF -f pdf,md
papr REF -f pdf,md,bib,csl
```

Supported formats:

| name | output |
| --- | --- |
| `pdf` | original PDF |
| `md` | Markdown |
| `txt` | native PDF text |
| `bib` | BibLaTeX |
| `bibtex` | legacy BibTeX |
| `csl` | CSL-JSON |
| `json` | normalized papr metadata |

Metadata-only outputs do not require downloading the PDF.
Both CSL-JSON and papr's normalized JSON exports can be used as input again.

## Filenames

The default template is a small subset of Zotero's file-renaming syntax:

```text
{{ firstCreator suffix="_" }}{{ year suffix="_" }}{{ title truncate="100" }}
```

Available variables include `firstCreator`, `author`, `year`, `title`,
`journal`, `container-title`, `DOI`, `PMID`, `PMCID`, and
`citationKey`. Supported modifiers are `prefix`, `suffix`, and `truncate`.

Override it per command:

```bash
papr REF --filename '{{ year suffix="_" }}{{ firstCreator suffix="_" }}{{ title truncate="80" }}'
```

## Configuration

```bash
papr config init
papr config path
```

Example:

```toml
download_dir = "~/Downloads"
formats = ["pdf"]
email = "me@example.org"
# unpaywall_email = "me@example.org"
# openalex_api_key = "..."

[filename]
template = '{{ firstCreator suffix="_" }}{{ year suffix="_" }}{{ title truncate="100" }}'
max_length = 180
ascii = false
space = "_"

[processors.md]
backend = "auto"
model = "mistral-ocr-latest"

[matching]
auto_accept_score = 0.78
auto_accept_margin = 0.08
max_candidates = 5

[local]
enabled = true
zotero_api_url = "http://127.0.0.1:23119/api"
# zotero_data_dir = "~/Zotero"
# zolit_repo = "~/src/zolit"
# zolit_db = "~/src/zolit/data.local/zolit.sqlite"
# zolit_sbs_repo = "~/src/zolit-sbs"
```

Environment variables:

```text
PAPR_EMAIL
UNPAYWALL_EMAIL
OPENALEX_API_KEY
MISTRAL_API_KEY
MISTRAL_API_KEY_FILE
PAPR_ZOTERO_API_URL
PAPR_ZOTERO_DATA_DIR
PAPR_ZOLIT_REPO
PAPR_ZOLIT_DB
PAPR_ZOLIT_SBS_REPO
```

## Batch mode

```bash
papr refs.txt -f pdf,md --non-interactive --report report.json
```

In an interactive terminal, batch mode shows live progress on stderr with the
current resolve, fetch, process, or export step and its elapsed time. The bar tracks
completed papers; after two papers complete, it estimates remaining time. Remote
OCR shows a spinner; native extraction reports page counts. When stderr is not
a terminal, papr prints plaintext stage messages instead. Downloads show
received bytes and a progress bar when the response supplies a usable size.

Use `--quiet` to suppress progress and the final batch summary while keeping
results and errors. `--verbose` adds resolver and download diagnostics.

Each item in the JSON file written by `--report` includes `duration` in seconds,
per-stage `timings` in seconds, and `failed_stage` on errors. The terminal summary
shows successes, failures, cache hits, elapsed time, and the output directory.

The process exits non-zero if at least one item fails.
Unreadable or malformed input files and corrupt PDFs are recorded in the report;
other inputs continue processing. Local SQLite libraries are read once per
command and reused for subsequent references in that batch.

## Local PDFs

```bash
papr unknown.pdf -f md,bib,csl
papr unknown.pdf --rename
papr unknown.pdf --rename --dry-run
```

For local PDFs, papr looks for a DOI in extracted text, then PDF metadata, and
attempts bibliographic enrichment using metadata or the filename. The file's
creation date is not used as the publication year.
`--local-only` and `--no-local` also control this enrichment. `--dry-run` previews
the destination without renaming or replacing PDFs.

## Development

```bash
git clone https://github.com/rossant/papr.git
cd papr
uv sync --dev
uv run pytest
uv run ruff check .
uv build
```

If `papr` is installed as an editable uv tool, source edits are picked up
immediately, but dependency changes require reinstalling the tool environment:

```bash
uv tool install --force --editable '.[ucl]'
```

Omit `[ucl]` if you do not use institutional browser access.

CI tests Python 3.11 and 3.13 on Linux and macOS.
