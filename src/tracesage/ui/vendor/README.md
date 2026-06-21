# Vendored UI assets

Third-party static assets bundled with the tracesage UI so it works **fully
offline** — no CDN fetch at page load.

| File | Source | Version | License |
|---|---|---|---|
| `pico.min.css` | [PicoCSS](https://picocss.com) (`@picocss/pico`) | 2.1.1 | MIT |

To update PicoCSS, re-download the pinned version and bump the table:

```bash
curl -fsSL "https://unpkg.com/@picocss/pico@<VERSION>/css/pico.min.css" \
  -o src/tracesage/ui/vendor/pico.min.css
```

PicoCSS is MIT licensed (© 2019–2025 Pico CSS contributors); the license header
is preserved at the top of `pico.min.css`.
