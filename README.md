# I Shoot Rock

A tiny arcade shooter that lives in one HTML file. Rocks fall, you shoot them, gravity wins eventually.

## Play

Open `index.html` in any modern browser — no build step, no dependencies, no server.

```
open index.html      # macOS
```

## Controls

| Action | Input |
| --- | --- |
| Aim | Move the mouse / drag on touch |
| Shoot | Left click, or `Space` |
| Pause / resume | `P` (auto-pauses when the window loses focus) |
| Restart | Click or `Space` on the title and game-over screens |

## Rules

- Three lives. A rock that reaches the ground costs one.
- Big rocks take 3 hits, medium 2, small 1. Each hit shows a health bar.
- Score: 30 / 20 / 10 points for small, medium, and large rocks.
- Spawn rate and fall speed ramp up over the first ~40 seconds.
- Your best score is saved to `localStorage`.

## Structure

- `index.html` — everything: markup, styles, and the canvas game loop.

## Notes

Rendering is plain Canvas 2D with a fixed `requestAnimationFrame` loop and delta-time updates, so the game speed is independent of display refresh rate.
