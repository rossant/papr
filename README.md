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
papr duhaime 1987
papr "Jenny 2006 abusive head trauma" -f pdf,md
papr 10.1542/peds.2006-1172 -f pdf,md,bib
papr refs.txt -f pdf,md
papr refs.bib -f pdf
papr article.pdf -f md,bib,csl
```

By default, files are written to `~/Downloads`.
An unquoted author/year query such as `papr duhaime 1987` is treated as one
reference. Quote each citation when passing several references in one command;
file inputs and DOI inputs remain separate.

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

To save a retrieved paper and PDF to Zotero, opt in per command:

```bash
papr REF --zotero
papr REF --zotero --collection Inbox
```

Zotero groups and collections are different: the SBS group library is library
ID `5593385`, while `Inbox` is an optional collection inside a selected
library. Set the group library as your personal default with
`papr config set zotero.group_id 5593385`. With a group selected, imports,
searches, and resolution use that group. A collection can then be selected
within it with `--collection` or `papr config set zotero.collection Inbox`;
leave the collection unset to use the group library without a collection.
Use `papr config set zotero.collection ''` to clear a previously configured
collection. The generic personal default has no group ID and uses My Library.
Papr does not create or fall back to another library if the configured group
is unavailable. `PAPR_ZOTERO_COLLECTION` sets the optional collection name.
Zotero 10 or later with its local API enabled is required. On the first
interactive save, Zotero asks for native write permission; choose **Always
Allow** to save authorization in a server-scoped file with owner-only access.
Use `--non-interactive` to avoid that prompt.

`papr zotero last` (also `papr zotero`) imports the exact PDF and metadata from
the latest successful recorded download. It does not guess from file
modification times in Downloads. If the export was moved, papr can recover a
byte-identical copy from the configured download directory or its content cache,
preserving the attachment filename. A changed file at the recorded path is
reported instead of silently replaced. Import an existing PDF with:

```bash
papr zotero add paper.pdf
papr zotero add paper.pdf --dry-run
papr zotero add paper.pdf --non-interactive --no-compress
```

`--collection` overrides the personal default; `--dry-run` previews the import
and, for a group target, shows its ID and name using read-only library
information. Zotero write authorization is server-wide. These details describe
the configured targeting behavior; a live SBS group import has not been
verified.
An existing parent item is reused by DOI, or by exact title, year, and authors
when a DOI match is unavailable. With incomplete metadata, an existing PDF must
have identical contents to prove the match; ambiguous imports stop before writes.
An already attached copy of the same PDF is skipped. In group libraries, an
existing local PDF remains authoritative: papr reuses it even when a newly
compressed copy has different bytes, rather than adding another attachment. PDFs are
stored as copies by default, and a failed Zotero import leaves the downloaded
PDF in place.

If the local API is unavailable, `papr` can fall back to a read-only SQLite
snapshot. Standard data directories are detected automatically:

```text
~/Zotero
~/Library/Application Support/Zotero
```

The SQLite fallback is read-only and uses SQLite's backup mechanism rather than
modifying `zotero.sqlite`. If Zotero holds the database lock, papr stops trying
after about one second and continues to other sources. Failed snapshots are
remembered for the rest of that command so batch items do not repeat the wait.
Enable the local API setting above to let papr search Zotero while it is open.

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

## Installation diagnostics

```bash
papr doctor
papr doctor --json
papr doctor --strict
```

The doctor checks the installation, configuration, directory access, PDF
backends and local source availability, including Zolit index freshness. It
does not create directories, modify libraries or call remote bibliographic/OCR
services. Saved credentials and browser profiles are checked for presence;
their contents are not printed and authentication is not tested. Optional
backends, unavailable local sources and untested remote connectivity produce
warnings. Required directory or installation failures return a non-zero exit
code; `--strict` also fails on warnings.

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

## PDF compression

PDF compression is enabled by default when exporting PDFs or saving them to
Zotero. Ghostscript targets 200 dpi for color and grayscale images and 600 dpi
for monochrome images. Only smaller, validated results are used; cached
optimized copies leave source PDFs untouched, and native extraction and OCR
continue to use the original. If Ghostscript is unavailable, papr tries
lossless compression with qpdf or pypdf. Encrypted or signed PDFs are kept
unchanged; forms and annotations use lossless compression.

Disable compression for one command with `--no-compress`, or set
`papr config set pdf.compress false`. Configure compression in your personal
config:

```toml
[pdf]
compress = true
dpi = 200
timeout = 120

[zotero]
group_id = 5593385 # SBS shared group library; optional personal setting
collection = "Inbox" # optional collection within the selected library
```

For standalone compression, use `papr compress`:

```bash
papr compress paper.pdf
papr compress paper1.pdf paper2.pdf -o compressed/
papr compress -i paper.pdf
papr compress paper.pdf --dpi 150 --overwrite
```

When compression reduces the file size, the default output is
`paper.compressed.pdf` beside the source. Otherwise, the original stays unchanged.
`-o` selects an output directory; `-i` / `--in-place` explicitly replaces the source, and
`--overwrite` allows replacing an existing destination. `--dpi` overrides the
image resolution. Multiple files are processed independently, with failures
reported while the batch continues; each result reports the size saved.

Ghostscript and qpdf are optional external commands (for example,
`brew install ghostscript`). Without them, pypdf provides a lossless fallback.
Compression status and size details are included in JSON batch reports; Zotero
import results are also included in those reports.

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
PAPR_ZOTERO_COLLECTION
PAPR_ZOLIT_REPO
PAPR_ZOLIT_DB
PAPR_ZOLIT_SBS_REPO
```

## Batch mode

