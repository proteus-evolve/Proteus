# The Proteus site

Four static pages plus a small Python backend. There is no build step or frontend framework:
open `web/static/index.html` in a browser and the site works (the visit badge is simply
unavailable when offline). The canonical site is <https://proteus-evolve.github.io/>. Its
deployment repository is
[`proteus-evolve/proteus-evolve.github.io`](https://github.com/proteus-evolve/proteus-evolve.github.io),
whose published page files mirror `web/static/`; repository-only metadata such as
`.nojekyll` and its README live only in the deployment repository. Treat `web/static/` as
the source of truth. The active DSH campaign feed is the one generated exception: the
sidecar publisher updates the deployment copy of `assets/dsh-audio-live.json` while the
run is active, while this repository retains the scheduled-state seed.

## Files

| Path | What it is |
|---|---|
| [`web/static/index.html`](static/index.html) | The landing page. Page-specific CSS and JavaScript remain inline in `<style>`/`<script>` blocks; shared tokens and components come from `site.css`. Everything below under "The landing page" lives here. |
| [`web/static/playground.html`](static/playground.html) | The public Evolving Lab: three curated episode-by-episode replays with an expandable action stream, measurements, setup, and source note for every episode. Hosted run creation is intentionally unavailable; the plus card points people to the repository. |
| [`web/static/run.html`](static/run.html) | Live tracker for one submitted run: polls the backend, draws the identity fabric (one cell per episode, coloured by the surface that grew most). |
| [`web/static/demo.html`](static/demo.html) | Specimen viewer for a single recorded trajectory. |
| [`web/static/assets/site.css`](static/assets/site.css) | The only shared stylesheet: design tokens, both themes, and the components every page uses (`.wrap`, `.btn`, `.micro`, `table`, `.fabric`, `header.site`, nav pills). |
| [`web/static/assets/theme.js`](static/assets/theme.js) | Theme toggle. Writes `data-theme` on `<html>` and remembers the choice. |
| [`web/static/assets/case-data.js`](static/assets/case-data.js) | `window.CASE` — the replayed trajectory the landing page animates. Real data from one fleet seed (control arm, 30 episodes), scrubbed. |
| [`web/static/assets/demo-data.js`](static/assets/demo-data.js) | The same shape, for `demo.html`. |
| [`web/static/assets/lab-data.js`](static/assets/lab-data.js) | Editorial metadata and one-sentence summaries for every curated episode shown in the public Evolving Lab. |
| [`web/static/assets/dsh-audio-live.json`](static/assets/dsh-audio-live.json) | Episode-level public feed for the DSH audio campaign; starts as `scheduled` and is replaced by the privacy-reduced exporter while the run is active. |
| [`web/static/assets/proteus-mark.svg`](static/assets/proteus-mark.svg) | Graph mark alone. |
| [`web/static/assets/proteus-logo.svg`](static/assets/proteus-logo.svg) | Graph mark + traced PROTEUS wordmark, both centred on a shared axis in a 621x734 box. |
| [`web/server.py`](server.py) | Reserved local/future Lab backend: a FIFO queue with a concurrency cap, a harness allowlist, per-run episode caps, and run/status JSON endpoints. The public Evolving Lab does not call it. Localhost / trusted network only — see its module docstring. |

The two `.png` files in `assets/` are the original raster logo and a legacy architecture
diagram. The vector `.svg` files replaced the logo PNG on every page; the PNG is kept only
as the source of record. The legacy architecture PNG is not embedded—the current,
code-aligned architecture diagram is Mermaid in the root README. The landing-page footer
loads its public visit count from `hits.sh`; that counter is the site's one intentional
runtime network dependency.

The landing page also polls the privacy-reduced DSH campaign feed. A red button stays in
the fixed navigation and another appears in the hero, with its label changing between
`soon`, `live N/12`, and `replay`. Both open the live experiment at
`playground.html#live-campaign`; the full episode data is loaded only by the Lab.

## Conventions

**Colour and type come from tokens in `site.css`.** `--bg --panel --ink --soft --dim
--rule` for the ground, `--anchor`/`--anchor-hi` for the single green chroma anchor, and
`--s-notes --s-tools --s-skills --s-instr` for surface identity — those four are data
channels shared with the fleet atlas, so they mean the same thing on every page and in
the paper's figures. Do not hardcode a hex value; add a token.

**Both themes are mandatory; light is the default.** Light is zine-paper, dark is atlas
night-print. The palette is defined on bare `:root` and repeated under
`:root[data-theme="dark"]` / `:root[data-theme="light"]`; `theme.js` installs light when
there is no remembered choice. Only redefine tokens in those blocks — never style a
component inside a theme-specific media query.

**Type.** Serif (`Iowan Old Style`/Palatino/Georgia) for prose, `SF Mono`/ui-monospace
for every label, caption, and number. `.micro` is the uppercase letterspaced mono label
used throughout. No webfonts: the pages must work with no network.

**Logos are inlined, never `<img>`.** An `<img>`-loaded SVG is an isolated document, so
`currentColor` resolves to black and the mark disappears in dark mode. Inline the SVG and
scope its internal `<style>` rules to `.mark` so they do not leak into the page.

## The landing page

One scroll-snapped narrative. `#stage` is a fixed full-viewport layer holding the
trajectory plot; the hero sits over it at low opacity, and an IntersectionObserver adds
`body.trace-front` when the trace section scrolls in, which brings the stage forward and
reveals its readouts.

- **World vs frame.** The trajectory has a fixed world geometry (`SP`, `PADX`, `VH`,
  `TOP`, `BOT`; x = episode, y = tool calls) that never changes. The SVG *frame* is
  separate and set by `setFrame()`: 1000x520 normally, 520x560 on a phone, because a
  landscape frame drawn 375px wide is a 195px sliver. Everything that maps world to
  screen reads `FW`/`FH`, so a new frame needs no other edits. Rotating re-frames.
- **Camera.** `cam`/`tgt` (x, y, scale) with `follow` and `over` modes, eased per frame;
  `applyCam()` writes one transform on `#tj-world` and redraws the gridlines in screen
  space. Dot radii and stroke widths are computed from world-units-per-CSS-pixel
  (`perPx`) so a dot renders the same size in any frame.
- **Player.** A virtual clock (`vt`) advanced by rAF against `speed`, with pause and
  drag-to-scrub. Episode index is derived from `vt`, so the trajectory, the dome's step
  feed, and the scrubber label all follow one source of truth. It starts at episode 15.
- **Dome.** Clicking a dot flies a ghost circle from the dot to a half-disc pinned to the
  left edge, in that dot's colour: inside is the episode replaying step by step, outside
  are the run's setup and measurements. Live episodes animate; finished ones do not.
- **Responsive.** Two blocks at the end of the `<style>`: `@media (max-width:760px)` for
  portrait phones and `@media (max-height:600px)` for anything short (a phone held
  sideways is 812px wide and misses the first). Both are commented with why each rule
  exists. Section heights use `svh`, not `vh`: `100vh` is the *large* viewport, so a
  `100vh` section is taller than what you can see while the address bar is out.

## Working on it

Serve the directory and open it — any static server will do:

```bash
python3 -m http.server 8000 --directory web/static
```

The backend is retained for local development and future hosted runs. The public
Evolving Lab is a static replay gallery and does not call it:

```bash
python3 web/server.py --max-concurrent 2
```

Before shipping a change, check it at 375x812 and 1440x900 in both themes, and confirm
`document.documentElement.scrollWidth` equals `innerWidth` at 320, 375, and 390 — a
page-level horizontal scroll is the failure mode this layout is most prone to.

## Known gaps

- `run.html` inserts arm labels with `innerHTML`, and `server.py` accepts HTML in
  review/record surface names: stored XSS if the backend is ever exposed. Escape at both
  ends before hosting this anywhere but localhost.
- Pages deploys `web/static/`, but no automated browser or visual regression test covers
  the web directory.
