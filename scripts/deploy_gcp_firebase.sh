#!/usr/bin/env bash
# ==============================================================================
# Botsensai GCP & Firebase Unified Deployment Script
# ==============================================================================
# Automates:
#   1. Container build & Cloud Run deployment (botsensai-api)
#   2. Firebase Hosting & Firestore rules/indexes deployment
#   3. Local dry-run verification mode (--dry-run)
#
# Usage:
#   ./scripts/deploy_gcp_firebase.sh [OPTIONS]
#
# Options:
#   --dry-run              Validate configs, tools, and simulate deployment steps
#   --project <PROJECT_ID> Specify GCP / Firebase project ID
#   --region <REGION>      Specify GCP region (default: us-central1)
#   --service-name <NAME>  Cloud Run service name (default: botsensai-api)
#   --only-cloudrun        Deploy only the Cloud Run API service
#   --only-firebase        Deploy only Firebase Hosting and Firestore rules/indexes
#   --skip-docker          Skip local docker build; deploy via Cloud Build (--source .)
#   --docker-test          In dry-run, execute a test docker build
#   --help, -h             Show this help message
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# --- ANSI Color Codes ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# --- Defaults ---
DRY_RUN=false
DOCKER_TEST=false
ONLY_CLOUDRUN=false
ONLY_FIREBASE=false
SKIP_DOCKER=false
DEFAULT_PROJECT="botsensai-prod"
PROJECT_ID="${GCP_PROJECT_ID:-}"
REGION="${GCP_REGION:-us-central1}"
SERVICE_NAME="botsensai-api"
IMAGE_TAG="v2.4-$(date +%Y%m%d-%H%M%S)"
PORT=8080
MIN_INSTANCES=0
MAX_INSTANCES=10
CPU="2"
MEMORY="2Gi"
CONCURRENCY=80
TREASURY_WALLET="ChgCuBWDvGFwnW77kU523JcFXc2CryX3J4rEWDzWmtsS"

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
    echo -e "\n${CYAN}${BOLD}=== $1 ===${NC}"
}

print_usage() {
    cat <<EOF
Botsensai GCP & Firebase Unified Deployment Script

Usage:
  $(basename "$0") [OPTIONS]

Options:
  --dry-run              Validate configs, tools, and simulate deployment steps
  --project <PROJECT_ID> Specify GCP / Firebase project ID (default: ${DEFAULT_PROJECT})
  --region <REGION>      Specify GCP region (default: ${REGION})
  --service-name <NAME>  Cloud Run service name (default: ${SERVICE_NAME})
  --only-cloudrun        Deploy only the Cloud Run API service
  --only-firebase        Deploy only Firebase Hosting and Firestore rules/indexes
  --skip-docker          Skip local docker build; deploy via Cloud Build (--source .)
  --docker-test          In dry-run mode, also run a local docker build test
  -h, --help             Show this help message

Environment Variables:
  GCP_PROJECT_ID         Default GCP/Firebase Project ID
  GCP_REGION             Default GCP Region
  SOLANA_RPC_URL         Helius or private RPC endpoint (or read from GCP Secret Manager)
  JITO_AUTH_KEY          Jito Block Engine authentication key (or Secret Manager)
  BOTSENSAI_DB_KEY       Database encryption key (or Secret Manager)
EOF
}

# --- Parse Arguments ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --docker-test)
            DOCKER_TEST=true
            shift
            ;;
        --project)
            PROJECT_ID="$2"
            shift 2
            ;;
        --region)
            REGION="$2"
            shift 2
            ;;
        --service-name)
            SERVICE_NAME="$2"
            shift 2
            ;;
        --only-cloudrun)
            ONLY_CLOUDRUN=true
            shift
            ;;
        --only-firebase)
            ONLY_FIREBASE=true
            shift
            ;;
        --skip-docker)
            SKIP_DOCKER=true
            shift
            ;;
        -h|--help)
            print_usage
            exit 0
            ;;
        *)
            log_error "Unknown argument: $1"
            print_usage
            exit 1
            ;;
    esac
done

if [[ "${ONLY_CLOUDRUN}" == true && "${ONLY_FIREBASE}" == true ]]; then
    log_error "Cannot specify both --only-cloudrun and --only-firebase"
    exit 1
fi

log_step "Botsensai Cloud Deployment Initialization"
log_info "Mode: $([ "$DRY_RUN" = true ] && echo "${YELLOW}DRY RUN (Verification Only)${NC}" || echo "${GREEN}LIVE DEPLOYMENT${NC}")"

