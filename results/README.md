# Recorded runs

`manifests/` holds the 16 run manifests that `runner/run_job.py` wrote on
2026-07-11, one per run, named by run ID. The generated images and clips are
not in the repo; `docs/media/` holds one re-encoded comparison clip (see the
main README for its provenance).

`summarize.py` prints the table below and builds `docs/data.json`, which feeds
the README charts and the landing page:

```bash
python3 results/summarize.py            # table
python3 results/summarize.py --check    # docs/data.json is current (also a test)
```

## What the numbers measure

`elapsed_s` is measured by `execute_run()` in `runner/run_job.py`: from just
before `POST /prompt` to the end of the last output download. It includes
queue wait, model loading and the download through the SSM tunnel. It excludes
the ffmpeg encode.

The manifests do not record the instance type, the ComfyUI, custom node or
torch versions, or model file checksums. `rig.config.toml` in the import
commit sets `instance_type = "g6e.xlarge"` (1x NVIDIA L40S 48 GB); that the
runs used it is an inference from the config, not a recorded fact.

## Edits made when copying the manifests

The copies differ from the originals only in these fields:

1. `job_file`: eight chain segment manifests stored an absolute path inside
   the original checkout. The path is now repo-relative.
2. `job_file` of runs 20260711-143107 and 20260711-143242: the original
   pointed at a temporary copy of the job outside the repo. It now names
   `jobs/headphones_test/frame_headphones.toml`. The `job` block of these two
   runs and of 20260711-144914 is as recorded and holds an earlier prompt,
   negative prompt and seed (555000) than the committed job file, which
   matches run 20260711-145433 (seed 555001).
3. `job.persona_id`: the two persona IDs of the source are replaced by
   `presenter_kitchen_v1` and `presenter_desk_v1`, the names used in this repo.
4. `job.prompt` of run 20260711-145433: a camera brand and model name is
   replaced with "a full-frame camera".

Timings, prompt IDs, output file names, timestamps and error messages are as
recorded.

## Runs

| Run | Job file | Model run | Seconds | Status |
|---|---|---|---:|---|
| 20260711-103430-persona_ref | `jobs/examples/persona_ref.toml` | flux_persona_ref | 194.0 | ok |
| 20260711-103824-frame_shaker | `jobs/live_test/frame_shaker.toml` | flux_persona_ref | 16.7 | ok |
| 20260711-103944-video_shaker | `jobs/live_test/video_shaker.toml` | wan22 | 1432.2 | ok |
| 20260711-131254-bakeoff_shaker | `jobs/live_test/bakeoff_shaker.toml` | wan22 | 1490.4 | ok |
| 20260711-131254-bakeoff_shaker | `jobs/live_test/bakeoff_shaker.toml` | hunyuan15 | 1587.9 | ok |
| 20260711-143107-frame_headphones | `jobs/headphones_test/frame_headphones.toml` | flux_persona_ref |  | error |
| 20260711-143242-frame_headphones | `jobs/headphones_test/frame_headphones.toml` | flux_persona_ref |  | error |
| 20260711-144914-frame_headphones | `jobs/headphones_test/frame_headphones.toml` | flux_persona_ref | 155.1 | ok |
| 20260711-145433-frame_headphones | `jobs/headphones_test/frame_headphones.toml` | flux_persona_ref | 26.2 | ok |
| 20260711-145915-wan22_seg1 | `jobs/headphones_test/generated/wan22_seg1.toml` | wan22 | 1444.2 | ok |
| 20260711-152357-wan22_seg2 | `jobs/headphones_test/generated/wan22_seg2.toml` | wan22 | 1412.1 | ok |
| 20260711-154755-wan22_seg3 | `jobs/headphones_test/generated/wan22_seg3.toml` | wan22 | 1408.8 | ok |
| 20260711-161138-wan22_seg4 | `jobs/headphones_test/generated/wan22_seg4.toml` | wan22 | 1409.3 | ok |
| 20260711-163511-hunyuan15_seg1 | `jobs/headphones_test/generated/hunyuan15_seg1.toml` | hunyuan15 | 1572.9 | ok |
| 20260711-170127-hunyuan15_seg2 | `jobs/headphones_test/generated/hunyuan15_seg2.toml` | hunyuan15 | 1451.7 | ok |
| 20260711-172553-hunyuan15_seg3 | `jobs/headphones_test/generated/hunyuan15_seg3.toml` | hunyuan15 | 1397.4 | ok |
| 20260711-174924-hunyuan15_seg4 | `jobs/headphones_test/generated/hunyuan15_seg4.toml` | hunyuan15 | 1378.9 | ok |

The two error rows failed with `cannot reach ComfyUI at http://127.0.0.1:8188`
and `(timed out)`. The manifests do not say why; a tunnel that was not open is
the likely cause (inference). At that revision the run folder was created
before the first request; `run_job.py` now checks the connection first.
