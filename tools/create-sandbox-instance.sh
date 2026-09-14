#!/usr/bin/env bash
# Create a sandbox Amazon Connect instance wired for the ciopulse Connect quickstart.
#
#   tools/create-sandbox-instance.sh [alias] [region]
#
# Creates: a Connect instance (Connect-managed users), an S3 bucket for
# recordings, chat transcripts and analytics output, the storage associations,
# Contact Lens enabled at instance level, and EventBridge notifications on the
# bucket. It does NOT create a contact flow, claim a phone number, or store an
# API key; those steps are in docs/sandbox-instance.md.
#
# Safe to re-run: every step checks before it creates.
set -euo pipefail

ALIAS="${1:-ciopulse-sandbox}"
REGION="${2:-ap-southeast-2}"
export AWS_DEFAULT_REGION="$REGION"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${BUCKET:-${ALIAS}-connect-${ACCOUNT}}"
PREFIX="connect/${ALIAS}"

say() { printf '\n==> %s\n' "$*"; }

# ---------------------------------------------------------------- instance
say "Connect instance '${ALIAS}' in ${REGION} (account ${ACCOUNT})"
INSTANCE_ID=$(aws connect list-instances \
  --query "InstanceSummaryList[?InstanceAlias=='${ALIAS}'].Id | [0]" --output text)
if [[ "$INSTANCE_ID" == "None" || -z "$INSTANCE_ID" ]]; then
  INSTANCE_ID=$(aws connect create-instance \
    --identity-management-type CONNECT_MANAGED \
    --instance-alias "$ALIAS" \
    --inbound-calls-enabled --outbound-calls-enabled \
    --query Id --output text)
  echo "created ${INSTANCE_ID}"
else
  echo "exists ${INSTANCE_ID}"
fi

say "waiting for the instance to become ACTIVE (usually 1-3 minutes)"
for _ in $(seq 1 60); do
  STATUS=$(aws connect describe-instance --instance-id "$INSTANCE_ID" \
    --query Instance.InstanceStatus --output text)
  [[ "$STATUS" == "ACTIVE" ]] && break
  [[ "$STATUS" == "CREATION_FAILED" ]] && { echo "instance creation failed"; exit 1; }
  sleep 10
done
echo "status ${STATUS}"
INSTANCE_ARN=$(aws connect describe-instance --instance-id "$INSTANCE_ID" --query Instance.Arn --output text)

# ---------------------------------------------------------------- bucket
say "S3 bucket ${BUCKET}"
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "exists"
else
  aws s3api create-bucket --bucket "$BUCKET" \
    --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  echo "created"
fi

say "EventBridge notifications on the bucket (fresh bucket, so replacing the config is safe)"
aws s3api put-bucket-notification-configuration --bucket "$BUCKET" \
  --notification-configuration '{"EventBridgeConfiguration": {}}'
echo "enabled"

# ---------------------------------------------------------------- storage associations
associate() {  # <resource-type> <sub-prefix>
  local type="$1" sub="$2"
  local existing
  existing=$(aws connect list-instance-storage-configs --instance-id "$INSTANCE_ID" \
    --resource-type "$type" --query 'StorageConfigs[0].AssociationId' --output text)
  if [[ "$existing" != "None" && -n "$existing" ]]; then
    echo "${type}: already associated (${existing})"
    return
  fi
  aws connect associate-instance-storage-config --instance-id "$INSTANCE_ID" \
    --resource-type "$type" \
    --storage-config "StorageType=S3,S3Config={BucketName=${BUCKET},BucketPrefix=${PREFIX}/${sub}}" \
    --query AssociationId --output text | sed "s/^/${type}: associated /"
}
say "storage associations under s3://${BUCKET}/${PREFIX}/"
associate CALL_RECORDINGS  CallRecordings
associate CHAT_TRANSCRIPTS ChatTranscripts

# ---------------------------------------------------------------- instance attributes
say "instance attributes"
aws connect update-instance-attribute --instance-id "$INSTANCE_ID" --attribute-type CONTACT_LENS --value true
aws connect update-instance-attribute --instance-id "$INSTANCE_ID" --attribute-type CONTACTFLOW_LOGS --value true
echo "CONTACT_LENS=true CONTACTFLOW_LOGS=true"

# ---------------------------------------------------------------- summary
ACCESS_URL=$(aws connect describe-instance --instance-id "$INSTANCE_ID" --query Instance.InstanceAccessUrl --output text 2>/dev/null || echo "https://${ALIAS}.my.connect.aws")
cat <<SUMMARY

Done. Values for 'sam deploy --guided':

  TranscriptBucketName = ${BUCKET}
  ConnectInstanceArn   = ${INSTANCE_ARN}
  VoiceKeyPattern      = *Analysis/Voice/Redacted/*.json   (default)
  ChatKeyPattern       = *Analysis/Chat/Redacted/*.json    (default)

Admin console: ${ACCESS_URL}

Still to do by hand (docs/sandbox-instance.md):
  1. Create the API-key secret:
       aws secretsmanager create-secret --name ciopulse/api-key --secret-string '<your key>'
  2. Create an admin user in the instance (or use the emergency login link on the instance page).
  3. In the flow designer, add 'Set recording and analytics behavior' with analytics ON,
     redaction ON, RedactedOnly, to the inbound flow; then run a test chat.
  4. Optional: claim an SMS-capable phone number for voice tests.
SUMMARY
