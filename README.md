# papr

`papr` is a small command-line tool for scholarly papers. It resolves incomplete
references, finds the best available PDF, optionally uses institutional access,
and exports the result to PDF, Markdown, BibLaTeX/BibTeX, CSL-JSON, plain text,
or JSON.

The project is intentionally modular but small: **resolve → fetch → process → export**.
It does not depend on Zotero or a local bibliography database.

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

## Reference resolution

`papr` accepts DOI, PMID, free-form citations, `.txt` reference lists,
BibTeX/BibLaTeX, CSL-JSON, and local PDFs.

Resolution uses Crossref and OpenAlex, with PubMed support for explicit PMIDs.
Results from multiple sources are merged by DOI and ranked using title, author,
and year similarity.

```bash
papr resolve "Jenny 2006 abusive head trauma"
papr resolve 10.1542/peds.2006-1172 --json
```

Ambiguous interactive queries present a choice. In batch/non-interactive mode,
ambiguous references fail rather than silently selecting a weak match.

## PDF acquisition

The acquisition order is:

1. Open-access PDF URL returned by OpenAlex
2. Unpaywall, when an email is configured
3. PubMed Central, when a PMCID is known
4. publisher/DOI landing page and `citation_pdf_url`
5. UCL authenticated access, when configured

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
```

Environment variables: `PAPR_EMAIL`, `UNPAYWALL_EMAIL`,
`OPENALEX_API_KEY`, and `MISTRAL_API_KEY`.

## Batch mode

```bash
papr refs.txt -f pdf,md --non-interactive --report report.json
```

The process exits non-zero if at least one item fails.

## Local PDFs

```bash
papr unknown.pdf -f md,bib,csl
papr unknown.pdf --rename
```

For local PDFs, papr looks for a DOI in extracted text, then PDF metadata, and
attempts bibliographic enrichment.

## Development

```bash
git clone https://github.com/rossant/papr.git
cd papr
uv sync --dev
uv run pytest
uv run ruff check .
```

CI tests Python 3.11 and 3.13 on Linux and macOS.
