# Opening animations

Custom "Opening" animations for the faceid-nim lock-screen pill, made
with the faceid-nim settings app (**Opening** page) and installed/stored
per user at `~/.config/faceid-nim/openings/`.

## Security model

Animations are **declarative only**. A `.faceopen` package is a zip that
holds `opening.json` (colours, an easing name, a duration, an optional
headline) plus up to one `logo.svg` / `logo.png`. The shell extension
interprets that with a fixed set of easing curves — an installed
animation can repaint the unlock pill, and nothing else. It can never
execute code in gnome-shell.

## Package format

`.faceopen` is a plain zip:

```
opening.json     { kind, id, name, description, text, bg, accent,
                   ease, fade_ms, scale, ring }
logo.svg         optional logo shown beside the headline
logo.png         ditto (only one of them is used)
```

`bg`/`accent` are `#rrggbb` hex (blank = keep the dark pill / default
tint). `ease` is one of `outCubic`, `outBack`, `outExpo`. `fade_ms`
clamps to 120–3000. `ring: true` paints a soft accent halo instead of
the logo. Nothing else is accepted.

## Making and sharing

1. Open the app → **Opening** → **Create an opening animation**.
2. Pick colours, headline, easing, duration, optional logo.
3. Press **Publish to GitHub** — the app exports a `.faceopen` and
   gives you ready-to-run `gh` commands (gist or release).
4. Others use the app's **Install from URL** with the raw link, or
   **Install from file** with the downloaded package.

## Adding your animation to this catalogue

Make a pull request to this repo adding:

- your package file to `openings/packages/<id>.faceopen`, and
- one entry to `openings/catalog.json` whose `url` is the raw link.

The app's **Browse community** page fetches `catalog.json` and installs
entries directly.