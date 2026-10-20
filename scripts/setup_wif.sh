#!/usr/bin/env bash
# Keyless Google login for GitHub Actions (Workload Identity Federation).
# Run in Google Cloud Shell:
#   curl -fsSL https://raw.githubusercontent.com/hamidmalik99/designcrowdauto/main/scripts/setup_wif.sh | bash -s YOUR_PROJECT_ID
# Safe to re-run: steps that already exist are skipped.
set -uo pipefail

PROJECT_ID="${1:-$(gcloud config get-value project 2>/dev/null)}"
SA="${SA:-outreach-bot}"
REPO="${REPO:-hamidmalik99/designcrowdauto}"
[ -z "$PROJECT_ID" ] && { echo "Usage: bash setup_wif.sh PROJECT_ID"; exit 1; }
SA_EMAIL="$SA@$PROJECT_ID.iam.gserviceaccount.com"

step() { echo; echo "==> $1"; }
ok_or_exists() { # run a command; treat "already exists" as success
  local out; out=$("$@" 2>&1); local rc=$?
  if [ $rc -eq 0 ]; then echo "done"; elif echo "$out" | grep -qi "already exists"; then echo "already exists, skipped";
  else echo "$out"; echo "FAILED: $*"; exit 1; fi
}

step "Using project $PROJECT_ID"
gcloud config set project "$PROJECT_ID" >/dev/null 2>&1 || { echo "Can't select project $PROJECT_ID"; exit 1; }
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)') || exit 1

step "Enabling APIs (Sheets, Drive, IAM Credentials, STS)"
ok_or_exists gcloud services enable sheets.googleapis.com drive.googleapis.com iamcredentials.googleapis.com sts.googleapis.com

step "Service account $SA_EMAIL"
ok_or_exists gcloud iam service-accounts create "$SA" --display-name="Outreach bot"

step "Workload identity pool 'github'"
ok_or_exists gcloud iam workload-identity-pools create github --location=global --display-name="GitHub"

step "GitHub provider (only $REPO may log in)"
ok_or_exists gcloud iam workload-identity-pools providers create-oidc github-repo --location=global \
  --workload-identity-pool=github --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='$REPO'"

step "Allowing that repo to act as the service account"
ok_or_exists gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"

echo
echo "================ ALL DONE ================"
echo "GitHub repo > Settings > Secrets and variables > Actions > Variables tab:"
echo "  GCP_WIF_PROVIDER    = projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/github-repo"
echo "  GCP_SERVICE_ACCOUNT = $SA_EMAIL"
echo "Share the Google Sheet with $SA_EMAIL as Editor."
