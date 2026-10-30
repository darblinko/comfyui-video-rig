# Workflows

ComfyUI workflow templates in **API format** (the JSON you get from
`Workflow > Export (API)` in the ComfyUI web UI). `runner/run_job.py` loads a
template, injects job values into tagged nodes, and submits it.

## Validate before burning GPU time

The JSON files here are hand-written starters. ComfyUI node and field names
drift between versions, so on first run:

1. Open the tunnel (`python3 launch_rig.py tunnel`) and load http://localhost:8188.
2. Open `Workflow > Browse Templates` and load the official template for the
   model (Wan 2.2 image-to-video, HunyuanVideo, Flux).
3. Compare it against the starter here. If they differ, fix the starter or
   replace it entirely: tag the nodes as below, then `Export (API)` over the file.

## RIG_* title convention

`run_job.py` finds nodes by their title (`_meta.title`) and sets these fields
when present:

| Title | Node kind | Injected |
|---|---|---|
| `RIG_POSITIVE` | CLIPTextEncode | `text` = job `prompt` |
| `RIG_NEGATIVE` | CLIPTextEncode | `text` = job `negative_prompt` |
| `RIG_SAMPLER`, `RIG_SAMPLER_2`, ... | KSampler / KSamplerAdvanced | `seed` / `noise_seed` = job `seed` (any title starting with `RIG_SAMPLER`) |
| `RIG_START_IMAGE` | LoadImage | `image` = uploaded job `start_image` |
| `RIG_MASK` | LoadImageMask | `image` = uploaded job `mask_image` |
| `RIG_LATENT` | latent/video-size node | `width`/`height` = job `resolution`; `length`/`frames`/`num_frames` from `duration_seconds` x model fps (`length` rounded to 4k+1 for Wan) |
| `RIG_VIDEO` | CreateVideo | `fps` = model native fps |
| `RIG_OUTPUT` | SaveImage / SaveVideo | nothing injected; marks the output node |

"Model fps" is the native fps of the model (16 for Wan 2.2, 24 for
HunyuanVideo 1.5). A job that names a workflow file directly
(`workflow = "workflows/x.json"`) uses the job's `fps` if set, else the native
fps of the model whose workflow has that file name; a video workflow with
neither is rejected before anything is queued. An unknown model name or a
missing workflow file also stops the job before anything is queued.

Untagged nodes pass through untouched, so any exported workflow works as long
as the relevant nodes carry these titles. Top-level JSON keys starting with
`_` (like `_note`) are stripped before submission.

## Files

- `flux_persona_ref.json`: persona reference stills for a synthetic presenter persona
- `flux_start_frame.json`: harmonizes a product paste-up into a photoreal start frame (an ImageCompositeMasked node after decoding copies the original pixels back wherever the mask is black)
- `wan22_i2v.json`: Wan 2.2 A14B image-to-video, two-stage high/low-noise sampling
- `hunyuan15_i2v.json`: HunyuanVideo 1.5 720p image-to-video, adapted (MIT License, see `THIRD_PARTY_NOTICES.md`) from the
  official `video_hunyuan_video_1.5_720p_i2v` template (EasyCache and the 1080p
  super-resolution stage omitted; CFGGuider cfg 6, shift 7, euler/simple 20 steps)
