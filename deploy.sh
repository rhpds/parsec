#!/bin/bash

# Parsec OpenShift Deployment Script
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

ENVIRONMENT="${1:-prod}"
DRY_RUN="${2:-false}"

if [ "${ENVIRONMENT}" = "dev" ]; then
    CONFIG_FILE="openshift/local/local-config-dev.yaml"
elif [ "${ENVIRONMENT}" = "prod" ]; then
    CONFIG_FILE="openshift/local/local-config.yaml"
else
    echo -e "${RED}Invalid environment: ${ENVIRONMENT}${NC}"
    echo "Usage: $0 [prod|dev] [dry-run]"
    exit 1
fi

echo -e "${BLUE}Parsec Deployment Script${NC}"
echo -e "${BLUE}========================${NC}"
echo -e "${YELLOW}Environment: ${ENVIRONMENT}${NC}"
echo -e "${YELLOW}Config File: ${CONFIG_FILE}${NC}"
echo ""

# Check required tools
for tool in oc yq; do
    if ! command -v $tool &> /dev/null; then
        echo -e "${RED}Required tool '$tool' is not installed${NC}"
        exit 1
    fi
done

# Check config file
if [ ! -f "$CONFIG_FILE" ]; then
    echo -e "${RED}Config file not found: $CONFIG_FILE${NC}"
    echo ""
    echo -e "${YELLOW}Create it from the template:${NC}"
    echo -e "  mkdir -p openshift/local"
    echo -e "  cp openshift/local-config.template.yaml $CONFIG_FILE"
    echo -e "  vi $CONFIG_FILE"
    exit 1
fi

# Load config
NAMESPACE=$(yq eval '.deployment.namespace' "$CONFIG_FILE")
CLUSTER_DOMAIN=$(yq eval '.deployment.cluster_domain' "$CONFIG_FILE")
IMAGE_REGISTRY=$(yq eval '.deployment.image_registry' "$CONFIG_FILE")
GIT_REPOSITORY=$(yq eval '.git.repository' "$CONFIG_FILE")
GIT_BRANCH=$(yq eval '.git.branch' "$CONFIG_FILE")
OAUTH_ENABLED=$(yq eval '.oauth.enabled // true' "$CONFIG_FILE")

echo -e "Namespace:      ${GREEN}${NAMESPACE}${NC}"
echo -e "Cluster Domain: ${GREEN}${CLUSTER_DOMAIN}${NC}"
echo -e "Repository:     ${GREEN}${GIT_REPOSITORY}${NC}"
echo -e "Branch:         ${GREEN}${GIT_BRANCH}${NC}"
echo -e "OAuth SSO:      ${GREEN}${OAUTH_ENABLED}${NC}"
echo -e "Dry Run:        ${GREEN}${DRY_RUN}${NC}"
echo ""

# Check OpenShift login
if ! oc whoami &> /dev/null; then
    echo -e "${RED}Not logged in to OpenShift${NC}"
    echo "Please login: oc login <cluster-url>"
    exit 1
fi
echo -e "${GREEN}Logged in as: $(oc whoami)${NC}"
echo ""

# Detect sed
if command -v gsed &> /dev/null; then
    SED_CMD="gsed -i"
elif [[ "$OSTYPE" == "darwin"* ]]; then
    SED_CMD="sed -i ''"
else
    SED_CMD="sed -i"
fi

# Determine overlay
if [ "${ENVIRONMENT}" = "dev" ]; then
    OVERLAY_DIR="openshift/overlays/dev"
else
    OVERLAY_DIR="openshift/overlays/prod"
fi

wait_for_deployment() {
    local name=$1
    local timeout=${2:-300}
    echo -e "${YELLOW}Waiting for deployment ${name}...${NC}"
    if oc rollout status deployment/${name} -n ${NAMESPACE} --timeout=${timeout}s; then
        echo -e "${GREEN}Deployment ${name} is ready${NC}"
    else
        echo -e "${RED}Deployment ${name} failed${NC}"
        return 1
    fi
}

