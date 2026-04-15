# Poodll Media Server — Deployment Guide

> [!NOTE]
> This is **not** ECS task-based deployment. The server runs as Docker Compose on a single GPU-enabled EC2 instance, managed by an Auto Scaling Group for automatic health replacement.

---

## Regional Deployments

| Endpoint | AWS Region | Instance type |
|---|---|---|
| `media-eu.poodll.io` | `eu-west-1` (Ireland) | `g4dn.xlarge` or `g6.xlarge` |
| `media-us.poodll.io` | `us-east-1` (N. Virginia) | `g4dn.xlarge` or `g6.xlarge` |
| `media-ap.poodll.io` | `ap-northeast-1` (Tokyo) | `g4dn.xlarge` |
| `media-cn.poodll.cn` | `cn-northwest-1` (Ningxia) | `g4dn.xlarge` (L4 not available) |

Start with `media-eu.poodll.io`. All regions use the **same Docker images and scripts** — only the region config file differs.


---

## One-Time Setup (Local Machine)

### 1. Install AWS CLI

```bash
# macOS
brew install awscli

# Windows (PowerShell)
msiexec.exe /i https://awscli.amazonaws.com/AWSCLIV2.msi

# Verify
aws --version
```

### 2. Configure AWS credentials

```bash
aws configure --profile aws-global
# → AWS Access Key ID: (from IAM → Users → Security credentials)
# → AWS Secret Access Key: (same page)
# → Default region name: eu-west-1
# → Default output format: json
```

### 3. Create an EC2 key pair (for SSH access)

Either create in the AWS Console (EC2 → Key Pairs → Create key pair) or via CLI:
Use the aws-global or aws-china profile in AWS commands depending on the target region

```bash
 aws ec2 create-key-pair \
    --key-name poodll-media-key-eu \
    --region eu-west-1 \
    --profile aws-global \
    --query "KeyMaterial" \
    --output text > poodll-media-key-eu.pem  
	
	 aws ec2 create-key-pair \
    --key-name poodll-media-key-cn \
    --region cn-northwest-1 \
    --profile aws-china \
    --query "KeyMaterial" \
    --output text > poodll-media-key-cn.pem  

chmod 400 poodll-media-key-eu.pem
chmod 400 poodll-media-key-cn.pem
```

### 4. Find required AWS identifiers

You'll need these for your config file:

```bash
# Your current public IP (for SSH-only access rule)
curl https://checkip.amazonaws.com

# VPC ID (use the default VPC)
aws ec2 describe-vpcs \
    --filters "Name=isDefault,Values=true" \
    --query "Vpcs[0].VpcId" \
    --region eu-west-1 \
    --profile aws-global \
    --output text

# Subnet ID (pick any public subnet in that VPC)
aws ec2 describe-subnets \
    --filters "Name=defaultForAz,Values=true" \
    --query "Subnets[0].SubnetId" \
    --region eu-west-1 \
    --output text \
    --profile aws-global

# Hosted Zone ID for poodll.io
aws route53 list-hosted-zones-by-name \
    --dns-name poodll.io \
    --query "HostedZones[0].Id" \
    --output text \
    --profile aws-global

# Returns: /hostedzone/Z1234567890ABCDEF  (strip the /hostedzone/ prefix)
```

### 5. Generate strong LiveKit credentials

```bash
openssl rand -hex 16   # → API key  (e.g. a3f8b2c1d4e5f6a7)
openssl rand -hex 32   # → API secret (must be ≥ 32 bytes)
```

---

## Deploying a Region

### Step 1: Create the region config file

```bash
cp livekit-worker-setup/deploy/region.conf.example livekit-worker-setup/deploy/region-eu.conf
```

Edit `region-eu.conf` and fill in all values (see the annotated example file).
Key fields:

