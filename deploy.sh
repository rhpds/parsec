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

wait_for_build() {
    local build_name=$1
    local timeout=${2:-300}
    echo -e "${YELLOW}Waiting for build ${build_name}...${NC}"
    local elapsed=0
    while [ $elapsed -lt $timeout ]; do
        local phase=$(oc get build ${build_name} -n ${NAMESPACE} -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
        case $phase in
            Complete)
                echo -e "${GREEN}Build ${build_name} complete${NC}"
                return 0
                ;;
            Failed|Error|Cancelled)
                echo -e "${RED}Build ${build_name} failed: ${phase}${NC}"
                return 1
                ;;
            *)
                sleep 10
                elapsed=$((elapsed + 10))
                if [ $((elapsed % 30)) -eq 0 ]; then
                    echo -e "${YELLOW}  Build ${phase}... (${elapsed}s)${NC}"
                fi
                ;;
        esac
    done
    echo -e "${RED}Build timed out after ${timeout}s${NC}"
    return 1
}

create_secrets() {
    echo -e "${BLUE}Creating secrets...${NC}"

    # Provision DB
    local db_host=$(yq eval '.secrets.provision_db.host' "$CONFIG_FILE")
    local db_port=$(yq eval '.secrets.provision_db.port' "$CONFIG_FILE")
    local db_name=$(yq eval '.secrets.provision_db.database' "$CONFIG_FILE")
    local db_user=$(yq eval '.secrets.provision_db.user' "$CONFIG_FILE")
    local db_password=$(yq eval '.secrets.provision_db.password' "$CONFIG_FILE")
    local alert_api_key=$(yq eval '.alert_api_key // ""' "$CONFIG_FILE")

    local secret_args=(
        --from-literal=db-host="$db_host"
        --from-literal=db-port="$db_port"
        --from-literal=db-name="$db_name"
        --from-literal=db-user="$db_user"
        --from-literal=db-password="$db_password"
    )
    if [ -n "$alert_api_key" ]; then
        secret_args+=(--from-literal=alert-api-key="$alert_api_key")
    fi

    oc create secret generic parsec-secrets -n ${NAMESPACE} \
        "${secret_args[@]}" \
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

    # Create the OAuthClient (cluster-scoped) with redirect URI
    local redirect_uri="https://${NAMESPACE}.${CLUSTER_DOMAIN}/oauth/callback"

    cat <<OAUTH_EOF | oc apply -f -
apiVersion: oauth.openshift.io/v1
kind: OAuthClient
metadata:
  name: ${oauth_client_name}
  labels:
    app: parsec
    component: oauth
secret: ${oauth_client_secret}
redirectURIs:
- "${redirect_uri}"
grantMethod: auto
respondWithChallenges: false
OAUTH_EOF

    echo -e "${GREEN}OAuth client created: ${oauth_client_name}${NC}"
    echo -e "${GREEN}  Redirect URI: ${redirect_uri}${NC}"
}

# --- Main deployment flow ---

if [ "${DRY_RUN}" = "true" ]; then
    echo -e "${YELLOW}[DRY RUN] Rendering kustomize output:${NC}"
    oc kustomize "$OVERLAY_DIR"
    echo ""
    echo -e "${YELLOW}[DRY RUN] No changes applied${NC}"
    exit 0
fi

# Step 1: Create namespace
echo -e "${BLUE}Step 1: Creating namespace...${NC}"
if oc get namespace ${NAMESPACE} &> /dev/null; then
    echo -e "${YELLOW}Namespace ${NAMESPACE} already exists${NC}"
else
    oc create namespace ${NAMESPACE}
    echo -e "${GREEN}Created namespace: ${NAMESPACE}${NC}"
fi
echo ""

# Step 2: Create secrets (before kustomize apply so pods can start)
echo -e "${BLUE}Step 2: Creating secrets...${NC}"
create_secrets
echo ""

# Step 3: OAuth setup (before kustomize so OAuthClient exists when proxy starts)
if [ "${OAUTH_ENABLED}" = "true" ]; then
    echo -e "${BLUE}Step 3: Setting up OAuth...${NC}"
    generate_oauth_secrets
    echo ""
fi

# Step 4: Apply kustomize overlay (creates deployments, services, routes, etc.)
echo -e "${BLUE}Step 4: Applying kustomize overlay...${NC}"
oc apply -k "$OVERLAY_DIR"
echo -e "${GREEN}Resources applied${NC}"

# Patch route host to be clean: parsec-dev.apps... or parsec.apps... instead of parsec-route-parsec-dev.apps...
ROUTE_HOST="${NAMESPACE}.${CLUSTER_DOMAIN}"
echo -e "${BLUE}Setting route host: ${ROUTE_HOST}${NC}"
oc patch route parsec-route -n ${NAMESPACE} --type=json -p "[{\"op\": \"add\", \"path\": \"/spec/host\", \"value\": \"${ROUTE_HOST}\"}]"
echo -e "${GREEN}Route host set${NC}"
echo ""

