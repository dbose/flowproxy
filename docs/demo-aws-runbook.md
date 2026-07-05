# AWS demo runbook: FlowProxy end-to-end with QuickSight

Stand up the full loop: GitHub Actions publishes a manifest bundle and a DuckDB
sidecar to S3, an EC2 instance runs FlowProxy in STORE mode, and Amazon
QuickSight (Standard) connects and slices the semantic layer.

Topology and rationale: [ADR-0016](adr/0016-e2e-aws-demo-topology.md). Plan:
[demo-aws-plan.md](demo-aws-plan.md).

This uses the **cheapest** path: one t3.micro (free-tier eligible), one S3
bucket, GitHub OIDC (no stored AWS keys), public IP + password auth. Teardown at
the end leaves no bill.

> The offline dress rehearsal (build -> bundle -> entrypoint -> wire query ->
> AMER 3000 / EMEA 2000) is already green, so the app is proven. This runbook is
> only the AWS + QuickSight wiring.

Set these once:

```bash
export AWS_REGION=us-east-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export BUCKET=flowproxy-demo-$ACCOUNT_ID
export S3_PREFIX=s3://$BUCKET/finance
export GH_REPO=dbose/flowproxy      # owner/repo
```

## 1. S3 bucket

```bash
aws s3api create-bucket --bucket $BUCKET --region $AWS_REGION \
  $( [ "$AWS_REGION" = "us-east-1" ] || echo --create-bucket-configuration LocationConstraint=$AWS_REGION )
aws s3api put-public-access-block --bucket $BUCKET \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

## 2. GitHub OIDC provider + CI publish role

Lets GitHub Actions assume an AWS role with no long-lived keys.

```bash
# OIDC provider (skip if it already exists in the account)
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 || true

# Trust policy: only this repo, only workflow_dispatch-driven runs on main
cat > /tmp/trust.json <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "arn:aws:iam::$ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
      "StringLike": {"token.actions.githubusercontent.com:sub": "repo:$GH_REPO:*"}
    }
  }]
}
JSON
aws iam create-role --role-name flowproxy-ci-publish \
  --assume-role-policy-document file:///tmp/trust.json

# Least privilege: write only under this bucket/prefix
cat > /tmp/ci-policy.json <<JSON
{"Version":"2012-10-17","Statement":[
  {"Effect":"Allow","Action":["s3:PutObject","s3:GetObject","s3:ListBucket"],
   "Resource":["arn:aws:s3:::$BUCKET","arn:aws:s3:::$BUCKET/finance/*"]}
]}
JSON
aws iam put-role-policy --role-name flowproxy-ci-publish \
  --policy-name s3-publish --policy-document file:///tmp/ci-policy.json

echo "CI role ARN: arn:aws:iam::$ACCOUNT_ID:role/flowproxy-ci-publish"
```

Then set the repo variables (Settings -> Secrets and variables -> Actions ->
Variables), or via CLI:

```bash
gh variable set AWS_REGION --body "$AWS_REGION" --repo $GH_REPO
gh variable set AWS_PUBLISH_ROLE_ARN --body "arn:aws:iam::$ACCOUNT_ID:role/flowproxy-ci-publish" --repo $GH_REPO
gh variable set FLOWPROXY_S3_PREFIX --body "$S3_PREFIX" --repo $GH_REPO
```

## 3. Publish the bundle + sidecar (run the workflow)

```bash
gh workflow run semantic-layer.yml --repo $GH_REPO -f publish=true
gh run watch --repo $GH_REPO
# On success, confirm the two objects exist:
aws s3 ls $S3_PREFIX/manifest/
aws s3 ls $S3_PREFIX/warehouse/
#   manifest/current.tar   manifest/VERSION   warehouse/finance_demo.duckdb
```

## 4. EC2 instance role (read S3)

```bash
cat > /tmp/ec2-trust.json <<'JSON'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
aws iam create-role --role-name flowproxy-ec2 --assume-role-policy-document file:///tmp/ec2-trust.json
cat > /tmp/ec2-policy.json <<JSON
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:GetObject","s3:ListBucket"],
  "Resource":["arn:aws:s3:::$BUCKET","arn:aws:s3:::$BUCKET/finance/*"]}]}
JSON
aws iam put-role-policy --role-name flowproxy-ec2 --policy-name s3-read --policy-document file:///tmp/ec2-policy.json
aws iam create-instance-profile --instance-profile-name flowproxy-ec2
aws iam add-role-to-instance-profile --instance-profile-name flowproxy-ec2 --role-name flowproxy-ec2
```

## 5. Security group (open 5432 to QuickSight + 22 to you)

QuickSight (Standard) connects from AWS-published IP ranges for your region.
Look them up: [QuickSight IP ranges](https://docs.aws.amazon.com/quicksight/latest/user/regions.html).

```bash
export VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
export SG_ID=$(aws ec2 create-security-group --group-name flowproxy-demo \
  --description "FlowProxy demo" --vpc-id $VPC_ID --query GroupId --output text)
