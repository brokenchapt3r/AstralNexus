from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from AstralNexus.core.task_graph import TaskNode


class ResourceTier(str, Enum):
    """Execution locations governed by the approved Worker Profile."""

    LOCAL = "local"
    EDGE = "edge"
    CLOUD = "cloud"


@dataclass
class Resource:
    """A concrete execution resource that can be leased."""

    resource_id: str
    tier: ResourceTier
    privacy: str
    cost: str
    latency: str
    capabilities: List[str]
    available: bool = True
    capacity: int = 4
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("resource capacity must be at least 1")
        self.metadata.setdefault("active_leases", [])


@dataclass
class ResourcePool:
    """Inventory of resources; it contains no scheduling policy."""

    resources: Dict[ResourceTier, List[Resource]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for tier in ResourceTier:
            self.resources.setdefault(tier, [])

    def add_resource(self, resource: Resource) -> None:
        if any(
            existing.resource_id == resource.resource_id
            for existing in self.get_all_resources()
        ):
            raise ValueError(f"duplicate resource_id: {resource.resource_id}")
        self.resources[resource.tier].append(resource)

    def get_available_resources(self, tier: ResourceTier) -> List[Resource]:
        return [
            resource
            for resource in self.resources.get(tier, [])
            if resource.available
            and len(resource.metadata.get("active_leases", [])) < resource.capacity
        ]

    def get_all_resources(self) -> List[Resource]:
        return [
            resource
            for tier_resources in self.resources.values()
            for resource in tier_resources
        ]

    def get_resource(self, resource_id: str) -> Optional[Resource]:
        return next(
            (
                resource
                for resource in self.get_all_resources()
                if resource.resource_id == resource_id
            ),
            None,
        )


class ResourceLeaseError(RuntimeError):
    """Raised when a Governor-approved resource target cannot be leased."""


class ResourceScheduler:
    """Lease executor for Governor-approved resource decisions.

    This component deliberately contains no capability, privacy, cost, latency,
    or context scoring. GlobalGovernor owns those decisions. The scheduler only
    validates the approved tier/resource against live inventory and records the
    lease boundary.
    """

    def __init__(self, resource_pool: ResourcePool):
        if not isinstance(resource_pool, ResourcePool):
            raise TypeError("ResourceScheduler requires an explicit ResourcePool")
        self.resource_pool = resource_pool
        self.scheduling_history: List[Dict[str, Any]] = []

    def lease_resource(self, node: TaskNode) -> Resource:
        """Lease an available resource without changing Governor policy."""

        profile = node.metadata.get("worker_profile")
        if not isinstance(profile, dict) or not profile:
            raise ResourceLeaseError(
                f"task {node.node_id} has no Governor-approved worker_profile"
            )

        approved_tier_value = profile.get("resource_tier")
        if not approved_tier_value:
            raise ResourceLeaseError(
                f"task {node.node_id} has no Governor-approved resource_tier"
            )
        try:
            approved_tier = ResourceTier(str(approved_tier_value))
        except ValueError as exc:
            raise ResourceLeaseError(
                f"task {node.node_id} has invalid resource_tier: "
                f"{approved_tier_value}"
            ) from exc

        approved_resource_id = profile.get("resource_id")
        if approved_resource_id:
            resource = self.resource_pool.get_resource(str(approved_resource_id))
            if resource is None:
                raise ResourceLeaseError(
                    f"approved resource does not exist: {approved_resource_id}"
                )
            if resource.tier != approved_tier:
                raise ResourceLeaseError(
                    f"approved resource {resource.resource_id} is "
                    f"{resource.tier.value}, expected {approved_tier.value}"
                )
            active_leases = resource.metadata.get("active_leases", [])
            if not resource.available or len(active_leases) >= resource.capacity:
                raise ResourceLeaseError(
                    f"approved resource is unavailable: {resource.resource_id}"
                )
        else:
            candidates = sorted(
                self.resource_pool.get_available_resources(approved_tier),
                key=lambda item: item.resource_id,
            )
            if not candidates:
                raise ResourceLeaseError(
                    f"no available resource in Governor-approved tier "
                    f"{approved_tier.value}"
                )
            resource = candidates[0]

        active_leases = resource.metadata.setdefault("active_leases", [])
        if node.node_id in active_leases:
            raise ResourceLeaseError(
                f"task {node.node_id} already holds resource {resource.resource_id}"
            )
        active_leases.append(node.node_id)
        reason = (
            f"leased {resource.resource_id} within Governor-approved "
            f"tier {approved_tier.value}"
        )
        self.scheduling_history.append(
            {
                "node_id": node.node_id,
                "resource_id": resource.resource_id,
                "resource_tier": resource.tier.value,
                "decision_source": "global_governor",
                "reason": reason,
            }
        )
        return resource

    def release_resource(self, resource: Resource, *, node_id: str) -> None:
        """Release a lease at a worker lifecycle boundary."""

        active_leases = resource.metadata.setdefault("active_leases", [])
        if node_id not in active_leases:
            raise ResourceLeaseError(
                f"task {node_id} does not hold resource {resource.resource_id}"
            )
        active_leases.remove(node_id)

    def get_scheduling_history(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self.scheduling_history]

    def get_resource_utilization(self) -> Dict[str, Any]:
        all_resources = self.resource_pool.get_all_resources()
        used_resources = [
            resource
            for resource in all_resources
            if resource.metadata.get("active_leases")
        ]
        total = len(all_resources)
        return {
            "total_resources": total,
            "used_resources": len(used_resources),
            "utilization_rate": len(used_resources) / total if total else 0.0,
            "by_tier": {
                tier.value: {
                    "total": len(resources),
                    "available": sum(
                        max(
                            0,
                            resource.capacity
                            - len(resource.metadata.get("active_leases", [])),
                        )
                        for resource in resources
                        if resource.available
                    ),
                }
                for tier, resources in self.resource_pool.resources.items()
            },
        }