# Step 5: Patch configmap with environment-specific values (now that configmap exists)
echo -e "${BLUE}Step 5: Patching configmap with local settings...${NC}"
existing_config=$(oc get configmap parsec-config -n ${NAMESPACE} -o jsonpath='{.data.config\.yaml}' 2>/dev/null || echo "")
if [ -n "$existing_config" ]; then
    updated_config="$existing_config"

    # Cost-monitor dashboard URL
    local_cm_dashboard_url=$(yq eval '.cost_monitor.dashboard_url // ""' "$CONFIG_FILE")
    if [ -n "$local_cm_dashboard_url" ]; then
        updated_config=$(echo "$updated_config" | yq eval ".cost_monitor.dashboard_url = \"${local_cm_dashboard_url}\"" -)
        echo -e "${GREEN}  Dashboard URL: ${local_cm_dashboard_url}${NC}"
    fi

    # CloudTrail Lake event data store ID
    local_ct_eds_id=$(yq eval '.cloudtrail.event_data_store_id // ""' "$CONFIG_FILE")
    if [ -n "$local_ct_eds_id" ]; then
        updated_config=$(echo "$updated_config" | yq eval ".cloudtrail.event_data_store_id = \"${local_ct_eds_id}\"" -)
        echo -e "${GREEN}  CloudTrail EDS: ${local_ct_eds_id}${NC}"
    fi

    oc create configmap parsec-config -n ${NAMESPACE} \
        --from-literal=config.yaml="$updated_config" \
        --dry-run=client -o yaml | oc apply -f -
    echo -e "${GREEN}ConfigMap patched${NC}"
fi
echo ""

# Step 6: Configure webhook — reuse existing secret to avoid breaking GitHub webhooks
echo -e "${BLUE}Step 6: Configuring webhook...${NC}"
EXISTING_SECRET=$(oc get bc parsec -n ${NAMESPACE} -o jsonpath='{.spec.triggers[?(@.type=="GitHub")].github.secret}' 2>/dev/null | awk '{print $NF}')  # pragma: allowlist secret
if [ -n "$EXISTING_SECRET" ] && [ "$EXISTING_SECRET" != "placeholder" ]; then  # pragma: allowlist secret
    WEBHOOK_SECRET="$EXISTING_SECRET"
    echo -e "${GREEN}Reusing existing webhook secret${NC}"
else
    WEBHOOK_SECRET=$(openssl rand -base64 20 | tr -d "=+/" | cut -c1-20)
    oc set triggers bc parsec -n ${NAMESPACE} --remove-all
    oc set triggers bc parsec -n ${NAMESPACE} --from-config
    oc set triggers bc parsec -n ${NAMESPACE} --from-github --secret="$WEBHOOK_SECRET"
    echo -e "${GREEN}New webhook secret generated${NC}"
fi
API_SERVER=$(oc whoami --show-server | sed 's|https://||')
WEBHOOK_URL="https://${API_SERVER}/apis/build.openshift.io/v1/namespaces/${NAMESPACE}/buildconfigs/parsec/webhooks/${WEBHOOK_SECRET}/github"
echo -e "${GREEN}Webhook configured${NC}"
echo ""

# Step 7: Trigger build and wait for it to complete
echo -e "${BLUE}Step 7: Building image...${NC}"
BUILD_NAME=$(oc start-build parsec -n ${NAMESPACE} -o name | sed 's|build.build.openshift.io/||')
if ! wait_for_build "$BUILD_NAME" 600; then
    echo -e "${RED}Build failed. Check logs: oc logs build/${BUILD_NAME} -n ${NAMESPACE}${NC}"
    exit 1
fi
echo ""

# Step 8: Wait for deployment (now that image exists)
echo -e "${BLUE}Step 8: Waiting for deployment...${NC}"
# Restart to pick up the new image
oc rollout restart deployment/parsec -n ${NAMESPACE}
wait_for_deployment "parsec" 300
echo ""

# Step 9: Wait for OAuth proxy
if [ "${OAUTH_ENABLED}" = "true" ]; then
    echo -e "${BLUE}Step 9: Waiting for OAuth proxy...${NC}"
    wait_for_deployment "oauth-proxy" 300
    echo ""
fi

# Status
echo -e "${BLUE}Deployment Status:${NC}"
oc get pods -n ${NAMESPACE} --no-headers | grep -v build
echo ""
echo -e "${BLUE}Routes:${NC}"
oc get routes -n ${NAMESPACE}
echo ""

# URLs
PARSEC_URL="${NAMESPACE}.${CLUSTER_DOMAIN}"
echo -e "${GREEN}Deployment complete!${NC}"
echo ""
echo -e "${BLUE}Parsec URL:${NC} https://${PARSEC_URL}"
echo ""
echo -e "${BLUE}GitHub Webhook:${NC}"
echo -e "  URL: ${WEBHOOK_URL}"
echo -e "  Content type: application/json"
echo -e "  Secret: (leave blank)"
echo -e "  Events: Just the push event"
echo ""
echo -e "${BLUE}Next steps:${NC}"
echo -e "  1. Open https://${PARSEC_URL} (login with OpenShift credentials)"
echo -e "  2. Add the GitHub webhook above for auto-builds on push"
echo -e "  3. Update allowed users:"
echo -e "     oc edit configmap parsec-allowed-users -n ${NAMESPACE}"
echo -e "  4. View logs:"
echo -e "     oc logs -f deployment/parsec -n ${NAMESPACE}"
echo -e "     oc logs -f deployment/oauth-proxy -n ${NAMESPACE}"