create_secrets() {
    echo -e "${BLUE}Creating secrets...${NC}"

    # Provision DB
    local db_host=$(yq eval '.secrets.provision_db.host' "$CONFIG_FILE")
    local db_port=$(yq eval '.secrets.provision_db.port' "$CONFIG_FILE")
    local db_name=$(yq eval '.secrets.provision_db.database' "$CONFIG_FILE")
    local db_user=$(yq eval '.secrets.provision_db.user' "$CONFIG_FILE")
    local db_password=$(yq eval '.secrets.provision_db.password' "$CONFIG_FILE")

    oc create secret generic parsec-secrets -n ${NAMESPACE} \
        --from-literal=db-host="$db_host" \
        --from-literal=db-port="$db_port" \
        --from-literal=db-name="$db_name" \
        --from-literal=db-user="$db_user" \
        --from-literal=db-password="$db_password" \
        --dry-run=client -o yaml | oc apply -f -

    echo -e "${GREEN}parsec-secrets created${NC}"

    # Vertex AI credentials
    local vertex_sa_file=$(yq eval '.secrets.vertex.service_account_file' "$CONFIG_FILE")
    if [ -f "$vertex_sa_file" ]; then
        oc create secret generic vertex-credentials -n ${NAMESPACE} \
            --from-file=service-account.json="$vertex_sa_file" \
            --dry-run=client -o yaml | oc apply -f -
        echo -e "${GREEN}vertex-credentials created from ${vertex_sa_file}${NC}"
    else
        echo -e "${YELLOW}Vertex SA file not found: ${vertex_sa_file} -- skipping${NC}"
    fi

    # Cloud credentials (AWS, Azure, GCP billing)
    local aws_key=$(yq eval '.secrets.aws.access_key_id' "$CONFIG_FILE")
    local aws_secret=$(yq eval '.secrets.aws.secret_access_key' "$CONFIG_FILE")
    local aws_region=$(yq eval '.secrets.aws.region // "us-east-1"' "$CONFIG_FILE")

    local azure_client_id=$(yq eval '.secrets.azure.client_id' "$CONFIG_FILE")
    local azure_client_secret=$(yq eval '.secrets.azure.client_secret' "$CONFIG_FILE")
    local azure_tenant_id=$(yq eval '.secrets.azure.tenant_id' "$CONFIG_FILE")
    local azure_storage=$(yq eval '.secrets.azure.storage_account' "$CONFIG_FILE")
    local azure_container=$(yq eval '.secrets.azure.container' "$CONFIG_FILE")

    local gcp_project=$(yq eval '.secrets.gcp.project_id' "$CONFIG_FILE")
    local gcp_dataset=$(yq eval '.secrets.gcp.billing_dataset' "$CONFIG_FILE")
    local gcp_billing=$(yq eval '.secrets.gcp.billing_account_id' "$CONFIG_FILE")

    oc create secret generic parsec-cloud-credentials -n ${NAMESPACE} \
        --from-literal=PARSEC_AWS__ACCESS_KEY_ID="$aws_key" \
        --from-literal=PARSEC_AWS__SECRET_ACCESS_KEY="$aws_secret" \
        --from-literal=PARSEC_AWS__REGION="$aws_region" \
        --from-literal=PARSEC_AZURE__CLIENT_ID="$azure_client_id" \
        --from-literal=PARSEC_AZURE__CLIENT_SECRET="$azure_client_secret" \
        --from-literal=PARSEC_AZURE__TENANT_ID="$azure_tenant_id" \
        --from-literal=PARSEC_AZURE__STORAGE_ACCOUNT="$azure_storage" \
        --from-literal=PARSEC_AZURE__CONTAINER="$azure_container" \
        --from-literal=PARSEC_GCP__PROJECT_ID="$gcp_project" \
        --from-literal=PARSEC_GCP__BILLING_DATASET="$gcp_dataset" \
        --from-literal=PARSEC_GCP__BILLING_ACCOUNT_ID="$gcp_billing" \
        --dry-run=client -o yaml | oc apply -f -

    echo -e "${GREEN}parsec-cloud-credentials created${NC}"

    # GCP billing SA (separate from Vertex SA)
    local gcp_sa_file=$(yq eval '.secrets.gcp.service_account_file' "$CONFIG_FILE")
    if [ -f "$gcp_sa_file" ]; then
        oc create secret generic gcp-billing-credentials -n ${NAMESPACE} \
            --from-file=service-account.json="$gcp_sa_file" \
            --dry-run=client -o yaml | oc apply -f -
        echo -e "${GREEN}gcp-billing-credentials created${NC}"
    fi

    # Allowed users
    local allowed_users=$(yq eval '.auth.allowed_users // ""' "$CONFIG_FILE")
    oc create configmap parsec-allowed-users -n ${NAMESPACE} \
        --from-literal=allowed-users="$allowed_users" \
        --dry-run=client -o yaml | oc apply -f -

    echo -e "${GREEN}parsec-allowed-users configmap created${NC}"

    # Patch parsec-config configmap with cost-monitor dashboard URL
    local cm_dashboard_url=$(yq eval '.cost_monitor.dashboard_url // ""' "$CONFIG_FILE")
    if [ -n "$cm_dashboard_url" ]; then
        echo -e "${BLUE}Setting cost-monitor dashboard URL...${NC}"
        # Read existing config, inject cost_monitor.dashboard_url
        local existing_config=$(oc get configmap parsec-config -n ${NAMESPACE} -o jsonpath='{.data.config\.yaml}' 2>/dev/null || echo "")
        if [ -n "$existing_config" ]; then
            local updated_config=$(echo "$existing_config" | yq eval ".cost_monitor.dashboard_url = \"${cm_dashboard_url}\"" -)
            oc create configmap parsec-config -n ${NAMESPACE} \
                --from-literal=config.yaml="$updated_config" \
                --dry-run=client -o yaml | oc apply -f -
            echo -e "${GREEN}cost-monitor dashboard URL set: ${cm_dashboard_url}${NC}"
        fi
    fi

    # Webhook secrets
    local gh_secret=$(yq eval '.secrets.webhook.github_secret' "$CONFIG_FILE")
    local generic_secret=$(yq eval '.secrets.webhook.generic_secret' "$CONFIG_FILE")

    oc create secret generic parsec-webhook-secret -n ${NAMESPACE} \
        --from-literal=WebHookSecretKey="$gh_secret" \
        --dry-run=client -o yaml | oc apply -f -

    echo -e "${GREEN}All secrets created${NC}"
}

