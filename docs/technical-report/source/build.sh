#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p build
env LC_ALL=C LANG=C LC_CTYPE=C latexmk -pdf -interaction=nonstopmode \
    -halt-on-error -file-line-error -outdir=build main.tex
cp build/main.pdf build/proteus-tech-report.pdf
printf '%s\n' 'Built build/proteus-tech-report.pdf'