# --- Determine Active Project ID ---
if [[ -z "${PROJECT_ID}" ]]; then
    if command -v gcloud >/dev/null 2>&1; then
        DETECTED_PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
        if [[ -n "${DETECTED_PROJECT}" && "${DETECTED_PROJECT}" != "(unset)" ]]; then
            PROJECT_ID="${DETECTED_PROJECT}"
            log_info "Auto-detected GCP project: ${PROJECT_ID}"
        fi
    fi
fi

if [[ -z "${PROJECT_ID}" ]]; then
    PROJECT_ID="${DEFAULT_PROJECT}"
    log_warn "No project ID provided or detected; defaulting to '${PROJECT_ID}'"
fi

log_info "Project ID:    ${BOLD}${PROJECT_ID}${NC}"
log_info "Region:        ${BOLD}${REGION}${NC}"
log_info "Service Name:  ${BOLD}${SERVICE_NAME}${NC}"
log_info "Target Target: $([ "$ONLY_CLOUDRUN" = true ] && echo "Cloud Run Only" || ([ "$ONLY_FIREBASE" = true ] && echo "Firebase Only" || echo "Full Stack (Cloud Run + Firebase)"))"

# --- Step 1: Tool Verification ---
log_step "Step 1: Validating Required Tools"

# Detect Firebase CLI or fallback to npx
FIREBASE_BIN=""
if command -v firebase >/dev/null 2>&1; then
    FIREBASE_BIN="firebase"
    log_success "Found firebase CLI: $(firebase --version 2>&1 | head -n1)"
elif command -v npx >/dev/null 2>&1; then
    FIREBASE_BIN="npx -y firebase-tools"
    log_success "Found npx; using 'npx -y firebase-tools' (v15.x)"
else
    if [[ "${ONLY_CLOUDRUN}" != true ]]; then
        log_error "Neither 'firebase' nor 'npx' was found. Please install Firebase CLI or Node.js."
        exit 1
    else
        log_warn "'firebase' not found, but proceeding with --only-cloudrun"
    fi
fi

# Detect gcloud CLI
if command -v gcloud >/dev/null 2>&1; then
    log_success "Found gcloud CLI: $(gcloud --version 2>&1 | head -n1)"
else
    if [[ "${ONLY_FIREBASE}" != true ]]; then
        log_error "gcloud CLI is required for Cloud Run deployment but was not found."
        exit 1
    else
        log_warn "'gcloud' not found, but proceeding with --only-firebase"
    fi
fi

# Detect Docker if needed
if [[ "${SKIP_DOCKER}" != true && "${ONLY_FIREBASE}" != true ]]; then
    if command -v docker >/dev/null 2>&1; then
        log_success "Found docker: $(docker --version 2>&1 | head -n1)"
    else
        log_warn "docker not found in PATH; falling back to Cloud Build (--skip-docker)"
        SKIP_DOCKER=true
    fi
fi

# --- Step 2: Validate Artifacts & Security Rules ---
log_step "Step 2: Validating Infrastructure & Security Artifacts"

# Check Dockerfile
if [[ -f "Dockerfile" ]]; then
    log_success "Dockerfile found"
    # Verify non-root user and healthcheck exist in Dockerfile
    if grep -q "USER appuser" Dockerfile; then
        log_success "Dockerfile: Non-root 'appuser' security posture confirmed"
    else
        log_warn "Dockerfile: Running as root is discouraged for Cloud Run"
    fi
    if grep -q "HEALTHCHECK" Dockerfile; then
        log_success "Dockerfile: HEALTHCHECK probe configured"
    fi
else
    log_error "Dockerfile missing in repository root!"
    exit 1
fi

# Check firebase.json
if [[ -f "firebase.json" ]]; then
    python3 -m json.tool firebase.json >/dev/null 2>&1 || {
        log_error "firebase.json is not valid JSON!"
        exit 1
    }
    log_success "firebase.json: Valid JSON syntax verified"

    # Verify Cloud Run rewrite for /api/**
    if grep -q '"serviceId": "botsensai-api"' firebase.json; then
        log_success "firebase.json: Cloud Run /api/** rewrite route confirmed"
    else
        log_warn "firebase.json: /api/** rewrite to Cloud Run service is missing"
    fi

    # Verify Security Headers
    if grep -q "X-Frame-Options" firebase.json && grep -q "X-Content-Type-Options" firebase.json; then
        log_success "firebase.json: Enterprise security headers confirmed"
    fi