| Field | Where to find it |
|---|---|
| `AWS_REGION` | e.g. `eu-west-1` |
| `MEDIA_DOMAIN` | e.g. `media-eu.poodll.io` |
| `VPC_ID` | From Step 4 above |
| `SUBNET_ID` | From Step 4 above |
| `YOUR_IP` | From Step 4 above |
| `HOSTED_ZONE_ID` | From Step 4 above (without `/hostedzone/`) |
| `KEY_PAIR_NAME` | Name of the key pair from Step 3 |
| `LIVEKIT_API_KEY` | Generated in Step 5 |
| `LIVEKIT_API_SECRET` | Generated in Step 5 |
| `MEDIA_AWS_ACCESS_KEY_ID` | AWS key for Poodll S3 bucket access |
| `REPO_URL` | Your Git repository URL |
| `ADMIN_EMAIL` | `woof@poodll.com` (for Let's Encrypt certs) |

> [!TIP]
> **Customizing the Bootstrap**: The `deploy/deploy.sh` script reads the bootstrap logic from **`deploy/userdata.sh`**. If you need to add custom initialization steps (e.g., additional system packages), you can edit `userdata.sh` directly before running the deploy script.

> [!CAUTION]

> Never commit `region-*.conf` files to git — they contain plaintext secrets.

### Step 2: Run the deploy script

```bash
cd livekit-worker-setup
bash deploy/deploy.sh deploy/region-eu.conf
```

The script will:
1. Find the latest GPU AMI (Amazon Linux 2023 + NVIDIA drivers)
2. Create an IAM role with S3 and SSM permissions
3. Create a security group with the correct ports open
4. Allocate an Elastic IP
5. Generate `livekit.prod.yaml` and `egress.prod.yaml` from templates
6. Create a Launch Template with the bootstrap User Data script
7. Create an Auto Scaling Group (desired=1, min=1) for health replacement
8. Create/update the Route 53 A record pointing to the Elastic IP

Output example:
```
==========================================================
 Deployment complete: media-eu.poodll.io
==========================================================
 Elastic IP:      52.12.34.56
 Token endpoint:  https://media-eu.poodll.io/token
 LiveKit WSS:     wss://media-eu.poodll.io:7880


 The instance is launching. Bootstrap takes ~5 minutes.
 Monitor via: SSH to 52.12.34.56 then:
   tail -f /var/log/userdata.log
==========================================================
```

### Step 3: Monitor the bootstrap

SSH to the instance ~2 minutes after the script completes:

```bash
ssh -i poodll-media-key.pem ec2-user@<elastic-ip>
# use the real ip if this fails, elastic ip might be slow or een broken

# Watch the bootstrap log
tail -f /var/log/userdata.log

# Once bootstrap is done, check containers
cd /opt/poodll-media-server/livekit-worker-setup
docker compose -f docker-compose.prod.yml ps
```

All containers should show `Up`:
```
NAME             STATUS
redis            Up
livekit-server   Up
egress           Up
worker-agent     Up
token-server     Up
caddy            Up
```

### Step 4: Verify the deployment

```bash
# Check Caddy got a Let's Encrypt certificate
docker compose -f docker-compose.prod.yml logs caddy | grep certificate

# Test the token endpoint
curl "https://media-eu.poodll.io/token?parent=https://demo.poodll.io&appid=test"
# Should return: {"error": "Authentication failed", ...} (expected without a real poodlltoken)

# Confirm LiveKit WebSocket is accessible
curl -i "https://media-eu.poodll.io/token" 2>&1 | head -5

```

---

## Security Group Ports Reference

The deploy script opens these ports automatically:

| Port | Protocol | Purpose |
|---|---|---|
| 22 | TCP | SSH — restricted to `YOUR_IP` only |
| 80 | TCP | Let's Encrypt ACME challenge (Caddy) |
| 443 | TCP | HTTPS — token endpoint + test UI |
| 7880 | TCP | LiveKit WebSocket (WSS) |
| 7881 | TCP | LiveKit RTC TCP fallback |
| 7882 | UDP | LiveKit WebRTC media |

---

## Deploying Additional Regions

The process is identical — just create a new config file:

```bash
cp deploy/region.conf.example deploy/region-us.conf
# Edit: change AWS_REGION, MEDIA_DOMAIN, VPC_ID, SUBNET_ID
# (Use YOUR_IP from your current machine, and re-run aws ec2 describe-vpcs --profile aws-global
#  with --region us-east-1 to get the US VPC/subnet)

bash deploy/deploy.sh deploy/region-us.conf
```

> [!NOTE]
> The IAM role (`poodll-media-server-role`) is global — the script will detect it already exists and skip creation on subsequent regions. The security group (`SG_NAME`), launch template, and ASG are regional — give them distinct names per region (e.g. `poodll-media-server-sg-us`).

---

## Updating a Deployed Instance

If you change code and want to update a running instance:

```bash
# Option 1: SSH and pull + restart (quick, no downtime planning needed)
ssh -i poodll-media-key.pem ec2-user@<elastic-ip>
cd /opt/poodll-media-server
git pull
docker compose -f livekit-worker-setup/docker-compose.prod.yml pull
docker compose -f livekit-worker-setup/docker-compose.prod.yml up -d --build

# Option 2: Re-run deploy.sh (creates a new Launch Template version;
#            next time the ASG replaces the instance, new code is used)
bash deploy/deploy.sh deploy/region-eu.conf
```

---

## Instance Replacement Behaviour

The Auto Scaling Group (ASG) continually monitors the EC2 instance's health. If:
- The instance fails an EC2 status check (hardware failure, kernel panic, etc.)
- You manually terminate the instance

...the ASG will automatically launch a new replacement instance. The User Data script re-runs on the new instance, re-associates the Elastic IP, clones the repo, and starts the stack. **DNS is not affected** — `media-eu.poodll.io` always points to the same Elastic IP.


Expected recovery time: ~5–8 minutes (instance launch + bootstrap).

---

## Monitoring

```bash
# Container status
docker compose -f docker-compose.prod.yml ps

# Live logs (all containers)
docker compose -f docker-compose.prod.yml logs -f

# Worker agent logs only
docker compose -f docker-compose.prod.yml logs -f worker-agent

# GPU utilisation
nvidia-smi
# or: watch -n2 nvidia-smi

# Disk usage (egress creates large temp files)
df -h

# Caddy certificate status
docker compose -f docker-compose.prod.yml logs caddy | grep -i cert
```

---

## Cost Reference

| Configuration | Monthly cost (approx.) |
|---|---|
| `g4dn.xlarge` On-Demand (24/7) | ~$380/month |
| `g4dn.xlarge` Reserved 1yr | ~$220/month |
| `g6.xlarge` On-Demand (24/7) | ~$575/month |
| Elastic IP (when associated) | Free |
| Route 53 hosted zone | ~$0.50/month |
| Data transfer (variable) | ~$0.09/GB |

> [!TIP]
> For a school use case, consider scheduling the instance to shut down overnight and weekends. An ASG scheduled action can set `desired=0` in the evening and `desired=1` in the morning — saving ~50% of compute costs with zero code changes.
