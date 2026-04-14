#!/usr/bin/env bash
# =============================================================================
# Poodll Media Server — Deployment Script
#
# Usage:
#   1. Copy deploy/region.conf.example to deploy/region-eu.conf
#   2. Fill in all values in region-eu.conf
#   3. Run: bash deploy/deploy.sh deploy/region-eu.conf
#
# Requires: AWS CLI v2 configured with credentials (aws configure)
# Run from: the livekit-worker-setup/ directory
# =============================================================================
set -euo pipefail

# ── Load region config ───────────────────────────────────────────────────────
CONFIG_FILE="${1:-}"
if [[ -z "$CONFIG_FILE" || ! -f "$CONFIG_FILE" ]]; then
    echo "Usage: bash deploy/deploy.sh deploy/region-eu.conf"
    exit 1
fi
# shellcheck source=/dev/null
source "$CONFIG_FILE"
echo "Deploying: $MEDIA_DOMAIN in $AWS_REGION"

# ── Helpers ──────────────────────────────────────────────────────────────────
# Use the configured profile if provided
aws_with_profile() {
    local cmd="aws"
    if [[ -n "${AWS_PROFILE:-}" ]]; then
        aws "$@" --profile "$AWS_PROFILE"
    else
        aws "$@"
    fi
}

awsr() { aws_with_profile "$@" --region "$AWS_REGION" --output text; }
awsg() { aws_with_profile "$@"; } # g for global (no region)
log()  { echo ""; echo "==> $*"; }

# ── 1. Resolve latest Deep Learning GPU AMI ──────────────────────────────────
log "Finding latest Deep Learning GPU AMI (Amazon Linux 2023)..."
AMI_ID=$(awsr ec2 describe-images \
    --owners amazon \
    --filters "Name=name,Values=Deep Learning Base OSS Nvidia Driver GPU AMI (Amazon Linux 2023)*" \
    --query "sort_by(Images, &CreationDate)[-1].ImageId")
echo "    AMI: $AMI_ID"

# ── 2. IAM role for EC2 ──────────────────────────────────────────────────────
log "Creating IAM role: $IAM_ROLE_NAME ..."
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

# Create role (skip if already exists)
awsg iam create-role \
    --role-name "$IAM_ROLE_NAME" \
    --assume-role-policy-document "$TRUST" \
    --output text 2>/dev/null || echo "    (role already exists, continuing)"

# Attach managed policies
awsg iam attach-role-policy --role-name "$IAM_ROLE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMReadOnlyAccess 2>/dev/null || true

# S3 write access to Poodll buckets
ACCOUNT_ID=$(awsg sts get-caller-identity --query Account --output text)
S3_POLICY="{
  \"Version\":\"2012-10-17\",
  \"Statement\":[{
    \"Effect\":\"Allow\",
    \"Action\":[\"s3:PutObject\",\"s3:GetObject\"],
    \"Resource\":[
      \"arn:aws:s3:::poodll-audioprocessing-out-${AWS_REGION}/*\",
      \"arn:aws:s3:::poodll-videoprocessing-out-${AWS_REGION}/*\",
      \"arn:aws:s3:::poodll-audioprocessing-out/*\",
      \"arn:aws:s3:::poodll-videoprocessing-out/*\"
    ]
  },{
    \"Effect\":\"Allow\",
    \"Action\":[\"ec2:AssociateAddress\",\"ec2:DescribeAddresses\"],
    \"Resource\":\"*\"
  }]
}"
awsg iam put-role-policy \
    --role-name "$IAM_ROLE_NAME" \
    --policy-name PoodllMediaServerPolicy \
    --policy-document "$S3_POLICY" 2>/dev/null || true

# Instance profile
awsg iam create-instance-profile \
    --instance-profile-name "$IAM_ROLE_NAME" \
    --output text 2>/dev/null || true
awsg iam add-role-to-instance-profile \
    --instance-profile-name "$IAM_ROLE_NAME" \
    --role-name "$IAM_ROLE_NAME" 2>/dev/null || true

