# Visual spec — football ticker

Canvas: `1920×1080`.
Background image: `public/assets/football-ticker-bg.png`.

The background already includes:
- black full-frame base;
- lower rounded ticker plate;
- white outline;
- orange `ЦЕНТРАЛ ПАРК` label;
- small icon above label.

HTML should only add moving text.

Recommended text layer:

```css
.news-viewport {
  position: absolute;
  left: 260px;
  right: 32px;
  bottom: 20px;
  height: 76px;
  overflow: hidden;
  display: flex;
  align-items: center;
  -webkit-mask-image: linear-gradient(90deg, transparent 0, #000 100px, #000 100%);
  mask-image: linear-gradient(90deg, transparent 0, #000 100px, #000 100%);
}
```

Font:

```css
font-family: 'PFDinTextCompPro-BoldItal', 'Arial Narrow', Arial, sans-serif;
font-weight: 700;
font-style: italic;
font-size: 40px;
letter-spacing: .02em;
text-transform: uppercase;
```

Tune exact `left`, `bottom`, `height`, and `font-size` against vMix preview.
