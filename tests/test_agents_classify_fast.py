"""Coverage tests for classify_fast in src/agent/agents.py.

Tests the regex-based fast-path classifier that routes single-domain
queries directly to sub-agents without an orchestrator LLM call.
"""

from src.agent.agents import classify_fast


class TestClassifyFast:
    # --- AAP2 ---
    def test_aap2_job_url(self):
        assert classify_fast("What happened with jobs/playbook/12345?") == "aap2"

    def test_aap2_rhpds_prefix(self):
        assert classify_fast("RHPDS deploy-ocp4 job failed") == "aap2"

    def test_aap2_job_failed(self):
        assert classify_fast("The job failed with an error") == "aap2"

    def test_aap2_failed_provision(self):
        assert classify_fast("The failed provision needs investigation") == "aap2"

    # --- Babylon ---
    def test_babylon_keyword(self):
        assert classify_fast("What is happening in babylon?") == "babylon"

    def test_babylon_catalog_item(self):
        assert classify_fast("Show me the catalog item ocp4-workshop") == "babylon"

    def test_babylon_resource_claim(self):
        assert classify_fast("Check the ResourceClaim for guid abc123") == "babylon"

    def test_babylon_workshop(self):
        assert classify_fast("List the workshop deployments") == "babylon"

    def test_babylon_splunk_logs(self):
        assert classify_fast("Check splunk logs for the deployment") == "babylon"

    # --- Cost ---
    def test_cost_keyword(self):
        assert classify_fast("What is the cost of this account?") == "cost"

    def test_cost_spending(self):
        assert classify_fast("How much did we spend last month?") == "cost"

    def test_cost_odcr(self):
        assert classify_fast("Check the ODCR utilization") == "cost"

    def test_cost_billing(self):
        assert classify_fast("Show me the billing for Q1") == "cost"

    def test_cost_azure_pool(self):
        assert classify_fast("What is the azure pool status?") == "cost"

    def test_cost_gcp_project(self):
        assert classify_fast("Show costs for gcp project openenv-abc123") == "cost"

    # --- Security ---
    def test_security_cloudtrail(self):
        assert classify_fast("Check cloudtrail for IAM key creation") == "security"

    def test_security_marketplace(self):
        assert classify_fast("List marketplace subscriptions for the account") == "security"

    def test_security_who_created(self):
        assert classify_fast("Who created this instance?") == "security"

    def test_security_compromised(self):
        assert classify_fast("Is this account compromised?") == "security"

    def test_security_running_instances(self):
        assert classify_fast("What is running instances on this account?") == "security"

    # --- OCPV ---
    def test_ocpv_keyword(self):
        assert classify_fast("Check ocpv cluster health") == "ocpv"

    def test_ocpv_pvc_pending(self):
        assert classify_fast("Why is the PVC stuck pending?") == "ocpv"

    def test_ocpv_storage_class(self):
        assert classify_fast("List storage class options") == "ocpv"

    def test_ocpv_cluster_number(self):
        assert classify_fast("What's happening on ocpv08?") == "ocpv"

    # --- Icinga ---
    def test_icinga_keyword(self):
        assert classify_fast("Check icinga for problems") == "icinga"

    def test_icinga_monitoring_alert(self):
        assert classify_fast("What monitoring alerts are active?") == "icinga"

    def test_icinga_host_down(self):
        assert classify_fast("Check the host down state") == "icinga"

    def test_icinga_downtime_schedule(self):
        assert classify_fast("Schedule downtime active for maintenance") == "icinga"

    def test_icinga_acknowledge(self):
        assert classify_fast("Acknowledge the critical alert") == "icinga"

    # --- Ambiguous / None ---
    def test_ambiguous_returns_none(self):
        """Both cost and security match -> orchestrator."""
        assert classify_fast("What is the cost of this compromised account's spending?") is None

    def test_both_aap2_and_babylon_returns_none(self):
        """Both AAP2 and Babylon match -> orchestrator."""
        assert classify_fast("RHPDS job failed on babylon cluster") is None

    def test_generic_question_returns_none(self):
        assert classify_fast("Hello, how are you?") is None

    def test_empty_string_returns_none(self):
        assert classify_fast("") is None