```bash
papr refs.txt -f pdf,md --non-interactive --report report.json
```

Every executed export batch also saves a versioned manifest in papr's local
data directory (`batches/`). Use `--manifest batch.json` to choose its path.
The manifest records each input's selection or exclusion, article metadata and
source identifiers when available, compression outcome, output paths and SHA-256
checksums. These are private local records: they can contain local paths and
bibliographic metadata and should not be committed to a public repository.

Re-export the saved files offline, without resolving references or recompressing:

```bash
papr refs.txt --manifest batch.json --non-interactive
papr refs.txt --batch-name reading --non-interactive
papr batch list
papr batch list --json
papr batch summary reading
papr batch show batch.json
papr batch check batch.json
papr batch export batch.json -o ~/Downloads
papr batch export batch.json -o ~/Dropbox --dry-run
papr batch resume reading
papr next-refs.txt --exclude-delivered batch.json --manifest next-batch.json
```

`--batch-name` registers a friendly name alongside a stable batch ID. Batch
commands and `--exclude-delivered` accept a name, ID or manifest path. The local
catalog also tracks manifests saved outside the default data directory. Names
can be reused; ambiguous names must be replaced by an ID or explicit path.
`list` and `summary` verify artifact checksums and report available, missing,
changed, unreadable and incomplete items. Availability includes recoverable
cache copies; it does not prove bibliographic relevance or publication status.

`resume` retries saved exports offline and retains their output directory unless
`-o` overrides it. Successful files with identical contents are reused. Unresolved
references and missing or changed artifacts remain failures and return a
non-zero exit code; reference resolution must be rerun separately. Resume does
not retry Zotero imports. Export and resume create a new manifest unless
`--manifest` explicitly selects a path; `--name` names the result, and otherwise
the original name is retained. Use the new manifest's ID for the next retry if
retaining the name makes it ambiguous.

`--exclude-delivered` is repeatable and also works with `papr batch export`.
It matches successful PDF exports by all available DOI, PMID and library/item-key
aliases; titles alone do not prove identity. Local exports remain available if a
subsequent Zotero save failed. Sources are checked before re-export starts;
identical destination files are reused and different files receive a suffix
unless `--overwrite` is explicit. An interrupted copy records successful and
pending artifacts in the new manifest, which can be used to retry the export.

Exported artifacts are cached by their checksum under papr's local cache
directory (`artifacts/`), so relocation does not lose access to them. Removing
this cache frees space, but missing originals then need to be restored.
`--dry-run` does not copy files or record download history; a manifest is written
for a normal export preview only when `--manifest` or `--batch-name` is supplied
explicitly. Batch export and resume previews do not save a manifest.

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

Exact local DOI matches with an existing PDF avoid remote lookup. Use `--enrich`
on export, `resolve`, or `zotero add` to request remote metadata enrichment.
`--local-only` still prevents remote lookup, including with `--enrich`.

`papr sources --check` probes SQLite access and compares the Zolit source files
with the last successful synchronization; `--json` exposes the diagnostic states
and synchronization timestamp. An older index has unknown freshness, and an
absent item in that index does not establish absence from Zotero. Refresh it
explicitly with `zolit zotero sync`. Group-scoped resolution also needs a fresh
index carrying group provenance; legacy entries with unknown group membership
are excluded rather than assigned to the configured group.

Zotero, Zolit and structured SBS seeds preserve bibliographic document types in
CSL and supported BibTeX exports, including conference papers and books. A
journal-article type or DOI alone does not establish peer review, final
publication status, infant age or a demonstrated CVT/SDH relationship.
Use `--item-type article-journal` on export or `resolve` to restrict document
types; repeat it to allow several types. Unresolved local PDFs have type
`document`. Excluded document types are recorded in export manifests.

## Cache maintenance

```bash
papr cache status
papr cache status --json
papr cache prune --dry-run
papr cache prune
papr cache prune --dry-run --manifest old-external-batch.json
```

Status reports sizes for artifact, PDF, processor and recovered-file caches.
Pruning removes only orphaned artifact objects at least seven days old. It
protects all known manifests, including externally registered manifests and
pending exports, plus the latest recorded download. Older external manifests
that predate the catalog can be protected with repeatable `--manifest` options.
Missing or malformed protection records stop pruning. The PDF, processor and
recovered-file caches, browser profiles and exported user files are retained.

`--older-than DAYS` changes the grace period; `0` disables it. A dry run lists
eligible objects without creating files or directories. Pruning and exports
share a process lock so pruning cannot remove an artifact while an export is
publishing its manifest. Export operations are serialized, including their
retrieval and processing stages. A competing state writer waits up to five
seconds, then reports that Papr is busy; retry it after the current operation.

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
uv sync --locked --dev
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

`uv.lock` pins development and test dependencies. Library installations use the
compatible dependency ranges in `pyproject.toml`; Zolit and zolit-sbs remain
optional local integrations rather than runtime package dependencies. Update
the lock deliberately with `uv lock --upgrade`, then rerun the checks above.

CI tests Python 3.11 and 3.13 on Linux and macOS. Zolit's CI has a separate job
that checks its current checkout against public Papr main, using synthetic
indexes written by Zolit itself. Hosting that job in the private Zolit
repository avoids needing a credential for Papr to access it. Run those
contract checks locally with a sibling checkout:

```bash
uv pip install -e ../zolit
uv run --no-sync pytest -q -m integration tests/integration
uv sync --locked --dev
```

`--no-sync` retains the temporarily installed sibling package; the final sync
restores the ordinary Papr development environment. These tests do not read or
modify a real Zotero library. They are skipped when Zolit is not installed.
