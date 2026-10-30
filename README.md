# ComfyUI video rig

Proof of concept for AI product-demo video generation. The single question it
answers: **can this pipeline produce an IG-ready product-demo clip with a
consistent synthetic persona and a pixel-faithful branded product?**

Scope decisions (interview, 2026-07-10): quality first, real EC2 G6e rig
driven entirely by `rig.config.toml`, synthetic brand persona (no real
likeness), product fidelity via composited start frames, rig + S3 outputs
only (no SQS/DynamoDB/FastAPI yet), Wan 2.2 vs Hunyuan 1.5 bake-off.

## Architecture

```
WSL workstation                          AWS us-west-2
---------------                          -------------
launch_rig.py  ──boto3──────────────▶    EC2 g6e.xlarge (L40S 48GB)
runner/run_job.py ──SSM tunnel:8188─▶      ComfyUI (127.0.0.1 only, systemd)
                                           /opt/models (FLUX, Wan 2.2, Hunyuan)
                                           idle watchdog (auto-stop)
outputs/<run_id>/ ◀──downloads──           │
   └──s3_sync.py──────────────────▶      S3 outputs bucket (presigned links)
```

No inbound ports are ever opened — all access is SSM. Cost safety is layered: user-data arms a 3h `shutdown -h`
fail-safe that bootstrap cancels once the watchdog timer is live; the watchdog
then stops the instance after `idle_stop_minutes` of GPU idle, kills a
bootstrap stuck past 3h, and treats a queue busy past `max_job_minutes` as a
hung job. A stopped instance costs only its EBS volume.

## Costs (us-west-2, approximate)

| Item | Rate |
|---|---|
| g6e.xlarge on-demand | ~$1.86/hr while running |
| 300GB gp3 EBS | ~$24/mo (persists while stopped) |
| S3 + SSM | negligible at POC scale |

## Prerequisites

- AWS CLI + Session Manager plugin; credentials through `AWS_PROFILE` or the default chain
- `RIG_OUTPUTS_BUCKET` set to an S3 bucket name you own (see `.env.example`)
- **G-instance vCPU quota**: g6e.xlarge needs 4 vCPUs of quota
  `L-DB2E81BA` ("Running On-Demand G and VT instances"). `launch` checks and
  warns; fresh accounts often sit at 0 and need a quota increase first.
- Optional: `HF_TOKEN` env var set at launch time if you swap in gated
  Hugging Face models (the default manifest needs no token). The token is
  stored in SSM parameter `/comfyui-video-rig/hf-token`; a previously stored token is
  reused on later launches until you delete the parameter (deprovision does).
- `ffmpeg` locally for the IG encode + side-by-side steps (`sudo apt install ffmpeg`)

## Runbook

### 0. Launch the rig

```bash
cd comfyui-video-rig
python3 launch_rig.py launch          # provisions bucket/role/SG as needed, boots the box
python3 launch_rig.py session         # shell on the box
  sudo tail -f /var/log/rig-bootstrap.log   # watch install + model downloads (30-60 min)
python3 launch_rig.py tunnel          # keep open; ComfyUI at http://localhost:8188
```

### 1. Validate workflows (first run only)

Node/field names drift across ComfyUI versions and parts of
`bootstrap/models.json` are marked UNVERIFIED. In the ComfyUI web UI, load
`Workflow > Browse Templates` for Wan 2.2 / HunyuanVideo / Flux, fix any
manifest filenames, and export a tagged `hunyuan15_i2v.json` —
see `workflows/README.md`.

### 2. Lock the persona

```bash
python3 runner/run_job.py jobs/examples/persona_ref.toml --no-sync
```

Rerun with different seeds until a candidate look wins. Save the winning refs
and the exact prompt text in `personas/presenter_kitchen_v1/` (see its README).

### 3. Build a start frame

Real product photo in `products/`, rough paste-up + mask, then FLUX harmonize:

```bash
# rough composite: product pasted onto a persona scene at roughly the right spot
magick persona_scene.png products/shaker_bottle.png -geometry +260+700 -composite \
  products/pasteups/shaker_kitchen_pasteup.png
# mask: BLACK protects the product, WHITE lets FLUX repaint (start from all-white,
# draw a black shape over the product; any editor works)

python3 runner/run_job.py jobs/examples/start_frame.toml --no-sync
# pick the best result and copy it to outputs/start_frames/shaker_kitchen_v1.png
```

### 4. The bake-off

```bash
python3 runner/run_job.py jobs/examples/shaker_demo.toml
```

Runs the same start frame + seed through Wan 2.2 and Hunyuan 1.5, downloads
clips to `outputs/<run_id>/`, encodes IG-ready 9:16 MP4s, builds
`compare_side_by_side.mp4`, and prints presigned S3 share links (7 days).

Iterate: vary seeds and prompts, keep the manifests — `outputs/*/manifest.json`
records every parameter of every run.

### 5. Stop the meter

```bash
python3 launch_rig.py stop       # or let the idle watchdog do it
python3 launch_rig.py start      # next session; models persist on EBS
```

## Success gate

Across ~10 test generations, at least one 5-8s clip where:

1. the persona's face/body stays consistent across frames
2. the product label is legible and undistorted for the clip's duration
3. hand-product contact is believable (no extra/fused fingers)
4. flicker/warp is within what a Topaz/upscale pass could clean up

Pass → productionize (queue worker, job table, API wrapper).
Fail → revisit models (LTX, closed APIs) before building any platform surface.

## Teardown

```bash
python3 launch_rig.py terminate    # deletes instance + EBS (models gone)
python3 launch_rig.py deprovision  # removes SG + IAM role/profile + HF token param; bucket kept
```

## Layout

```
rig.config.toml        every rig knob (instance type, AMI, disk, models, idle timeout)
launch_rig.py          provision/launch/status/stop/start/terminate/tunnel/session/deprovision
bootstrap/             runs on the instance at first boot (ComfyUI, models, systemd, watchdog)
workflows/             ComfyUI API-format templates, RIG_* injection convention
jobs/examples/         persona_ref -> start_frame -> shaker_demo (the three-step pipeline)
runner/                run_job.py (submit/collect/encode/compare), comfy_client.py, s3_sync.py
personas/ products/    persona bible + refs; real product photos + paste-ups
outputs/               run folders (gitignored): clips, manifests, side-by-sides
```
