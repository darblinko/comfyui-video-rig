# Products

Put product photos here (clean, well-lit, ideally on a plain background) and
name them by product, for example `shaker_bottle.png`. Everything in this
folder except this README is gitignored: product photos are usually someone
else's material.

`pasteups/` holds the rough composites and their masks fed to the
`flux_start_frame` workflow (`jobs/examples/start_frame.toml`). With ffmpeg,
which the rig already requires, and a 720x1280 persona still `scene.png`:

```bash
mkdir -p products/pasteups
# Scale the product to 200 px wide and paste it with its top-left corner at x=260, y=700.
ffmpeg -i scene.png -i products/shaker_bottle.png \
  -filter_complex "[1]scale=200:-2[p];[0][p]overlay=260:700" -frames:v 1 \
  products/pasteups/shaker_kitchen_pasteup.png
# Print the scaled product size (WxH) for the mask.
ffmpeg -i products/shaker_bottle.png -vf scale=200:-2 -f null - 2>&1 | grep -o '200x[0-9]*' | tail -1
# Mask of the same size: white is repainted, black (the product box) is kept.
# Replace 280 with the height printed above.
ffmpeg -i products/pasteups/shaker_kitchen_pasteup.png \
  -vf "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill,drawbox=x=260:y=700:w=200:h=280:color=black:t=fill" \
  -frames:v 1 products/pasteups/shaker_kitchen_mask.png
```

These commands were checked with ffmpeg 7.0.2 on generated test images; no
product run is recorded.
