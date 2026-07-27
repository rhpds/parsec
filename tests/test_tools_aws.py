"""Tests for src/tools/aws_account.py — AWS member account inspection."""

from unittest.mock import MagicMock, patch

import pytest

# Module-level caches that must be cleared between tests
import src.tools.aws_account as aws_account_module
from src.tools.aws_account import (
    _check_account_status,
    _classify_agreement,
    _describe_instances,
    _execute_action,
    query_aws_account,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear module-level credential caches before each test."""
    aws_account_module._assumed_creds.clear()
    aws_account_module._failed_assume_times.clear()
    yield
    aws_account_module._assumed_creds.clear()
    aws_account_module._failed_assume_times.clear()


FAKE_CREDS = {
    "AccessKeyId": "AKIA_FAKE_TEST_KEY_",
    "SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "SessionToken": "FwoGZXIvYXdzEBYaDH...",
}


# ---------------------------------------------------------------------------
# _classify_agreement
# ---------------------------------------------------------------------------


class TestClassifyAgreement:
    def test_recurring_payment_term(self):
        result = _classify_agreement(
            term_types={"recurringPaymentTerm"},
            estimated_cost=100.0,
            has_end_date=True,
            auto_renew=True,
        )
        assert result == "SaaS (Auto-Renew)"

    def test_renewal_term_auto_renew_not_false(self):
        result = _classify_agreement(
            term_types={"renewalTerm"},
            estimated_cost=50.0,
            has_end_date=True,
            auto_renew=True,
        )
        assert result == "SaaS (Auto-Renew)"

    def test_renewal_term_auto_renew_none(self):
        result = _classify_agreement(
            term_types={"renewalTerm"},
            estimated_cost=50.0,
            has_end_date=True,
            auto_renew=None,
        )
        assert result == "SaaS (Auto-Renew)"

    def test_renewal_term_auto_renew_disabled(self):
        result = _classify_agreement(
            term_types={"renewalTerm"},
            estimated_cost=50.0,
            has_end_date=True,
            auto_renew=False,
        )
        assert result == "SaaS (Auto-Renew Disabled)"

    def test_fixed_upfront_pricing_term(self):
        result = _classify_agreement(
            term_types={"fixedUpfrontPricingTerm"},
            estimated_cost=1000.0,
            has_end_date=True,
            auto_renew=None,
        )
        assert result == "Fixed/Upfront"

    def test_configurable_upfront_pricing_term(self):
        result = _classify_agreement(
            term_types={"configurableUpfrontPricingTerm"},
            estimated_cost=500.0,
            has_end_date=True,
            auto_renew=None,
        )
        assert result == "Fixed/Upfront"

    def test_has_end_date_with_cost(self):
        result = _classify_agreement(
            term_types={"someOtherTerm"},
            estimated_cost=75.0,
            has_end_date=True,
            auto_renew=None,
        )
        assert result == "SaaS (Auto-Renew)"

    def test_has_end_date_zero_cost_falls_through(self):
        result = _classify_agreement(
            term_types={"someOtherTerm"},
            estimated_cost=0.0,
            has_end_date=True,
            auto_renew=None,
        )
        assert result == "Pay-As-You-Go"

    def test_has_end_date_none_cost_falls_through(self):
        result = _classify_agreement(
            term_types={"someOtherTerm"},
            estimated_cost=None,
            has_end_date=True,
            auto_renew=None,
        )
        assert result == "Pay-As-You-Go"

    def test_default_pay_as_you_go(self):
        result = _classify_agreement(
            term_types=set(),
            estimated_cost=None,
            has_end_date=False,
            auto_renew=None,
        )
        assert result == "Pay-As-You-Go"


# ---------------------------------------------------------------------------
# _execute_action
# ---------------------------------------------------------------------------


class TestExecuteAction:
    @patch("src.tools.aws_account._get_assumed_creds")
    @patch("src.tools.aws_account.get_aws_session")
    def test_successful_dispatch_describe_instances(self, mock_session, mock_creds):
        mock_creds.return_value = FAKE_CREDS
        mock_session.return_value = MagicMock()

        mock_handler = MagicMock(return_value={"instance_count": 0, "instances": []})
        with patch.dict(
            aws_account_module._ACTION_DISPATCH,
            {"describe_instances": mock_handler},
        ):
            result = _execute_action("123456789012", "describe_instances", "us-east-1", None)

        mock_handler.assert_called_once_with(FAKE_CREDS, "us-east-1", None)
        assert result == {"instance_count": 0, "instances": []}

    @patch("src.tools.aws_account._get_assumed_creds")
    @patch("src.tools.aws_account.get_aws_session")
    def test_successful_dispatch_lookup_events(self, mock_session, mock_creds):
        mock_creds.return_value = FAKE_CREDS
        mock_session.return_value = MagicMock()

        mock_handler = MagicMock(return_value={"event_count": 0, "events": []})
        with patch.dict(
            aws_account_module._ACTION_DISPATCH,
            {"lookup_events": mock_handler},
        ):
            result = _execute_action("123456789012", "lookup_events", "us-west-2", None)

        mock_handler.assert_called_once_with(FAKE_CREDS, "us-west-2", None)
        assert result == {"event_count": 0, "events": []}

    @patch("src.tools.aws_account._get_assumed_creds")
    @patch("src.tools.aws_account.get_aws_session")
    def test_successful_dispatch_list_users(self, mock_session, mock_creds):
        mock_creds.return_value = FAKE_CREDS
        mock_session.return_value = MagicMock()

        mock_handler = MagicMock(return_value={"user_count": 0, "users": []})
        with patch.dict(
            aws_account_module._ACTION_DISPATCH,
            {"list_users": mock_handler},
        ):
            result = _execute_action("123456789012", "list_users", "us-east-1", None)

        mock_handler.assert_called_once_with(FAKE_CREDS, "us-east-1", None)
        assert result == {"user_count": 0, "users": []}

    @patch("src.tools.aws_account._check_account_status")
    @patch("src.tools.aws_account._get_assumed_creds")
    @patch("src.tools.aws_account.get_aws_session")
    def test_error_when_creds_none_suspended_account(self, mock_session, mock_creds, mock_status):
        mock_creds.return_value = None
        mock_session.return_value = MagicMock()
        mock_status.return_value = ("SUSPENDED", "sandbox-1234")

        result = _execute_action("123456789012", "describe_instances", "us-east-1", None)

        assert "error" in result
        assert "suspended" in result["error"].lower()
        assert result["account_status"] == "SUSPENDED"
        assert result["account_name"] == "sandbox-1234"

    @patch("src.tools.aws_account._check_account_status")
    @patch("src.tools.aws_account._get_assumed_creds")
    @patch("src.tools.aws_account.get_aws_session")
    def test_error_when_creds_none_unknown_status(self, mock_session, mock_creds, mock_status):
        mock_creds.return_value = None
        mock_session.return_value = MagicMock()
        mock_status.return_value = (None, None)

        result = _execute_action("123456789012", "describe_instances", "us-east-1", None)

        assert "error" in result
        assert "Cannot assume role" in result["error"]
        assert result["account_status"] == "UNKNOWN"
        assert result["account_name"] == ""

    @patch("src.tools.aws_account._get_assumed_creds")
    @patch("src.tools.aws_account.get_aws_session")
    def test_unknown_action_returns_error(self, mock_session, mock_creds):
        mock_creds.return_value = FAKE_CREDS
        mock_session.return_value = MagicMock()

        result = _execute_action("123456789012", "nonexistent_action", "us-east-1", None)

        assert "error" in result
        assert "Unknown action" in result["error"]
        assert "nonexistent_action" in result["error"]


# ---------------------------------------------------------------------------
# query_aws_account (async)
# ---------------------------------------------------------------------------


class TestQueryAwsAccount:
    @pytest.mark.asyncio
    async def test_invalid_account_id_too_short(self):
        result = await query_aws_account("12345", "describe_instances")
        assert "error" in result
        assert "Invalid AWS account ID" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_account_id_not_digits(self):
        result = await query_aws_account("12345678abcd", "describe_instances")
        assert "error" in result
        assert "Invalid AWS account ID" in result["error"]

    @pytest.mark.asyncio
    async def test_invalid_account_id_too_long(self):
        result = await query_aws_account("1234567890123", "describe_instances")
        assert "error" in result
        assert "Invalid AWS account ID" in result["error"]

    @pytest.mark.asyncio
    async def test_valid_call_dispatches_to_execute_action(self):
        with patch("src.tools.aws_account._execute_action") as mock_exec:
            mock_exec.return_value = {"instance_count": 2, "instances": ["a", "b"]}
            result = await query_aws_account(
                "123456789012", "describe_instances", "eu-west-1", {"state": "running"}
            )

        mock_exec.assert_called_once_with(
            "123456789012", "describe_instances", "eu-west-1", {"state": "running"}
        )
        assert result["account_id"] == "123456789012"
        assert result["action"] == "describe_instances"
        assert result["region"] == "eu-west-1"
        assert result["instance_count"] == 2

    @pytest.mark.asyncio
    async def test_valid_call_default_region(self):
        with patch("src.tools.aws_account._execute_action") as mock_exec:
            mock_exec.return_value = {"event_count": 0, "events": []}
            result = await query_aws_account("123456789012", "lookup_events")

        mock_exec.assert_called_once_with("123456789012", "lookup_events", "us-east-1", None)
        assert result["region"] == "us-east-1"

    @pytest.mark.asyncio
    async def test_exception_in_execute_action(self):
        with patch("src.tools.aws_account._execute_action") as mock_exec:
            mock_exec.side_effect = RuntimeError("boom")
            result = await query_aws_account("123456789012", "describe_instances")

        assert "error" in result
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# _describe_instances
# ---------------------------------------------------------------------------


class TestDescribeInstances:
    def test_with_state_filter(self):
        mock_ec2 = MagicMock()
        mock_paginator = MagicMock()
        mock_ec2.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-abc123",
                                "InstanceType": "t3.micro",
                                "State": {"Name": "running"},
                                "LaunchTime": MagicMock(isoformat=lambda: "2024-01-01T00:00:00"),
                                "Placement": {"AvailabilityZone": "us-east-1a"},
                                "PublicIpAddress": "1.2.3.4",
                                "Tags": [{"Key": "Name", "Value": "test-vm"}],
                            }
                        ]
                    }
                ]
            }
        ]

        with patch("src.tools.aws_account._make_client", return_value=mock_ec2):
            result = _describe_instances(FAKE_CREDS, "us-east-1", {"state": "running"})

        # Verify the filter was applied
        call_kwargs = mock_paginator.paginate.call_args[1]
        assert call_kwargs["Filters"] == [{"Name": "instance-state-name", "Values": ["running"]}]

        assert result["instance_count"] == 1
        inst = result["instances"][0]
        assert inst["instance_id"] == "i-abc123"
        assert inst["instance_type"] == "t3.micro"
        assert inst["state"] == "running"
        assert inst["public_ip"] == "1.2.3.4"
        assert inst["tags"] == {"Name": "test-vm"}

    def test_with_instance_ids_filter(self):
        mock_ec2 = MagicMock()
        mock_paginator = MagicMock()
        mock_ec2.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {
                "Reservations": [
                    {
                        "Instances": [
                            {
                                "InstanceId": "i-xyz789",
                                "InstanceType": "m5.large",
                                "State": {"Name": "stopped"},
                                "LaunchTime": MagicMock(isoformat=lambda: "2024-06-15T12:00:00"),
                                "Placement": {"AvailabilityZone": "us-west-2b"},
                                "Tags": [],
                            }
                        ]
                    }
                ]
            }
        ]

        with patch("src.tools.aws_account._make_client", return_value=mock_ec2):
            result = _describe_instances(FAKE_CREDS, "us-west-2", {"instance_ids": ["i-xyz789"]})

        call_kwargs = mock_paginator.paginate.call_args[1]
        assert call_kwargs["InstanceIds"] == ["i-xyz789"]
        assert "Filters" not in call_kwargs

        assert result["instance_count"] == 1
        inst = result["instances"][0]
        assert inst["instance_id"] == "i-xyz789"
        assert inst["public_ip"] == ""

    def test_no_filters(self):
        mock_ec2 = MagicMock()
        mock_paginator = MagicMock()
        mock_ec2.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Reservations": []}]

        with patch("src.tools.aws_account._make_client", return_value=mock_ec2):
            result = _describe_instances(FAKE_CREDS, "us-east-1", None)

        call_kwargs = mock_paginator.paginate.call_args[1]
        assert call_kwargs == {}
        assert result["instance_count"] == 0
        assert result["instances"] == []


# ---------------------------------------------------------------------------
# _check_account_status
# ---------------------------------------------------------------------------


class TestCheckAccountStatus:
    def test_returns_status_and_name(self):
        mock_session = MagicMock()
        mock_org = MagicMock()
        mock_session.client.return_value = mock_org
        mock_org.describe_account.return_value = {
            "Account": {"Status": "ACTIVE", "Name": "sandbox-5678"}
        }

        status, name = _check_account_status(mock_session, "123456789012")

        mock_session.client.assert_called_once_with("organizations", region_name="us-east-1")
        mock_org.describe_account.assert_called_once_with(AccountId="123456789012")
        assert status == "ACTIVE"
        assert name == "sandbox-5678"

    def test_returns_none_on_exception(self):
        mock_session = MagicMock()
        mock_org = MagicMock()
        mock_session.client.return_value = mock_org
        mock_org.describe_account.side_effect = Exception("Access denied")

        status, name = _check_account_status(mock_session, "123456789012")

        assert status is None
        assert name is None
