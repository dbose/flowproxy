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
> AMER 3000 / EMEA 2000) proves the **data path**. It does **not** exercise the
> JDBC introspection surface a real BI tool drives — the first live QuickSight
> run surfaced a chain of driver-path bugs, all now fixed on `main` (see
> [QuickSight JDBC compatibility](#quicksight-jdbc-compatibility-fixed-2026-07-05)).
> Budget time for that, not just the AWS wiring.

> This runbook was executed end-to-end on 2026-07-05; the demo fixes were merged
> to `main` (PR #1). Every step below runs against `main` as-is.

### Prerequisites (verified on a real run)

- **AWS CLI v2** configured (`aws configure`) with a key whose IAM identity can
  create S3/IAM/EC2 resources. A brand-new IAM user has **no** permissions and
  every step fails with `AccessDenied` — attach **AdministratorAccess** to the
  user (IAM -> Users -> *user* -> Add permissions), fine for a throwaway demo.
  Delete the access key at teardown. Note `aws sts get-caller-identity` succeeds
  even with zero permissions, so it is not a permissions check — actually try
  `aws s3api create-bucket` (step 1).
  - A truncated secret shows up as `SignatureDoesNotMatch`; a valid AWS secret
    is exactly 40 chars.
- **GitHub CLI** (`gh`) installed and authenticated (`gh auth login`) with
  `repo` + `workflow` scopes — needed for steps 2-3. `brew install gh` on macOS.
- **Run each step in one shell** so the `export`ed vars persist.

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

The `publish` job runs **only** on `workflow_dispatch`, which exists on `main`,
so dispatch directly. (If you ever run this from a feature branch, that branch
must already be pushed with the `workflow_dispatch` trigger, and you pass
`--ref <branch>`; the OIDC trust uses `repo:$GH_REPO:*` so any branch is allowed.
A 422 `Workflow does not have 'workflow_dispatch' trigger` means the ref on
GitHub is an older version without it.)

```bash
gh workflow run semantic-layer.yml --repo $GH_REPO --ref main -f publish=true
gh run watch --repo $GH_REPO --exit-status   # or: gh run list --workflow semantic-layer.yml

# On success, confirm the two objects exist:
aws s3 ls $S3_PREFIX/manifest/
aws s3 ls $S3_PREFIX/warehouse/
#   manifest/VERSION   manifest/current.tar   warehouse/finance_demo.duckdb
```

> Two CI fixes made this go green (now on `main`): `DBT_PROJECT_DIR` collided
> with the step `working-directory` (dbt couldn't find `test_projects/finance_demo`),
> and the `validate` gate must pass before the `publish` job runs.

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

# Strongly recommended: attach SSM so you can read logs / run docker commands on
# the box WITHOUT an SSH key. The instance launches without a key pair (step 6),
# so SSM is the only remote channel. AL2023 ships the SSM agent; it needs this.
aws iam attach-role-policy --role-name flowproxy-ec2 \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
```

> Attach the SSM policy **before** launch. Attached after the instance is
> already running, the agent can take 5-15 min to register (IAM propagation);
> attached here it comes up SSM-visible in ~1 min.

## 5. Security group (open 5432 to QuickSight + 22 to you)

QuickSight (Standard) connects from AWS-published IP ranges for your region.
Look them up: [QuickSight IP ranges](https://docs.aws.amazon.com/quicksight/latest/user/regions.html).

```bash
export VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
export SG_ID=$(aws ec2 create-security-group --group-name flowproxy-demo \
  --description "FlowProxy demo" --vpc-id $VPC_ID --query GroupId --output text)
export MYIP=$(curl -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 22   --cidr $MYIP/32
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 5432 --cidr $MYIP/32

# REQUIRED for QuickSight: it connects from AWS-published data-source IP ranges,
# NOT your IP. Without this rule QuickSight fails with GENERIC_SQL_EXCEPTION /
# "The connection attempt failed". Range for us-east-1 (verify for other regions):
#   https://docs.aws.amazon.com/quicksight/latest/user/regions.html
aws ec2 authorize-security-group-ingress --group-id $SG_ID --protocol tcp --port 5432 --cidr 52.23.63.224/27
```

## 6. Launch EC2 and run FlowProxy

Amazon Linux 2023, t3.micro, Docker, with a user-data script that builds the
image from the repo and runs it in STORE mode.

> The clone below pulls the repo default branch (`main`), which now carries all
> the demo fixes. If you demo from a feature branch instead, use
> `git clone -b <branch> ...` or the container runs stale code. The build takes
> ~5-10 min on a t3.micro (1 GB RAM), so port 5432 is **not** reachable
> immediately after `instance-running`.

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
# Poll until the container finishes building and 5432 opens (can be 5-10 min):
for i in $(seq 1 30); do nc -z -w3 $PUBLIC_IP 5432 && { echo OPEN; break; }; sleep 20; done

# psql, or any Postgres client. No psql? python: psycopg2.connect("host=... sslmode=disable")
psql "host=$PUBLIC_IP port=5432 user=analyst password=demopass dbname=flowproxy sslmode=disable" \
  -c 'SELECT "account__region","account_balance" FROM "daily_balances" GROUP BY 1'
#  AMER | 3000.0
#  EMEA | 2000.0
```

### Debugging / iterating without SSH (via SSM)

The instance has no key pair, so use SSM `send-command` to read logs or rebuild
after pushing a fix. This is exactly how the demo was iterated.

```bash
# Read container logs (crash-loop, S3 download, "listening on :5432"):
aws ssm send-command --instance-ids $INSTANCE_ID --document-name AWS-RunShellScript \
  --parameters 'commands=["docker ps -a","docker logs --tail 60 flowproxy 2>&1"]' \
  --query 'Command.CommandId' --output text
# then: aws ssm get-command-invocation --command-id <id> --instance-id $INSTANCE_ID \
#       --query StandardOutputContent --output text

# Rebuild in place after pushing a fix (faster than relaunching the instance):
aws ssm send-command --instance-ids $INSTANCE_ID --document-name AWS-RunShellScript \
  --timeout-seconds 900 --parameters "commands=[
    \"docker rm -f flowproxy || true\",
    \"cd /opt/flowproxy && git fetch origin && git reset --hard origin/main\",
    \"docker build -t flowproxy .\",
    \"docker run -d --restart unless-stopped -p 5432:5432 --name flowproxy \
      -e FLOWPROXY_MANIFEST_URI=$S3_PREFIX/manifest/ \
      -e FLOWPROXY_DUCKDB_URI=$S3_PREFIX/warehouse/finance_demo.duckdb \
      -e FLOWPROXY_MANIFEST_POLL_INTERVAL=300 -e FLOWPROXY_PASSWORD=demopass \
      -e AWS_REGION=$AWS_REGION flowproxy\"
  ]" --query 'Command.CommandId' --output text
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
6. Build an analysis: use a **Table** visual. Put `account__region` in **Group
   by** and `account_balance` in **Value**. Expect **AMER 3000, EMEA 2000** -
   the end-of-period balances. Add `metric_time` at a monthly grain to see
   1500 / 1200 / 2000 for EMEA.

The point to demo: set `account_balance` to **Sum** in QuickSight's field menu.
The number does not change, because the aggregation lives in the metric
definition, not the SQL. That is the guardrail, visible.

Notes from the live run:
- The connection-validation step may flash `02000 / No results were returned` on
  a metadata probe even when the connection is fine; it does not block you.
- Preview **`all_metrics`** fails by design — it is a metrics-only cube and
  QuickSight's preview issues `SELECT *`, which the semantic layer rejects
  (`SELECT * is not supported on virtual cubes`). Demo with `daily_balances`.
- If the introspection chain works but the visual shows blank / `NULL`, it is the
  driver alias handling — fixed on `main` (result columns are labelled with the
  client's `SELECT ... AS` aliases). See the JDBC section below.

## 8. Teardown

### 8a. Stop the bill only (keep everything reusable)

The **only** resource that costs money is the EC2 instance. Terminate it to stop
billing while keeping the S3 bundle, IAM roles, OIDC provider, instance profile,
and security group — so you can relaunch step 6 in minutes.

```bash
aws ec2 terminate-instances --instance-ids $INSTANCE_ID
aws ec2 wait instance-terminated --instance-ids $INSTANCE_ID
# The auto-assigned public IP releases with the instance; no EIP to free.
# S3 (a few MB) and IAM/OIDC/SG cost effectively nothing.
```

### 8b. Full teardown (leave no trace)

```bash
aws ec2 delete-security-group --group-id $SG_ID
aws s3 rm $S3_PREFIX --recursive && aws s3api delete-bucket --bucket $BUCKET --region $AWS_REGION
aws iam remove-role-from-instance-profile --instance-profile-name flowproxy-ec2 --role-name flowproxy-ec2
aws iam delete-instance-profile --instance-profile-name flowproxy-ec2
aws iam detach-role-policy --role-name flowproxy-ec2 \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore   # added in step 4
aws iam delete-role-policy --role-name flowproxy-ec2 --policy-name s3-read
aws iam delete-role --role-name flowproxy-ec2
aws iam delete-role-policy --role-name flowproxy-ci-publish --policy-name s3-publish
aws iam delete-role --role-name flowproxy-ci-publish
# (leave the OIDC provider if other repos use it)
# Also remove the AdministratorAccess key you created for the demo IAM user.
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

## QuickSight JDBC compatibility (fixed 2026-07-05)

The first live QuickSight run exposed a chain of driver-path bugs the offline
rehearsal never hit — QuickSight's PostgreSQL JDBC driver drives a
metadata/introspection sequence the wire client does not. Each was fixed and is
now on `main`. The pattern to remember: **honor the client's exact SQL** (its
projection, aliases, and the specific system-catalog probes it issues), and
**every metadata probe must return the row shape the driver expects** — an empty
result where the driver needs >=1 row aborts the whole flow.

| Symptom in QuickSight | Root cause | Fix commit |
| --- | --- | --- |
| container crash-loop, never listens | image missing `s3fs`; entrypoint could not download the S3 sidecar (`ModuleNotFoundError: s3fs`) | `c268874` |
| `02000 No results` at validate; no schema dropdown | schema probe returned raw `pg_namespace` columns, ignoring `SELECT nspname AS schema_name` | `014b935` |
| schema probe Parsed but never completed | catalog probes not answered over the **extended** query protocol (Parse/Describe/Bind/Execute), only simple | `7fa529f` |
| schema shows but **no tables** | JDBC `getTables()` matched the `pg_namespace` handler (returned schemas), then returned 0 rows | `d741c3b`, `00b724b` |
| preview aborts on a column probe | JDBC `getColumns()` **windowed** variant (`SELECT * FROM (SELECT ... row_number() OVER ... pg_attribute ...)`) unhandled; also `pg_settings max_index_keys` returned 0 rows | `9dac90c`, `468fb0d` |
| visual renders `NULL NULL` | data-query result columns kept the semantic names instead of the client's `SELECT ... AS "<alias>"`; QuickSight reads results by its generated alias | `ff238bd` |

Diagnose with the container log line `catalog probe matched '<name>' -> N rows
(handled=True/False)` — `handled=False` marks an unanswered probe. Capture the
exact (untruncated) query the driver sends with a short `tcpdump -A port 5432` on
the box (via SSM), since the app log truncates long SQL at ~500 chars.

## Troubleshooting

- `AccessDenied` on `s3api create-bucket` / any IAM call: the CLI identity has no
  permissions. Attach **AdministratorAccess** to the IAM user (prerequisites).
  `aws sts get-caller-identity` succeeding does **not** mean you have permissions.
- `SignatureDoesNotMatch`: wrong/truncated secret key. A valid secret is 40 chars.
- `Workflow does not have 'workflow_dispatch' trigger` (HTTP 422): the workflow
  version on that ref lacks the trigger — dispatch against `main`, or push the
  branch that has it and use `--ref <branch>` (step 3).
- QuickSight `GENERIC_SQL_EXCEPTION` / "connection attempt failed": the security
  group does not allow the QuickSight IP range on 5432 (step 5). This is a
  network timeout, not auth.
- `Catalog "finance_demo" does not exist`: the sidecar was renamed. It must keep
  the `finance_demo.duckdb` basename (ADR-0016 decision 4). The entrypoint
  preserves it from the URI basename.
- Blank QuickSight schema/table pane, or `02000 No results`: an introspection
  probe was not answered for that column shape — see the JDBC section above.
- Connection refused: security group does not allow the source IP on 5432, or
  the image is still building (~5-10 min on t3.micro; watch `docker logs
  flowproxy` via SSM, not SSH — the instance has no key pair).
