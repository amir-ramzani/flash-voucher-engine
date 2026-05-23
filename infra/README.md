# Infrastructure (CloudFormation)

| Template | What |
|---|---|
| `ecr.yaml` | ECR repository for the single app image |
| `stack.yaml` | DynamoDB tables, ElastiCache Redis 7 (primary + replica, Multi-AZ, TLS), ECS Fargate cluster with `api` (behind an ALB) and `worker` services, reconciler on an EventBridge schedule, EMF-based dashboard + alarms |
| `fis.yaml` | Fault Injection Service experiments: kill all workers; AZ power interruption → Redis failover |

Prerequisites: an existing VPC with ≥ 2 public and ≥ 2 private subnets (private ones need
NAT or VPC endpoints for ECR/Logs/DynamoDB), and the Cognito user pool + app client from the
multitenant OAuth project, with an `admin` group.

```bash
REGION=ap-southeast-1
aws cloudformation deploy --region $REGION --stack-name flash-voucher-ecr --template-file infra/ecr.yaml
REPO=$(aws cloudformation describe-stacks --region $REGION --stack-name flash-voucher-ecr \
  --query "Stacks[0].Outputs[?OutputKey=='RepositoryUri'].OutputValue" --output text)

aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin ${REPO%/*}
TAG=$(git rev-parse --short HEAD)
docker buildx build --platform linux/arm64 -t $REPO:$TAG --push .

aws cloudformation deploy --region $REGION --stack-name flash-voucher \
  --template-file infra/stack.yaml --capabilities CAPABILITY_IAM \
  --parameter-overrides VpcId=vpc-... PublicSubnetIds=subnet-a,subnet-b \
    PrivateSubnetIds=subnet-c,subnet-d ImageUri=$REPO:$TAG \
    CognitoUserPoolId=ap-southeast-1_XXXX CognitoClientId=xxxx

aws cloudformation deploy --region $REGION --stack-name flash-voucher-fis \
  --template-file infra/fis.yaml --capabilities CAPABILITY_IAM \
  --parameter-overrides AppStackName=flash-voucher AzToInterrupt=ap-southeast-1a
```

Validate locally with `cfn-lint infra/*.yaml`.

## Load testing in AWS

The deployed API runs `AUTH_MODE=cognito`, so the dev-token load tests won't authenticate.
Options: deploy a separate load-test stack with `AUTH_MODE=dev` (and a strong
`DEV_JWT_SECRET` passed as a secret, never publicly reachable), or pre-provision Cognito
test users. The first is simpler; don't run it on a public listener.

## Open items / verify before first deploy

- [ ] HTTPS listener + ACM certificate (the template only has HTTP :80).
- [ ] `WAIT` support on ElastiCache with your engine version (`REDIS_WAIT_REPLICAS=1`); set to 0 if unsupported.
- [ ] FIS action ids (`aws fis list-actions`) — ElastiCache action naming has changed over time.
- [ ] API autoscaling (target tracking on ALB request count) — not included yet.
- [ ] Cost: ElastiCache + NAT + ALB run 24/7 — tear down between sessions (`aws cloudformation delete-stack`; DynamoDB tables are retained).
