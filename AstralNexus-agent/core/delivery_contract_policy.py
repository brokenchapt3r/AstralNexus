"""One interpretation of persisted and newly derived delivery contracts."""
from copy import deepcopy
from typing import Any

CONTRACT_SCHEMA_VERSION = 2
GATE_POLICY_VERSION = "staged-contract-v1"


def normalize_delivery_contract(raw: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(raw)
    if not value:
        return value
    previous = int(value.get("contract_schema_version", 1))
    if previous > CONTRACT_SCHEMA_VERSION or previous < 1:
        raise ValueError("Unsupported delivery contract schema")
    value["contract_schema_version"] = CONTRACT_SCHEMA_VERSION
    value["gate_policy_version"] = GATE_POLICY_VERSION
    flag = value.get("summary_required", False)
    value["summary_required"] = flag if isinstance(flag, bool) else str(flag).lower() in {"true", "yes", "1"}
    for output in value.get("required_outputs", []) or []:
        if not isinstance(output, dict):
            continue
        from AstralNexus.core.output_paths import declared_output_is_required
        output["required"] = declared_output_is_required(output)
    if previous < CONTRACT_SCHEMA_VERSION:
        value.setdefault("migrated_from_contract_schema_version", previous)
    return value
