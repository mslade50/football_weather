# Vendored front-end libraries

No build step; these files are committed and served as static assets by the Worker.

| File | Package | Version | Source URL | Size |
|---|---|---|---|---|
| `maplibre-gl.js` | maplibre-gl | 4.7.1 | https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js | ~803 KB |
| `maplibre-gl.css` | maplibre-gl | 4.7.1 | https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css | ~66 KB |
| `uPlot.iife.min.js` | uplot | 1.6.31 | https://unpkg.com/uplot@1.6.31/dist/uPlot.iife.min.js | ~50 KB |
| `uPlot.min.css` | uplot | 1.6.31 | https://unpkg.com/uplot@1.6.31/dist/uPlot.min.css | ~2 KB |

Re-download (Git Bash):

```
cd site/web/vendor
curl -fsSL -o maplibre-gl.js  https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js
curl -fsSL -o maplibre-gl.css https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css
curl -fsSL -o uPlot.iife.min.js https://unpkg.com/uplot@1.6.31/dist/uPlot.iife.min.js
curl -fsSL -o uPlot.min.css   https://unpkg.com/uplot@1.6.31/dist/uPlot.min.css
```

Basemap tiles come from OpenFreeMap (`https://tiles.openfreemap.org/styles/liberty`), no key required.
`map.js` falls back to a blank dark background if the style fails to load, so markers still render.
