"""Full tool injection pipeline with Governor approval and budget enforcement.

Implements the complete flow required by #29:
  节点目标 → search_tools → 能力/权限过滤 → Schema 预算过滤 → Governor 批准 → 注入工具

Key constraints:
- Search results must include: name, capabilities, permissions, input/output schemas, 
  source, installation status, risk level
- Forbidden: full schema injection or treating uninstalled tools as callable
- High-risk tools require actual Governor approval before injection
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from AstralNexus.tools.tool_router import (
    discover_tools,
    estimate_schema_tokens,
    filter_by_schema_budget,
)

if TYPE_CHECKING:
    from AstralNexus.core.governor import Governor


@dataclass
class ToolInjectionDecision:
    """Governor's decision on whether to approve a tool for injection."""
    tool_name: str
    approved: bool
    reason: str
    risk_level: str
    estimated_tokens: int


class ToolInjectionPipeline:
    """Orchestrates the full tool discovery → filtering → approval → injection flow.
    
    This pipeline enforces the #29 requirement that tool injection must go through:
    1. Goal-aware discovery
    2. Capability/permission filtering
    3. Schema budget filtering
    4. Governor approval for high-risk tools
    5. Final injection with complete metadata
    
    Validation:
    - Rejects full schema injection (total tokens > budget)
    - Rejects uninstalled tools
    - Ensures all metadata fields are present
    """
    
    def __init__(
        self,
        governor: Governor,
        *,
        max_total_schema_tokens: int = 4000,
        require_governor_approval_for_high_risk: bool = True,
    ):
        self._governor = governor
        self._max_total_tokens = max_total_schema_tokens
        self._require_approval = require_governor_approval_for_high_risk
    
    def execute(
        self,
        *,
        node_goal: str,
        role: str,
        context_budget: int,
        reserve_for_output: int = 200,
        candidates: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Execute the full tool injection pipeline.
        
        Args:
            node_goal: The node's goal for semantic relevance
            role: Worker role for toolset resolution
            context_budget: Total token budget available for tool schemas
            reserve_for_output: Tokens to reserve for model output
            candidates: Optional allow-list of tool names
            
        Returns:
            List of approved tools with complete metadata
            
        Raises:
            ValueError: If total schema tokens exceed budget or uninstalled tools detected
        """
        # Step 1: Discover tools with capability/permission filtering
        schema_budget = max(0, context_budget - reserve_for_output)
        discovered = discover_tools(
            role=role,
            goal=node_goal,
            candidates=candidates,
            schema_budget=schema_budget,
            governor_approval=True,  # Mark high-risk tools
        )
        
        # Step 2: Validate installation status
        for tool in discovered:
            if not tool.get("installed", False):
                raise ValueError(
                    f"Uninstalled tool '{tool['name']}' cannot be injected. "
                    f"Install it first or remove from candidates."
                )
        
        # Step 3: Governor approval for high-risk tools
        approved_tools = []
        for tool in discovered:
            if tool.get("risk_level") == "high" and self._require_approval:
                # Actually call Governor for approval
                decision = self._request_governor_approval(
                    tool_name=tool["name"],
                    node_goal=node_goal,
                    risk_level=tool["risk_level"],
                    estimated_tokens=tool["schema_tokens"],
                )
                if decision.approved:
                    tool["approval_status"] = "governor_approved"
                    tool["governor_reason"] = decision.reason
                    approved_tools.append(tool)
                # If not approved, skip this tool
            else:
                # Low-risk tools are auto-approved
                tool["approval_status"] = "auto_approved"
                approved_tools.append(tool)
        
        # Step 4: Final budget validation - prevent full schema injection
        total_tokens = sum(t["schema_tokens"] for t in approved_tools)
        if total_tokens > self._max_total_tokens:
            raise ValueError(
                f"Total schema tokens ({total_tokens}) exceeds maximum allowed "
                f"({self._max_total_tokens}). Reduce tool count or increase budget."
            )
        
        # Step 5: Validate metadata completeness
        required_fields = {
            "name", "capabilities", "permissions", "input_schema", 
            "output_schema", "source", "installed", "risk_level", 
            "approval_status", "schema_tokens"
        }
        for tool in approved_tools:
            missing = required_fields - set(tool.keys())
            if missing:
                raise ValueError(
                    f"Tool '{tool['name']}' missing required metadata: {missing}"
                )
        
        return approved_tools
    
    def _request_governor_approval(
        self,
        *,
        tool_name: str,
        node_goal: str,
        risk_level: str,
        estimated_tokens: int,
    ) -> ToolInjectionDecision:
        """Request Governor approval for a high-risk tool.
        
        This is the actual Governor call, not just marking.
        """
        # Use Governor's tool approval capability
        approved = self._governor.approve_tool_injection(
            tool_name=tool_name,
            node_goal=node_goal,
            risk_level=risk_level,
            estimated_tokens=estimated_tokens,
        )
        
        if approved:
            return ToolInjectionDecision(
                tool_name=tool_name,
                approved=True,
                reason=f"Governor approved {risk_level}-risk tool for goal: {node_goal[:50]}",
                risk_level=risk_level,
                estimated_tokens=estimated_tokens,
            )
        else:
            return ToolInjectionDecision(
                tool_name=tool_name,
                approved=False,
                reason=f"Governor rejected {risk_level}-risk tool",
                risk_level=risk_level,
                estimated_tokens=estimated_tokens,
            )


def validate_injection_budget(
    tools: List[Dict[str, Any]],
    max_tokens: int,
) -> None:
    """Validate that total schema tokens don't exceed budget.
    
    This is a standalone validation function for use outside the pipeline.
    
    Args:
        tools: List of tools with 'schema_tokens' field
        max_tokens: Maximum allowed total tokens
        
    Raises:
        ValueError: If total exceeds max_tokens
    """
    total = sum(t.get("schema_tokens", 0) for t in tools)
    if total > max_tokens:
        raise ValueError(
            f"Schema token budget exceeded: {total} > {max_tokens}. "
            f"Reduce tool count or increase budget."
        )


def ensure_metadata_complete(tool: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure tool has all required metadata fields.
    
    Fills in sensible defaults for missing fields.
    
    Args:
        tool: Tool dictionary to validate
        
    Returns:
        Tool dictionary with all required fields present
    """
    defaults = {
        "capabilities": [],
        "permissions": ["read_only"],
        "input_schema": {},
        "output_schema": {},
        "source": "unknown",
        "installed": True,
        "risk_level": "low",
        "approval_status": "auto_approved",
        "schema_tokens": 0,
    }
    
    result = dict(tool)
    for field, default in defaults.items():
        if field not in result:
            result[field] = default
    
    return result
