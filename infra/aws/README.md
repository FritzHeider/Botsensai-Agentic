# Botsensai AWS Infrastructure

This directory contains the AWS deployment resources and configuration for Botsensai.

## Contents

- `cloudformation.yaml`: CloudFormation template provisioning an EC2 instance (`t3.medium`, Ubuntu 24.04), security groups, and automated bootstrap.
- `userdata.sh`: Standalone EC2 bootstrap script for installing Python 3.11+, Playwright browser binaries, Botsensai, and setting up daily cron tasks.

## Deployment via CloudFormation

Deploy the stack via the AWS CLI:

```bash
aws cloudformation create-stack \
  --stack-name botsensai-production \
  --template-body file://infra/aws/cloudformation.yaml \
  --parameters \
      ParameterKey=KeyName,ParameterValue=<your-keypair-name> \
      ParameterKey=InstanceType,ParameterValue=t3.medium \
      ParameterKey=RepositoryUrl,ParameterValue=https://github.com/FritzHeider/Botsensai-Agentic.git \
  --region us-east-1 \
  --profile agent-toolkit
```

## Connecting via AWS SSM Session Manager

To open an interactive terminal session into the instance without needing open inbound SSH ports:

```bash
aws ssm start-session \
  --target <instance-id> \
  --region us-east-1 \
  --profile agent-toolkit
```

## Automated Maintenance Tasks

The bootstrap script configures the following daily crons under the `ubuntu` user:

- `0 2 * * *`: Outcome labeling (`botsensai label --min-age-hours 24`)
- `0 3 * * *`: Model refitting (`botsensai fit --min-samples 200`)
- `0 4 * * *`: Track record update (`botsensai track-record --out docs/TRACK_RECORD.md`)
