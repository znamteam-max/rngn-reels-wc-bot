# Football World Cup ticker — exact brief

## Source

Use this page as the main automatic source:

```text
https://www.sports.ru/football/tournament/fifa-world-cup/news/
```

Task:
- fetch the latest **20** news items from this page;
- dedupe by title/url;
- keep newest first;
- expose them via `/api/news/football-world-cup`;
- football ticker should render these 20 items in a loop;
- ticker should periodically refresh so new items appear without rebuilding the URL.

## Visual

Use provided background:

```text
public/assets/football-ticker-bg.png
```

Original image size: `1920×1080`.

vMix should open the ticker as a Browser Source at `1920×1080`.
The ticker text lives only in the lower black strip.

Font:

```text
PFDinTextCompPro-BoldItal
```

The font file is not embedded in this archive. Place it manually in:

```text
public/fonts/PFDinTextCompPro-BoldItal.ttf
```

## Text behavior

- Take 20 latest news.
- Join them with separators.
- Scroll from right to left.
- Text must fade into a gradient before the left logo/label area.
- After the full list leaves the visible area, the news loop starts again.
- Fresh news should appear after the next API refresh.

## Layout notes

The background already contains the logo and base plate. Do not redraw the logo in HTML.
Only render moving text over the existing lower strip.

Recommended CSS region:

```text
text viewport: left 260px, right 32px, bottom 20px, height 76px
left fade/mask: ~100px, so text disappears before the logo
font-size: 34-42px, tune after visual check
```

## Important

This is a separate ticker project. Do not touch:
- match overlay;
- Winline;
- odds-service;
- Listen Больше Telegram bot for match graphics.
