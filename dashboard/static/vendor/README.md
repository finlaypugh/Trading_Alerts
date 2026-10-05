# Vendored files

Served from `/static/vendor/` so the page loads nothing off-site and the
`default-src 'self'` CSP stays as it is.

| File | Version | Source | License |
|---|---|---|---|
| `lightweight-charts.standalone.production.js` | 4.2.3 | `https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js` | Apache-2.0, TradingView, Inc. |

sha256 `c7dda807d662a95b3d257119ed315cec669e3bdf5aaece75c480a39307f23540`

The library's own attribution logo is turned off (`attributionLogo: false`)
because it injects an inline `<style>` the CSP blocks; the page credits
TradingView with a link under the chart instead.

To upgrade, download the new standalone build, update this table, and check
`app.js` against the release notes: v5 renamed `addCandlestickSeries` /
`addLineSeries` / `setMarkers`.
