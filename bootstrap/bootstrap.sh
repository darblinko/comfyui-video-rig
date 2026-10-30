#!/bin/bash
# Bootstraps the ComfyUI video rig on a fresh Deep Learning Base OSS NVIDIA
# Driver GPU AMI (Ubuntu 22.04). Runs once from EC2 user-data; safe to re-run
# manually: sudo bash /opt/rig-bootstrap/bootstrap.sh
#
# Cost-safety ordering: the idle watchdog is installed BEFORE anything slow or
# failure-prone, so no later failure can leave the box billing unattended. The
# watchdog holds off while bootstrap is in progress (no BOOT_MARKER yet) but
# stops a stuck bootstrap after 3 hours.
set -euo pipefail
exec > >(tee -a /var/log/rig-bootstrap.log) 2>&1

source /etc/rig.env
source /opt/rig-bootstrap/versions.env
COMFY_DIR=/opt/comfyui
MODELS_DIR=/opt/models
RUN_USER=ubuntu
PIP="$COMFY_DIR/venv/bin/pip"
BOOT_MARKER=/var/lib/rig-bootstrap-done
VERSIONS_FILE=/opt/rig-bootstrap/installed-versions.txt

# Clone a repository if absent, check out a pinned ref when one is given, and
# record the commit that ends up installed.
install_repo() {
  local url=$1 dir=$2 ref=$3
  if [ ! -d "$dir" ]; then
    git clone "$url" "$dir"
  fi
  # A re-run as root meets checkouts already chowned to $RUN_USER.
  local git=(git -c safe.directory="$dir" -C "$dir")
  if [ -n "$ref" ]; then
    "${git[@]}" fetch origin "$ref"
    "${git[@]}" checkout --detach FETCH_HEAD
  fi
  echo "$url $("${git[@]}" rev-parse HEAD)${ref:+ (pinned $ref)}" >> "$VERSIONS_FILE"
}

echo "=== [1/7] GPU check"
nvidia-smi

echo "=== [2/7] System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ffmpeg git python3-venv python3-pip curl

echo "=== [3/7] Idle watchdog (before anything slow can fail)"
install -m 0755 /opt/rig-bootstrap/idle_watchdog.sh /usr/local/bin/rig-idle-watchdog

cat > /etc/systemd/system/rig-idle-watchdog.service <<EOF
[Unit]
Description=Stop the rig when the GPU has been idle

[Service]
Type=oneshot
ExecStart=/usr/local/bin/rig-idle-watchdog
EOF

cat > /etc/systemd/system/rig-idle-watchdog.timer <<EOF
[Unit]
Description=Run the idle watchdog every 5 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now rig-idle-watchdog.timer
# The user-data fail-safe (shutdown -h +180) is no longer needed: the watchdog
# owns cost safety from here, including the 3h stuck-bootstrap backstop.
shutdown -c 2>/dev/null || true

echo "=== [4/7] ComfyUI"
echo "# bootstrap $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$VERSIONS_FILE"
install_repo https://github.com/comfyanonymous/ComfyUI "$COMFY_DIR" "$COMFYUI_REF"
if [ ! -d "$COMFY_DIR/venv" ]; then
  python3 -m venv "$COMFY_DIR/venv"
fi
"$PIP" install --upgrade pip
# shellcheck disable=SC2086  # TORCH_REQUIREMENTS is a list of specifiers
"$PIP" install $TORCH_REQUIREMENTS
"$PIP" install -r "$COMFY_DIR/requirements.txt"
"$PIP" install "huggingface_hub[hf_transfer]"

echo "=== [5/7] Custom nodes"
for spec in \
  "https://github.com/ltdrdata/ComfyUI-Manager $COMFYUI_MANAGER_REF" \
  "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite $VIDEOHELPERSUITE_REF"; do
  read -r repo ref <<< "$spec"
  dir="$COMFY_DIR/custom_nodes/$(basename "$repo")"
  install_repo "$repo" "$dir" "${ref:-}"
  if [ -f "$dir/requirements.txt" ]; then
    "$PIP" install -r "$dir/requirements.txt"
  fi
done

"$PIP" freeze >> "$VERSIONS_FILE"

echo "=== [6/7] Models"
mkdir -p "$MODELS_DIR"/{checkpoints,diffusion_models,text_encoders,vae,clip_vision,loras,controlnet,upscale_models}
cat > "$COMFY_DIR/extra_model_paths.yaml" <<EOF
rig_models:
  base_path: $MODELS_DIR
  checkpoints: checkpoints
  diffusion_models: diffusion_models
  text_encoders: text_encoders
  vae: vae
  clip_vision: clip_vision
  loras: loras
  controlnet: controlnet
  upscale_models: upscale_models
EOF

HF_TOKEN=$(aws ssm get-parameter --name "$RIG_HF_TOKEN_PARAM" --with-decryption \
  --query Parameter.Value --output text --region "$RIG_REGION" 2>/dev/null || true)
if [ -n "${HF_TOKEN:-}" ]; then
  export HF_TOKEN
fi
export HF_HUB_ENABLE_HF_TRANSFER=1
if ! "$COMFY_DIR/venv/bin/python" /opt/rig-bootstrap/download_models.py \
  --manifest /opt/rig-bootstrap/models.json \
  --groups "$RIG_MODEL_GROUPS" \
  --dest "$MODELS_DIR"; then
  echo "WARNING: model downloads failed entirely; continuing so services still" \
       "come up; fix bootstrap/models.json and re-run download_models.py manually"
fi

echo "=== [7/7] ComfyUI service"
chown -R "$RUN_USER:$RUN_USER" "$COMFY_DIR" "$MODELS_DIR"

cat > /etc/systemd/system/comfyui.service <<EOF
[Unit]
Description=ComfyUI (localhost only, reached through the SSM tunnel)
After=network.target

[Service]
User=$RUN_USER
WorkingDirectory=$COMFY_DIR
ExecStart=$COMFY_DIR/venv/bin/python main.py --listen 127.0.0.1 --port $RIG_COMFY_PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now comfyui.service

touch "$BOOT_MARKER"
echo "=== Bootstrap complete"
echo "ComfyUI on 127.0.0.1:$RIG_COMFY_PORT. Open a tunnel from your workstation:"
echo "  python3 launch_rig.py tunnel"