generate_oauth_secrets() {
    echo -e "${BLUE}Generating OAuth secrets...${NC}"

    local oauth_client_name="parsec-oauth-client"
    if [ "${ENVIRONMENT}" = "dev" ]; then
        oauth_client_name="parsec-oauth-client-dev"
    fi

    local oauth_client_secret=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-25)
    local cookie_secret=$(openssl rand -hex 16)

    export OAUTH_CLIENT_SECRET="$oauth_client_secret"

    oc create secret generic oauth-proxy-secret -n ${NAMESPACE} \
        --from-literal=client-id="$oauth_client_name" \
        --from-literal=client-secret="$oauth_client_secret" \
        --from-literal=cookie-secret="$cookie_secret" \
        --from-literal=session_secret="$cookie_secret" \
        --dry-run=client -o yaml | oc apply -f -

    echo -e "${GREEN}OAuth secrets generated for client: ${oauth_client_name}${NC}"
}

setup_oauth_client() {
    echo -e "${BLUE}Setting up OAuth client...${NC}"

    local oauth_client_name="parsec-oauth-client"
    if [ "${ENVIRONMENT}" = "dev" ]; then
        oauth_client_name="parsec-oauth-client-dev"
    fi

    # Clean up failed OAuth proxy pods
    oc delete pods -l component=oauth-proxy --field-selector=status.phase=Failed -n ${NAMESPACE} --ignore-not-found=true 2>/dev/null

    local max_attempts=30
    local attempt=0

    echo -e "${YELLOW}Waiting for OAuth client...${NC}"
    while [ $attempt -lt $max_attempts ]; do
        if oc get oauthclient "$oauth_client_name" &> /dev/null; then
            echo -e "${GREEN}OAuth client found: $oauth_client_name${NC}"
            break
        fi
        attempt=$((attempt + 1))
        if [ $((attempt % 5)) -eq 0 ]; then
            echo -e "${YELLOW}  Still waiting... ($attempt/$max_attempts)${NC}"
        fi
        sleep 3
    done

    if [ $attempt -ge $max_attempts ]; then
        echo -e "${RED}Failed to find OAuth client${NC}"
        return 1
    fi

    echo -e "${GREEN}OAuth client configured${NC}"
}