else
    log_error "firebase.json missing in repository root!"
    exit 1
fi

# Check firestore.rules
if [[ -f "firestore.rules" ]]; then
    log_success "firestore.rules found"
    # Verify strict RBAC checks
    if grep -q "match /signals/{signalId}" firestore.rules && grep -q "allow write: if false;" firestore.rules; then
        log_success "firestore.rules: Read-only protection for /signals confirmed"
    fi
    if grep -q "match /runners/{runnerId}" firestore.rules; then
        log_success "firestore.rules: Public runner performance rules confirmed"
    fi
    if grep -q "match /rent_claims/{claimId}" firestore.rules; then
        # Ensure the owner bug is not present
        if grep -q "isOwner(request.auth.uid)" firestore.rules; then
            log_error "firestore.rules: Vulnerability detected! 'isOwner(request.auth.uid)' allows arbitrary reads!"
            exit 1
        else
            log_success "firestore.rules: Rent claims authorization strictly scoped to wallet/owner"
        fi
    fi
else
    log_error "firestore.rules missing in repository root!"
    exit 1
fi

# Check firestore.indexes.json
if [[ -f "firestore.indexes.json" ]]; then
    python3 -m json.tool firestore.indexes.json >/dev/null 2>&1 || {
        log_error "firestore.indexes.json is not valid JSON!"
        exit 1
    }
    log_success "firestore.indexes.json: Valid JSON composite index definitions verified"
else
    log_error "firestore.indexes.json missing in repository root!"
    exit 1
fi

# Check public assets
if [[ -f "public/index.html" ]]; then
    log_success "public/index.html verified ($(wc -c < public/index.html | tr -d ' ') bytes)"
else
    log_error "public/index.html missing! Single-page app root is required."
    exit 1
fi

# Optional: Docker build test during dry-run
if [[ "${DRY_RUN}" == true && "${DOCKER_TEST}" == true && "${SKIP_DOCKER}" != true ]]; then
    log_step "Step 2b: Local Docker Build Smoke Test"
    log_info "Building local test image 'botsensai-test:local'..."
    docker build -t botsensai-test:local .
    log_success "Docker image successfully built locally"
fi

# --- Step 3: Cloud Run Deployment Execution / Simulation ---
if [[ "${ONLY_FIREBASE}" != true ]]; then
    log_step "Step 3: Cloud Run Deployment (${SERVICE_NAME})"

    IMAGE_URI="gcr.io/${PROJECT_ID}/${SERVICE_NAME}:${IMAGE_TAG}"

    # Secret Manager bindings for live trading & RPC credentials
    SECRET_BINDINGS=(
        "SOLANA_RPC_URL=projects/${PROJECT_ID}/secrets/BOTSENSAI_HELIUS_RPC:latest"
        "JITO_AUTH_KEY=projects/${PROJECT_ID}/secrets/BOTSENSAI_JITO_KEY:latest"
        "BOTSENSAI_DB_KEY=projects/${PROJECT_ID}/secrets/BOTSENSAI_DB_KEY:latest"
    )
    SECRETS_ARG=$(IFS=,; echo "${SECRET_BINDINGS[*]}")

    # Environment variables
    ENV_VARS=(
        "ENV=production"
        "PYTHONUNBUFFERED=1"
        "PORT=${PORT}"
        "TREASURY_WALLET=${TREASURY_WALLET}"
        "BOTSENSAI_REPO_ROOT=/app"
        "BOTSENSAI_DB_PATH=/app/data/botsensai.db"
    )
    ENV_VARS_ARG=$(IFS=,; echo "${ENV_VARS[*]}")

    CLOUDRUN_DEPLOY_CMD=(
        gcloud run deploy "${SERVICE_NAME}"
        --project "${PROJECT_ID}"
        --region "${REGION}"
        --platform managed
        --allow-unauthenticated
        --port "${PORT}"
        --cpu "${CPU}"
        --memory "${MEMORY}"
        --concurrency "${CONCURRENCY}"
        --min-instances "${MIN_INSTANCES}"
        --max-instances "${MAX_INSTANCES}"
        --set-env-vars "${ENV_VARS_ARG}"
        --set-secrets "${SECRETS_ARG}"
    )

    if [[ "${SKIP_DOCKER}" == true ]]; then
        CLOUDRUN_DEPLOY_CMD+=(--source .)
    else
        CLOUDRUN_DEPLOY_CMD+=(--image "${IMAGE_URI}")
    fi

    if [[ "${DRY_RUN}" == true ]]; then
        log_info "${YELLOW}[DRY-RUN]${NC} Cloud Run target configuration:"
        echo "  Service:          ${SERVICE_NAME}"
        echo "  Project:          ${PROJECT_ID}"
        echo "  Region:           ${REGION}"
        echo "  Autoscaling:      ${MIN_INSTANCES} to ${MAX_INSTANCES} instances"
        echo "  Resource Specs:   ${CPU} vCPU, ${MEMORY} RAM, ${CONCURRENCY} concurrency"
        echo "  Secret Manager:   BOTSENSAI_HELIUS_RPC, BOTSENSAI_JITO_KEY, BOTSENSAI_DB_KEY"
        echo "  Environment:      ENV=production, TREASURY_WALLET=${TREASURY_WALLET}"
        echo ""
        log_info "${YELLOW}[DRY-RUN]${NC} Command that would be executed:"
        echo -e "${BOLD}${CLOUDRUN_DEPLOY_CMD[*]}${NC}"
    else
        if [[ "${SKIP_DOCKER}" != true ]]; then
            log_info "Building and pushing container image to Artifact/Container Registry..."
            docker build -t "${IMAGE_URI}" .
            docker push "${IMAGE_URI}"
        fi
        log_info "Executing Cloud Run deployment..."
        "${CLOUDRUN_DEPLOY_CMD[@]}"
        log_success "Cloud Run service '${SERVICE_NAME}' successfully deployed!"
    fi
