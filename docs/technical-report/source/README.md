# Proteus technical report source

**Proteus: A Modular Framework for Agent Self-Improvement**  
The Proteus Authors · November 2026

This source builds the repository technical report. It retains the authors' ICLR
template typography without claiming conference submission or acceptance.

## Build the PDF

Install a TeX distribution with `latexmk`, pdfLaTeX, BibTeX, Times fonts, natbib,
TikZ, microtype, booktabs, tabularx, enumitem, pifont, fancyhdr, listings and xurl.
Then run from this directory:

```sh
sh build.sh
```

The output is `build/proteus-tech-report.pdf`. The supplied vector figure PDFs and
generated tables make this an offline build: no Python, model, Docker, or network
access is needed. PDF metadata such as creation time may differ between builds.

## Edit figures and inspect evidence

The two figures have editable SVGs and a Matplotlib source in
`scripts/generate_figures.py`. Run that script with Python and Matplotlib if you
want to regenerate the figures; this is optional for compiling the report.

`evidence/table-cells.json` maps all 73 numeric table values to frozen source
JSON pointers. `evidence/source-manifest.json` records original file hashes and
commit-pinned links. Given a Proteus checkout containing that Git history:

```sh
python3 scripts/verify_evidence.py --repo /path/to/Proteus
```

This command reads Git objects and performs no experiment or network request.
It verifies source hashes and the numeric mappings, and checks the 26 coverage
symbols against `evidence/coverage-audit.json`. That audit records the inspected
source revisions, source-file hashes, definitions and limits of Table 1. A source
audit is not a competing-framework execution or a performance comparison.

The documentation snapshot is `b828a85a1fcfebedc738ba02bee19d0314b0ea04`.
Historical studies, later pilots, and engineering checks keep their own recorded
source identities. Editorial integration into the repository does not turn those
results into experiments on a newer commit or qualify a future release tag.

The public bundle excludes superseded manuscripts, private editorial notes and
local machine paths. The original draft and full review archive remain with the
authors. See `THIRD_PARTY_NOTICES.md` for the retained template attribution.