# ── 3. Security group ────────────────────────────────────────────────────────
log "Setting up security group: $SG_NAME ..."
SG_ID=$(awsr ec2 describe-security-groups \
    --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC_ID" \
    --query "SecurityGroups[0].GroupId" 2>/dev/null || true)

if [[ -z "$SG_ID" || "$SG_ID" == "None" ]]; then
    SG_ID=$(awsr ec2 create-security-group \
        --group-name "$SG_NAME" \
        --description "Poodll Media Server" \
        --vpc-id "$VPC_ID" \
        --query GroupId)
    echo "    Created SG: $SG_ID"

    # Inbound rules
    awsr ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp  --port 22   --cidr "$YOUR_IP/32"  || true
    awsr ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp  --port 80   --cidr 0.0.0.0/0      || true
    awsr ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp  --port 443  --cidr 0.0.0.0/0      || true
    awsr ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp  --port 7880 --cidr 0.0.0.0/0      || true
    awsr ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol tcp  --port 7881 --cidr 0.0.0.0/0      || true
    awsr ec2 authorize-security-group-ingress --group-id "$SG_ID" --protocol udp  --port 7882 --cidr 0.0.0.0/0      || true
else
    echo "    Reusing existing SG: $SG_ID"
fi

# ── 4. Elastic IP ─────────────────────────────────────────────────────────────
log "Allocating Elastic IP..."
ALLOC_ID=$(awsr ec2 describe-addresses \
    --filters "Name=tag:Name,Values=$MEDIA_DOMAIN" \
    --query "Addresses[0].AllocationId" 2>/dev/null || true)

if [[ -z "$ALLOC_ID" || "$ALLOC_ID" == "None" ]]; then
    ALLOC_ID=$(awsr ec2 allocate-address \
        --domain vpc \
        --query AllocationId)
    awsr ec2 create-tags \
        --resources "$ALLOC_ID" \
        --tags "Key=Name,Value=$MEDIA_DOMAIN"
    echo "    Allocated EIP: $ALLOC_ID"
else
    echo "    Reusing existing EIP: $ALLOC_ID"
fi
EIP=$(awsr ec2 describe-addresses \
    --allocation-ids "$ALLOC_ID" \
    --query "Addresses[0].PublicIp")
echo "    Public IP: $EIP"

# ── 5. Generate livekit.prod.yaml + egress.prod.yaml from templates ──────────
log "Generating config files from templates..."
export LIVEKIT_API_KEY LIVEKIT_API_SECRET
envsubst '${LIVEKIT_API_KEY} ${LIVEKIT_API_SECRET}' \
    < livekit.prod.yaml.template > livekit.prod.yaml
envsubst '${LIVEKIT_API_KEY} ${LIVEKIT_API_SECRET}' \
    < egress.prod.yaml.template > egress.prod.yaml
echo "    livekit.prod.yaml + egress.prod.yaml written"

# ── 6. Build User Data script ─────────────────────────────────────────────────
log "Building User Data..."
USER_DATA=$(cat <<USEREOF
#!/bin/bash
set -euo pipefail
exec > >(tee /var/log/userdata.log | logger -t userdata) 2>&1

echo "=== Poodll Media Server Bootstrap ==="

# Docker
dnf install -y docker git gettext
systemctl enable --now docker
mkdir -p /usr/local/lib/docker/cli-plugins
curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
     -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# NVIDIA Container Toolkit
curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo \
     > /etc/yum.repos.d/nvidia-container-toolkit.repo
dnf install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

# Associate Elastic IP
INSTANCE_ID=\$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
aws ec2 associate-address \
    --instance-id "\$INSTANCE_ID" \
    --allocation-id "${ALLOC_ID}" \
    --region "${AWS_REGION}" || true

# Clone / update repo
DEPLOY_DIR="/opt/poodll-media-server"
if [ -d "\$DEPLOY_DIR" ]; then
    cd "\$DEPLOY_DIR" && git pull
else
    git clone "${REPO_URL}" "\$DEPLOY_DIR"
fi
cd "\$DEPLOY_DIR/livekit-worker-setup"

# Write .env.prod
cat > .env.prod <<ENV
LIVEKIT_API_KEY=${LIVEKIT_API_KEY}
LIVEKIT_API_SECRET=${LIVEKIT_API_SECRET}
LIVEKIT_URL=ws://livekit-server:7880
LIVEKIT_PUBLIC_URL=wss://${MEDIA_DOMAIN}:7880
AWS_ACCESS_KEY_ID=${MEDIA_AWS_ACCESS_KEY_ID}
AWS_SECRET_ACCESS_KEY=${MEDIA_AWS_SECRET_ACCESS_KEY}
AWS_REGION=${AWS_REGION}
S3_BUCKET=poodll-audioprocessing-out-${AWS_REGION}
CLOUDPOODLL_URL=https://cloud.poodll.com
REDIS_URL=redis://redis:6379
ADMIN_EMAIL=${ADMIN_EMAIL}
MEDIA_DOMAIN=${MEDIA_DOMAIN}
AUTH_CACHE_TTL_OK=3600
AUTH_CACHE_TTL_FAIL=60
WHISPER_MODEL_SIZE=${WHISPER_MODEL_SIZE:-small}
WHISPER_DEVICE=${WHISPER_DEVICE:-auto}
WHISPER_COMPUTE_TYPE=${WHISPER_COMPUTE_TYPE:-auto}
ENV

# Generate livekit + egress configs
envsubst '\${LIVEKIT_API_KEY} \${LIVEKIT_API_SECRET}' \
    < livekit.prod.yaml.template > livekit.prod.yaml
envsubst '\${LIVEKIT_API_KEY} \${LIVEKIT_API_SECRET}' \
    < egress.prod.yaml.template > egress.prod.yaml

# Start stack
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d --build

echo "=== Bootstrap complete ==="
USEREOF
)

# ── 7. Launch Template ───────────────────────────────────────────────────────
log "Creating Launch Template: $LAUNCH_TEMPLATE_NAME ..."
LT_ID=$(awsr ec2 describe-launch-templates \
    --filters "Name=launch-template-name,Values=$LAUNCH_TEMPLATE_NAME" \
    --query "LaunchTemplates[0].LaunchTemplateId" 2>/dev/null || true)

LT_DATA="{
  \"ImageId\": \"$AMI_ID\",
  \"InstanceType\": \"$INSTANCE_TYPE\",
  \"KeyName\": \"$KEY_PAIR_NAME\",
  \"SecurityGroupIds\": [\"$SG_ID\"],
  \"IamInstanceProfile\": {\"Name\": \"$IAM_ROLE_NAME\"},
  \"BlockDeviceMappings\": [{
    \"DeviceName\": \"/dev/xvda\",
    \"Ebs\": {\"VolumeSize\": 50, \"VolumeType\": \"gp3\", \"DeleteOnTermination\": true}
  }],
  \"UserData\": \"$(echo "$USER_DATA" | base64 -w0)\"
}"

