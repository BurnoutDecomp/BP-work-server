# Fonts and artwork

The repository's MIT license applies to the framework code and documentation.
The assets below have separate ownership and licensing. Bundling them does not
relicense them under MIT.

## Anton

`assets/anton-latin.ttf` is the Anton font already self-hosted by
`b5-decomp-server`. Its SIL Open Font License is included verbatim as
`assets/Anton-OFL.txt` and accompanies the built distribution.

## Oswald

`assets/oswald-variable.ttf` is the unmodified Oswald variable font from
[Google Fonts](https://github.com/google/fonts/tree/main/ofl/oswald). The
developer dashboard uses Oswald for its controls and labels; the framework
self-hosts it as `B5 Oswald`. Its SIL Open Font License is included verbatim as
`assets/Oswald-OFL.txt`. The source URLs and SHA-256 hashes of both files are in
`assets/provenance.json`.

## Burnout Paradise artwork

All PNG files in `assets/`, along with `paradise-city.webp`, are original
Burnout Paradise UI artwork. They retain their original ownership and are not
covered by the framework's MIT license. No open-source license for this artwork
is asserted here.

These files were copied unchanged from `b5-decomp-server/web/assets/` and
`BP-work-server/bp_work_server/static/img/ui/`. The optimized Paradise City logo
is the existing `b5-decomp-server` WebP; no new raster edits were made here.
`assets/provenance.json` records the source path and SHA-256 of every imported
file, including the fonts and their licenses.

Both dashboards now load these files from this library instead of keeping copies.

The Paradise preset uses these textures. Applications can override
`--b5-art-banner`, `--b5-art-banner-level` and `--b5-art-white` (with absolute URLs), replace the font-family
tokens, and use their own artwork. No game artwork is required by the JavaScript
module. The gallery includes an asset browser for the shared paint, brush,
splatter, skyline, seal, sign, chevron and scribble images.

`assets/banner-red-level.svg` embeds the unmodified `banner-red.png` bytes in
an SVG transform that levels the brush before it is resized. It remains game
artwork under the same ownership as its source, outside the framework's MIT
license. The source SHA-256 is recorded in the SVG metadata. Regenerate this
wrapper with `npm run assets:paint` after changing its source or geometry.

`assets/icons.svg` and `assets/favicon.svg` (the B5 mark, first drawn for
`b5-decomp-server`) are framework-authored vector code and are covered by MIT.
