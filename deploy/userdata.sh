#!/bin/bash
# =============================================================================
# Poodll Media Server — EC2 User Data Bootstrap Script
# Runs once on first boot. Sets up Docker, NVIDIA drivers, clones the repo,
# and starts the production stack.
#
# Usage: paste into EC2 Launch Template "User data" field (as text, not base64)
# =============================================================================
set -euo pipefail
exec > >(tee /var/log/userdata.log | logger -t userdata) 2>&1

echo "=== Poodll Media Server Bootstrap ==="

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
yum update -y
yum install -y git curl unzip gettext   # gettext provides envsubst

# ---------------------------------------------------------------------------
# 2. Docker (Amazon Linux 2023 uses dnf)
# ---------------------------------------------------------------------------
yum install -y docker
systemctl enable docker
systemctl start docker

# Docker Compose v2
mkdir -p /usr/local/lib/docker/cli-plugins
curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
     -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# ---------------------------------------------------------------------------
# 3. NVIDIA drivers + Container Toolkit
#    Works for both G4dn (T4) and G6 (L4) — driver 525+ supports both
# ---------------------------------------------------------------------------
# Install NVIDIA drivers (Amazon Linux 2023)
dnf install -y kernel-devel-$(uname -r) kernel-headers-$(uname -r) || true
curl -fsSL https://developer.download.nvidia.com/compute/cuda/repos/amzn2023/x86_64/cuda-keyring_1.1-1_all.deb \
     -o /tmp/cuda-keyring.rpm 2>/dev/null || true

# Use AWS-provided NVIDIA driver for ECS GPU-optimised AMI (preferred)
/usr/bin/nvidia-smi && echo "NVIDIA driver already installed (GPU AMI)" || {
    echo "Installing NVIDIA drivers..."
    dnf module install -y nvidia-driver:latest-dkms
}

# NVIDIA Container Toolkit
curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
     > /etc/yum.repos.d/nvidia-container-toolkit.repo
dnf install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

# ---------------------------------------------------------------------------
# 4. Associate Elastic IP
# ---------------------------------------------------------------------------
# This is aws's magic IP. i.e the script asks AWS: "What is my own unique Instance ID?"
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 associate-address \
    --instance-id "$INSTANCE_ID" \
    --allocation-id "${ALLOC_ID}" \
    --region "${AWS_REGION}" || true

# ---------------------------------------------------------------------------
# 5. Clone repository
# ---------------------------------------------------------------------------

# !! Replace with your actual repo URL !!
REPO_URL="${REPO_URL}"
DEPLOY_DIR="/opt/poodll-media-server"


if [ -d "$DEPLOY_DIR" ]; then
    cd "$DEPLOY_DIR" && git pull
else
    git clone "$REPO_URL" "$DEPLOY_DIR"
fi
cd "$DEPLOY_DIR/livekit-worker-setup"

# ---------------------------------------------------------------------------
# 5. Write production environment file
#    !! Store secrets in AWS SSM Parameter Store and retrieve here !!
# ---------------------------------------------------------------------------
# Example using SSM (replace parameter paths with your own):
# LIVEKIT_API_KEY=$(aws ssm get-parameter --name /poodll/media/LIVEKIT_API_KEY --with-decryption --query Parameter.Value --output text)

cat > .env.prod <<EOF
LIVEKIT_API_KEY=${LIVEKIT_API_KEY}
LIVEKIT_API_SECRET=${LIVEKIT_API_SECRET}
LIVEKIT_URL=ws://livekit-server:7880
LIVEKIT_PUBLIC_URL=${LIVEKIT_PUBLIC_URL}
AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}
AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}
AWS_REGION=${AWS_REGION}
S3_BUCKET=poodll-audioprocessing-out-${AWS_REGION}
CLOUDPOODLL_URL=${CLOUDPOODLL_URL}
REDIS_URL=redis://redis:6379
ADMIN_EMAIL=${ADMIN_EMAIL}
MEDIA_DOMAIN=${MEDIA_DOMAIN}
AUTH_CACHE_TTL_OK=3600
AUTH_CACHE_TTL_FAIL=60
RECORDING_READY_URL=
EOF

# ---------------------------------------------------------------------------
# 6. Generate LiveKit + Egress configs from templates (inject API keys)
# ---------------------------------------------------------------------------
envsubst '${LIVEKIT_API_KEY} ${LIVEKIT_API_SECRET}' \
    < livekit.prod.yaml.template > livekit.prod.yaml

envsubst '${LIVEKIT_API_KEY} ${LIVEKIT_API_SECRET}' \
    < egress.prod.yaml.template > egress.prod.yaml

# ---------------------------------------------------------------------------
# 7. Start the stack
# ---------------------------------------------------------------------------
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d --build

echo "=== Bootstrap complete. Stack is starting. ==="
echo "Monitor: docker compose -f docker-compose.prod.yml logs -f"