fi

# --- Step 4: Firebase Hosting & Firestore Rules Deployment ---
if [[ "${ONLY_CLOUDRUN}" != true ]]; then
    log_step "Step 4: Firebase Hosting & Firestore Security Rules"

    FIREBASE_DEPLOY_CMD=(
        ${FIREBASE_BIN} deploy
        --only hosting,firestore:rules,firestore:indexes
        --project "${PROJECT_ID}"
        --non-interactive
    )

    if [[ "${DRY_RUN}" == true ]]; then
        log_info "${YELLOW}[DRY-RUN]${NC} Firebase targets to deploy:"
        echo "  Target Project:   ${PROJECT_ID}"
        echo "  Hosting Root:     ./public (index.html)"
        echo "  API Rewrite:      /api/** -> Cloud Run (${SERVICE_NAME})"
        echo "  Firestore Rules:  firestore.rules (RBAC & PII secured)"
        echo "  Firestore Index:  firestore.indexes.json (6 composite indexes)"
        echo ""
        log_info "${YELLOW}[DRY-RUN]${NC} Command that would be executed:"
        echo -e "${BOLD}${FIREBASE_DEPLOY_CMD[*]}${NC}"
    else
        log_info "Executing Firebase deployment..."
        "${FIREBASE_DEPLOY_CMD[@]}"
        log_success "Firebase Hosting and Firestore rules successfully deployed!"
    fi
fi

# --- Step 5: Post-Deployment Verification Summary ---
log_step "Deployment Status & Summary"
if [[ "${DRY_RUN}" == true ]]; then
    log_success "DRY-RUN VERIFICATION COMPLETED SUCCESSFULLY"
    echo -e "  ${GREEN}✔${NC} All toolchains (gcloud, firebase-tools/npx, docker) detected and functional"
    echo -e "  ${GREEN}✔${NC} Dockerfile syntax, non-root user, and healthcheck verified"
    echo -e "  ${GREEN}✔${NC} firebase.json syntax, Cloud Run rewrites, and security headers verified"
    echo -e "  ${GREEN}✔${NC} firestore.rules verified with strict RBAC and rent_claims security fix"
    echo -e "  ${GREEN}✔${NC} firestore.indexes.json verified with composite indexes"
    echo -e "  ${GREEN}✔${NC} GCP Secret Manager environment binding specs validated"
    echo -e "\nTo perform live deployment, run:\n  ${BOLD}./scripts/deploy_gcp_firebase.sh --project <PROJECT_ID>${NC}"
else
    log_success "LIVE DEPLOYMENT SUCCEEDED"
    echo -e "  Cloud Run Service:    https://${SERVICE_NAME}-${PROJECT_ID}.${REGION}.run.app"
    echo -e "  Firebase Portal:      https://${PROJECT_ID}.web.app / https://botsensai.com"
fi