export MYIP=$(curl -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 22   --cidr $MYIP/32
# For a quick demo you may open 5432 to your IP too and use an SSH tunnel from QuickSight's
# region; for a real QuickSight connection, add each QuickSight IP range for $AWS_REGION:
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 5432 --cidr $MYIP/32
# aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 5432 --cidr <QUICKSIGHT_RANGE>/27
```

## 6. Launch EC2 and run FlowProxy

Amazon Linux 2023, t3.micro, Docker, with a user-data script that builds the
image from the repo and runs it in STORE mode.

```bash
cat > /tmp/userdata.sh <<USERDATA
#!/bin/bash
set -euxo pipefail
dnf install -y docker git
systemctl enable --now docker
git clone https://github.com/$GH_REPO /opt/flowproxy
cd /opt/flowproxy
docker build -t flowproxy .
docker run -d --restart unless-stopped -p 5432:5432 --name flowproxy \\
  -e FLOWPROXY_MANIFEST_URI=$S3_PREFIX/manifest/ \\
  -e FLOWPROXY_DUCKDB_URI=$S3_PREFIX/warehouse/finance_demo.duckdb \\
  -e FLOWPROXY_MANIFEST_POLL_INTERVAL=300 \\
  -e FLOWPROXY_PASSWORD=demopass \\
  -e AWS_REGION=$AWS_REGION \\
  flowproxy
USERDATA

export AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query 'Parameter.Value' --output text)
export INSTANCE_ID=$(aws ec2 run-instances \
  --image-id $AMI --instance-type t3.micro \
  --iam-instance-profile Name=flowproxy-ec2 \
  --security-group-ids $SG_ID \
  --user-data file:///tmp/userdata.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=flowproxy-demo}]' \
  --query 'Instances[0].InstanceId' --output text)

aws ec2 wait instance-running --instance-ids $INSTANCE_ID
export PUBLIC_IP=$(aws ec2 describe-instances --instance-ids $INSTANCE_ID \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo "FlowProxy will be at $PUBLIC_IP:5432 in ~2-3 min (image build + boot)"
```

Verify (from your machine, once the security group allows your IP):

```bash
# psql, or the repo's tiny wire client
psql "host=$PUBLIC_IP port=5432 user=analyst password=demopass dbname=flowproxy sslmode=disable" \
  -c 'SELECT "account__region","account_balance" FROM "daily_balances" GROUP BY 1'
#  AMER | 3000.0
#  EMEA | 2000.0
```

## 7. Connect QuickSight

1. QuickSight console -> **Datasets** -> **New dataset** -> **PostgreSQL**.
2. Connection:
   - Database server: the EC2 **public IP**
   - Port: `5432`
   - Database name: `flowproxy` (any name works)
   - Username: `analyst`  Password: `demopass`
   - SSL: disabled for the demo (terminate TLS at an NLB in production).
3. **Validate connection** -> **Create data source**.
4. Schema: pick **`semantic_layer`**. You will see the cubes:
   `daily_balances`, `transactions`, `all_metrics`, and the saved-query tables.
5. Choose **`daily_balances`**, select **Directly query your data** (not SPICE -
   SPICE issues `SELECT *`, which has no semantic mapping).
6. Build an analysis: drag `account__region` and `account_balance` onto a table
   visual. Expect **AMER 3000, EMEA 2000** - the end-of-period balances. Add
   `metric_time` at a monthly grain to see 1500 / 1200 / 2000 for EMEA.

The point to demo: set `account_balance` to **Sum** in QuickSight's field menu.
The number does not change, because the aggregation lives in the metric
definition, not the SQL. That is the guardrail, visible.

## 8. Teardown (leave no bill)

```bash
aws ec2 terminate-instances --instance-ids $INSTANCE_ID
aws ec2 wait instance-terminated --instance-ids $INSTANCE_ID
aws ec2 delete-security-group --group-id $SG_ID
aws s3 rm $S3_PREFIX --recursive && aws s3api delete-bucket --bucket $BUCKET --region $AWS_REGION
aws iam remove-role-from-instance-profile --instance-profile-name flowproxy-ec2 --role-name flowproxy-ec2
aws iam delete-instance-profile --instance-profile-name flowproxy-ec2
aws iam delete-role-policy --role-name flowproxy-ec2 --policy-name s3-read
aws iam delete-role --role-name flowproxy-ec2
aws iam delete-role-policy --role-name flowproxy-ci-publish --policy-name s3-publish
aws iam delete-role --role-name flowproxy-ci-publish
# (leave the OIDC provider if other repos use it)
```

## Production deltas (documented, not built)

- **Compute:** ECS Fargate task behind a Network Load Balancer exposing 5432;
  the image and entrypoint are identical, only placement changes.
- **Access:** QuickSight **VPC connection** (Enterprise edition) to a private
  NLB, TLS terminated at the NLB, no public IP.
- **Auth:** SCRAM + per-role metric ACLs (WS4), not cleartext password.
- **Warehouse:** swap the DuckDB profile in `profiles.template.yml` for a
  Snowflake/Redshift profile; the sidecar download disappears (a real warehouse
  is already networked).

## Troubleshooting

- `Catalog "finance_demo" does not exist`: the sidecar was renamed. It must keep
  the `finance_demo.duckdb` basename (ADR-0016 decision 4). The entrypoint
  preserves it from the URI basename.
- Blank QuickSight datasource pane: the driver's introspection probe was not
  answered. Check `information_schema.tables` returns the cubes (the repo's
  catalog responder handles the QuickSight JDBC path; Power BI is deferred).
- Connection refused: security group does not allow the source IP on 5432, or
  the image is still building (watch `docker logs flowproxy` via SSH).