if [[ -z "$LT_ID" || "$LT_ID" == "None" ]]; then
    LT_ID=$(awsr ec2 create-launch-template \
        --launch-template-name "$LAUNCH_TEMPLATE_NAME" \
        --launch-template-data "$LT_DATA" \
        --query "LaunchTemplate.LaunchTemplateId")
    echo "    Created LT: $LT_ID"
else
    # Create new version to pick up any changes
    awsr ec2 create-launch-template-version \
        --launch-template-id "$LT_ID" \
        --source-version '$Latest' \
        --launch-template-data "$LT_DATA" \
        --query "LaunchTemplateVersion.VersionNumber"
    echo "    Updated LT: $LT_ID (new version created)"
fi

# ── 8. Auto Scaling Group ─────────────────────────────────────────────────────
log "Creating Auto Scaling Group: $ASG_NAME ..."
EXISTING_ASG=$(awsg autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$ASG_NAME" \
    --region "$AWS_REGION" \
    --query "AutoScalingGroups[0].AutoScalingGroupName" \
    --output text 2>/dev/null || true)

if [[ -z "$EXISTING_ASG" || "$EXISTING_ASG" == "None" ]]; then
    awsg autoscaling create-auto-scaling-group \
        --auto-scaling-group-name "$ASG_NAME" \
        --launch-template "LaunchTemplateId=$LT_ID,Version=\$Latest" \
        --min-size 1 \
        --max-size 1 \
        --desired-capacity 1 \
        --vpc-zone-identifier "$SUBNET_ID" \
        --health-check-type EC2 \
        --health-check-grace-period 300 \
        --tags "Key=Name,Value=$MEDIA_DOMAIN,PropagateAtLaunch=true" \
        --region "$AWS_REGION"
    echo "    Created ASG: $ASG_NAME"
else
    # Update launch template version so next replacement uses new config
    awsg autoscaling update-auto-scaling-group \
        --auto-scaling-group-name "$ASG_NAME" \
        --launch-template "LaunchTemplateId=$LT_ID,Version=\$Latest" \
        --region "$AWS_REGION"
    echo "    Updated existing ASG: $ASG_NAME"
fi

# ── 9. Route 53 DNS ───────────────────────────────────────────────────────────
log "Updating Route53: $MEDIA_DOMAIN -> $EIP ..."
CHANGE_BATCH="{
  \"Changes\": [{
    \"Action\": \"UPSERT\",
    \"ResourceRecordSet\": {
      \"Name\": \"$MEDIA_DOMAIN\",
      \"Type\": \"A\",
      \"TTL\": 300,
      \"ResourceRecords\": [{\"Value\": \"$EIP\"}]
    }
  }]
}"
awsg route53 change-resource-record-sets \
    --hosted-zone-id "$HOSTED_ZONE_ID" \
    --change-batch "$CHANGE_BATCH" \
    --output text
echo "    DNS record created/updated"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "========================================================="
echo " Deployment complete: $MEDIA_DOMAIN"
echo "========================================================="
echo " Elastic IP:      $EIP"
echo " Token endpoint:  https://$MEDIA_DOMAIN/token"
echo " LiveKit WSS:     wss://$MEDIA_DOMAIN:7880"
echo ""
echo " The instance is launching. Bootstrap takes ~5 minutes."
echo " Monitor via: SSH to $EIP then:"
echo "   tail -f /var/log/userdata.log"
echo "========================================================="
