# Poodll Media Server — Deployment Guide

## Regional Deployments

| Endpoint | AWS Region | Instance |
|---|---|---|
| `media-eu.poodll.io` | `eu-west-1` (Ireland) | g4dn.xlarge or g6.xlarge |
| `media-us.poodll.io` | `us-east-1` (N. Virginia) | g4dn.xlarge or g6.xlarge |
| `media-ap.poodll.io` | `ap-northeast-1` (Tokyo) | g4dn.xlarge |
| `media-cn.poodll.cn` | `cn-northwest-1` (Ningxia) | g4dn.xlarge (L4 not available) |


Start with `media-eu.poodll.io`. All deployments use the same Docker images and config templates — only `.env.prod` values differ.


---

## Pre-requisites

- AWS account with EC2, Route 53, IAM access
- A GPU-optimised AMI: **Deep Learning Base OSS Nvidia Driver GPU AMI (Amazon Linux 2023)**  
  Search for it in the EC2 AMI catalogue — it has NVIDIA drivers pre-installed
- `poodll.io` hosted in Route 53 (or ability to add DNS records)


---

## Step 1: IAM Role for the EC2 Instance

Create an IAM role `poodll-media-server-role` with these policies:
- `AmazonSSMReadOnlyAccess` — to read secrets from SSM Parameter Store
- `AmazonEC2ContainerRegistryReadOnly` — if using ECR for images
- Custom inline policy for S3 write access to Poodll buckets:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:GetObject"],
    "Resource": [
      "arn:aws:s3:::poodll-audioprocessing-out-eu-west-1/*",
      "arn:aws:s3:::poodll-videoprocessing-out-eu-west-1/*"
    ]
  }]
}
```

---

## Step 2: Security Group

Create `poodll-media-server-sg` with inbound rules:

| Port | Protocol | Source | Purpose |
|---|---|---|---|
| 22 | TCP | Your IP only | SSH access |
| 80 | TCP | 0.0.0.0/0 | Let's Encrypt ACME challenge |
| 443 | TCP | 0.0.0.0/0 | HTTPS (token endpoint + test UI) |
| 7880 | TCP | 0.0.0.0/0 | LiveKit WebSocket (WSS) |
| 7881 | TCP | 0.0.0.0/0 | LiveKit RTC TCP fallback |
| 7882 | UDP | 0.0.0.0/0 | LiveKit WebRTC media |

---

## Step 3: Store Secrets in SSM Parameter Store

Store these as **SecureString** parameters:

```bash
aws ssm put-parameter --name /poodll/media/LIVEKIT_API_KEY    --value "your-key"    --type SecureString
aws ssm put-parameter --name /poodll/media/LIVEKIT_API_SECRET --value "your-secret" --type SecureString
aws ssm put-parameter --name /poodll/media/AWS_ACCESS_KEY_ID  --value "AKIA..."     --type SecureString
aws ssm put-parameter --name /poodll/media/AWS_SECRET_ACCESS_KEY --value "..."      --type SecureString
```

Generate strong LiveKit keys:
```bash
openssl rand -hex 16   # API key  (~32 chars)
openssl rand -hex 32   # API secret (~64 chars)
```

---

## Step 4: Launch Template + Auto Scaling Group

### Launch Template settings:
| Field | Value |
|---|---|
| AMI | Deep Learning Base OSS Nvidia Driver GPU AMI (Amazon Linux 2023) |
| Instance type | `g4dn.xlarge` (or `g6.xlarge` for L4 regions) |
| IAM role | `poodll-media-server-role` |
| Security group | `poodll-media-server-sg` |
| Storage | 100 GB gp3 |

| User data | Contents of `deploy/userdata.sh` (see below) |

### Populate User Data environment variables:

Edit `deploy/userdata.sh` to pull from SSM, or set directly (SSM recommended):

```bash
LIVEKIT_API_KEY=$(aws ssm get-parameter --name /poodll/media/LIVEKIT_API_KEY \
    --with-decryption --query Parameter.Value --output text)
# ... repeat for all secrets
```

### Auto Scaling Group settings:
| Field | Value |
|---|---|
| Desired | 1 |
| Minimum | 1 |
| Maximum | 1 |
| Health check | EC2 (default) |

> Setting `min=1` ensures ASG always replaces an unhealthy instance automatically.  
> Raise `max` later when you need to scale horizontally.

---

## Step 5: Elastic IP + DNS

1. Allocate an **Elastic IP** in the target region
2. Associate it with the EC2 instance (or with the ASG via a lifecycle hook script — see note below)
3. In Route 53, create an **A record**:
   - Name: `media-eu.poodll.io`

   - Value: the Elastic IP address
   - TTL: 300

> **ASG + Elastic IP note**: When an ASG replaces an instance, the new instance doesn't automatically inherit the Elastic IP. Add a small script to your User Data to re-associate the EIP using the instance's AWS credentials:
> ```bash
> ALLOCATION_ID="eipalloc-XXXXXXXXX"  # your EIP allocation ID
> INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
> aws ec2 associate-address --instance-id $INSTANCE_ID --allocation-id $ALLOCATION_ID
> ```

---

## Step 6: Verify Deployment

```bash
# SSH to instance
ssh -i your-key.pem ec2-user@<elastic-ip>

# Watch bootstrap logs
tail -f /var/log/userdata.log

# Check containers are running
docker compose -f /opt/poodll-media-server/livekit-worker-setup/docker-compose.prod.yml ps

# Check Caddy got a certificate
docker compose logs caddy | grep certificate
```

Test the token endpoint:
```bash
curl https://media-eu.poodll.io/token?poodlltoken=YOUR_TOKEN&appid=test&parent=https://demo.poodll.io

```

---

## Adding New Regions

1. Launch a new EC2 instance (same process) in the target region
2. Change these values in `.env.prod`:
   - `AWS_REGION=eu-west-1` → target region code
   - `LIVEKIT_PUBLIC_URL=wss://media-eu.poodll.io:7880` → new subdomain
   - `MEDIA_DOMAIN=media-eu.poodll.io` → new subdomain

   - `S3_BUCKET` → matching regional bucket
3. Allocate a new Elastic IP in that region
4. Add Route 53 A record for the new subdomain

Everything else — Docker images, Caddyfile, docker-compose.prod.yml — is identical.

---

## Monitoring

| What | How |
|---|---|
| Container health | `docker compose -f docker-compose.prod.yml ps` |
| Live logs | `docker compose -f docker-compose.prod.yml logs -f worker-agent` |
| GPU utilisation | `nvidia-smi` or `watch -n1 nvidia-smi` |
| Certificate status | `docker compose logs caddy` |
| Disk usage | `df -h` (egress can produce large temp files) |