# --- Main deployment flow ---

if [ "${DRY_RUN}" = "true" ]; then
    echo -e "${YELLOW}[DRY RUN] Rendering kustomize output:${NC}"
    oc kustomize "$OVERLAY_DIR"
    echo ""
    echo -e "${YELLOW}[DRY RUN] No changes applied${NC}"
    exit 0
fi

# Create namespace
echo -e "${BLUE}Creating namespace...${NC}"
if oc get namespace ${NAMESPACE} &> /dev/null; then
    echo -e "${YELLOW}Namespace ${NAMESPACE} already exists${NC}"
else
    oc create namespace ${NAMESPACE}
    echo -e "${GREEN}Created namespace: ${NAMESPACE}${NC}"
fi
echo ""

# Create secrets
create_secrets
echo ""

# OAuth setup
if [ "${OAUTH_ENABLED}" = "true" ]; then
    generate_oauth_secrets
    echo ""
fi

# Apply kustomize overlay
echo -e "${BLUE}Deploying Parsec...${NC}"
oc apply -k "$OVERLAY_DIR"
echo -e "${GREEN}Resources applied${NC}"
echo ""

# Patch BuildConfig with a real webhook secret (inline, not secretReference)
echo -e "${BLUE}Configuring webhook...${NC}"
WEBHOOK_SECRET=$(openssl rand -base64 20 | tr -d "=+/" | cut -c1-20)
oc patch bc parsec -n ${NAMESPACE} --type=json -p "[
  {\"op\": \"replace\", \"path\": \"/spec/triggers/1\", \"value\": {\"type\": \"GitHub\", \"github\": {\"secret\": \"${WEBHOOK_SECRET}\"}}},
  {\"op\": \"replace\", \"path\": \"/spec/triggers/2\", \"value\": {\"type\": \"Generic\", \"generic\": {\"secret\": \"${WEBHOOK_SECRET}\"}}}
]"
WEBHOOK_URL="https://$(oc get infrastructure cluster -o jsonpath='{.status.apiServerURL}' 2>/dev/null | sed 's|https://||')/apis/build.openshift.io/v1/namespaces/${NAMESPACE}/buildconfigs/parsec/webhooks/${WEBHOOK_SECRET}/github"
echo -e "${GREEN}Webhook secret configured${NC}"
echo ""

# Trigger initial build
echo -e "${BLUE}Triggering initial build...${NC}"
oc start-build parsec -n ${NAMESPACE}
echo -e "${GREEN}Build started${NC}"
echo ""

# Wait for deployment
wait_for_deployment "parsec" 300
echo ""

# OAuth client setup
if [ "${OAUTH_ENABLED}" = "true" ]; then
    if ! setup_oauth_client; then
        echo -e "${RED}OAuth client setup failed${NC}"
        exit 1
    fi
    echo ""
    wait_for_deployment "oauth-proxy" 300
    echo ""
fi

# Status
echo -e "${BLUE}Deployment Status:${NC}"
oc get pods -n ${NAMESPACE}
echo ""
echo -e "${BLUE}Routes:${NC}"
oc get routes -n ${NAMESPACE}
echo ""

# URLs
PARSEC_URL=$(oc get route parsec-route -n ${NAMESPACE} -o jsonpath='{.spec.host}' 2>/dev/null || echo "parsec-${NAMESPACE}.${CLUSTER_DOMAIN}")
echo -e "${GREEN}Deployment complete!${NC}"
echo ""
echo -e "${BLUE}Parsec URL: https://${PARSEC_URL}${NC}"
echo ""
echo -e "${BLUE}Next steps:${NC}"
echo -e "  1. Open https://${PARSEC_URL} (login with OpenShift credentials)"
echo -e "  2. Add GitHub webhook for auto-builds:"
echo -e "     URL: ${WEBHOOK_URL}"
echo -e "     Content type: application/json"
echo -e "     Secret: (leave blank)"
echo -e "     Events: Just the push event"
echo -e "  3. Update allowed users:"
echo -e "     oc edit configmap parsec-allowed-users -n ${NAMESPACE}"
echo -e "  4. View logs:"
echo -e "     oc logs -f deployment/parsec -n ${NAMESPACE}"
echo -e "     oc logs -f deployment/oauth-proxy -n ${NAMESPACE}"
