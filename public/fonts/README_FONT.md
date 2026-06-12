# Font placeholder

Use font family: `PFDinTextCompPro-BoldItal`.

The font binary is **not included in this archive**. Put the provided file manually here when working in the repo:

```text
public/fonts/PFDinTextCompPro-BoldItal.ttf
```

CSS should reference it as:

```css
@font-face {
  font-family: 'PFDinTextCompPro-BoldItal';
  src: url('/assets/fonts/PFDinTextCompPro-BoldItal.ttf') format('truetype');
  font-weight: 700;
  font-style: italic;
  font-display: swap;
}
```
