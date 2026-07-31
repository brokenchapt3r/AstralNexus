"""Tests for DeliveryContract dataclass and unified event protocol (问题7 & 8)."""
import pytest
from AstralNexus.core.governor import DeliveryContract, EventType


class TestDeliveryContract:
    """Test DeliveryContract dataclass functionality."""

    def test_create_delivery_contract(self):
        """Test basic DeliveryContract creation."""
        contract = DeliveryContract(
            goal_id="G-001",
            task_version=1,
            delivery_mode="code",
            execution_evidence_policy="runtime_required",
            required_outputs=[
                {
                    "output_id": "O-01",
                    "name": "main.py",
                    "type": "file",
                    "required": True,
                    "source": "explicit",
                    "acceptance_criteria": ["passes_all_tests"],
                }
            ],
            global_acceptance_criteria=["all_tests_pass"],
        )
        assert contract.goal_id == "G-001"
        assert contract.task_version == 1
        assert contract.delivery_mode == "code"
        assert len(contract.required_outputs) == 1

    def test_bind_artifact_paths(self):
        """Test binding artifact paths to required outputs."""
        contract = DeliveryContract(
            goal_id="G-001",
            task_version=1,
            required_outputs=[
                {"output_id": "O-01", "name": "main.py", "type": "file"},
                {"output_id": "O-02", "name": "test.py", "type": "file"},
            ],
        )
        artifact_map = {
            "O-01": "/output/artifacts/main.py",
            "O-02": "/output/artifacts/test.py",
        }
        contract.bind_artifact_paths(artifact_map)

        assert contract.required_outputs[0]["artifact_path"] == "/output/artifacts/main.py"
        assert contract.required_outputs[0]["artifact_verified"] is True
        assert contract.required_outputs[1]["artifact_path"] == "/output/artifacts/test.py"

    def test_bind_artifact_paths_partial(self):
        """Test binding only some artifact paths."""
        contract = DeliveryContract(
            goal_id="G-001",
            task_version=1,
            required_outputs=[
                {"output_id": "O-01", "name": "main.py", "type": "file"},
                {"output_id": "O-02", "name": "test.py", "type": "file"},
            ],
        )
        artifact_map = {"O-01": "/output/artifacts/main.py"}
        contract.bind_artifact_paths(artifact_map)

        assert contract.required_outputs[0]["artifact_path"] == "/output/artifacts/main.py"
        assert "artifact_path" not in contract.required_outputs[1]

    def test_as_dict(self):
        """Test serialization to dict."""
        contract = DeliveryContract(
            goal_id="G-001",
            task_version=2,
            delivery_mode="structured",
            required_sections=["Introduction", "Results"],
            global_acceptance_criteria=["all_sections_present"],
            needs_artifact_refs=True,
        )
        d = contract.as_dict()
        assert d["goal_id"] == "G-001"
        assert d["task_version"] == 2
        assert d["delivery_mode"] == "structured"
        assert d["required_sections"] == ["Introduction", "Results"]
        assert d["global_acceptance_criteria"] == ["all_sections_present"]
        assert d["needs_artifact_refs"] is True
        # Verify all fields present
        expected_keys = {
            "goal_id", "task_version", "delivery_mode", "execution_evidence_policy",
            "required_outputs", "required_sections", "recommended_sections",
            "hard_constraints", "soft_preferences", "global_acceptance_criteria",
            "completion_evidence", "forbidden_claims", "uncertainties",
            "assumptions", "needs_clarification", "needs_citations", "needs_artifact_refs",
        }
        assert expected_keys.issubset(set(d.keys()))


class TestEventProtocol:
    """Test unified event protocol (问题8)."""

    def test_event_type_constants_exist(self):
        """Test all required event type constants exist."""
        required_events = [
            "TASK_CREATED", "TASK_READY", "TASK_STARTED",
            "TASK_RESULT_SUBMITTED", "TASK_VERIFIED",
            "TASK_PROGRESS", "TASK_COMPLETED", "TASK_FAILED",
            "TASK_TIMEOUT", "TASK_BLOCKED",
            "BUDGET_UPDATED", "BUDGET_EXHAUSTED",
            "DELIVERY_CONTRACT_DERIVED", "DELIVERY_CONTRACT_FAILED",
            "GRAPH_PATCH_PROPOSED", "GRAPH_PATCH_APPLIED",
            "GRAPH_PATCH_FAILED", "GRAPH_PATCH_REJECTED",
            "WORKER_SELECTED", "WORKER_LOST", "WORKER_INSTANTIATED",
            "ARTIFACT_CREATED", "EVIDENCE_CONFLICT",
            "USER_REQUIREMENT_CHANGED", "RESOURCE_UNAVAILABLE",
            "VERIFIER_COMPLETED", "DELIVERY_VERIFIED",
            "SYNTHESIS_VERIFICATION", "SYNTHESIS_VERIFICATION_FAILED",
        ]
        for event_name in required_events:
            assert hasattr(EventType, event_name), f"Missing event: {event_name}"

    def test_event_type_values_are_governor_prefixed(self):
        """Test all event type values follow governor.* naming convention."""
        event_attrs = [
            attr for attr in dir(EventType)
            if not attr.startswith("_") and attr != "EVENT_VERSION"
        ]
        for attr in event_attrs:
            value = getattr(EventType, attr)
            if isinstance(value, str):
                assert value.startswith("governor."), f"Event {attr}={value} missing governor. prefix"

    def test_task_result_submitted_event(self):
        """Test TASK_RESULT_SUBMITTED event type exists and has correct value."""
        assert EventType.TASK_RESULT_SUBMITTED == "governor.task_result_submitted"

    def test_task_verified_event(self):
        """Test TASK_VERIFIED event type exists and has correct value."""
        assert EventType.TASK_VERIFIED == "governor.task_verified"
