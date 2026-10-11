"""Permission system for AstralNexus task execution.

Implements permission inheritance and intersection logic:
- Parent task permissions are inherited by child tasks
- Worker profile defines base capabilities
- Task contract specifies required permissions
- Effective permissions = parent ∩ profile ∩ contract

This ensures security through capability restriction at multiple levels.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set


class PermissionCategory(Enum):
    """Categories of permissions for fine-grained control."""
    FILE_READ = auto()
    FILE_WRITE = auto()
    NETWORK_ACCESS = auto()
    CODE_EXECUTION = auto()
    DATABASE_ACCESS = auto()
    SYSTEM_CALL = auto()
    TOOL_USAGE = auto()


@dataclass
class Permission:
    """A single permission with category and scope."""
    category: PermissionCategory
    scope: str = "*"  # e.g., "*.py", "http://*", "/data/*"
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __hash__(self):
        """Hash based on category and scope (metadata is not part of identity)."""
        return hash((self.category, self.scope))
    
    def __eq__(self, other):
        """Equality based on category and scope."""
        if not isinstance(other, Permission):
            return False
        return self.category == other.category and self.scope == other.scope
    
    def matches(self, other: Permission) -> bool:
        """Check if this permission covers another permission.
        
        A permission matches if:
        - Same category
        - This scope is a superset of other scope (or wildcard)
        """
        if self.category != other.category:
            return False
        
        if self.scope == "*":
            return True
        
        if other.scope == "*":
            return False
        
        # Simple glob matching for common patterns
        if self.scope.endswith("*") and other.scope.startswith(self.scope[:-1]):
            return True
        
        return self.scope == other.scope


@dataclass
class PermissionSet:
    """A set of permissions with intersection and union operations."""
    permissions: Set[Permission] = field(default_factory=set)
    
    def __post_init__(self):
        # Ensure we have a mutable set
        if not isinstance(self.permissions, set):
            self.permissions = set(self.permissions)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PermissionSet:
        """Create PermissionSet from dictionary representation."""
        perms = set()
        for cat_name, scopes in data.items():
            try:
                category = PermissionCategory[cat_name.upper()]
            except KeyError:
                continue
            
            if isinstance(scopes, list):
                for scope in scopes:
                    perms.add(Permission(category=category, scope=str(scope)))
            else:
                perms.add(Permission(category=category, scope=str(scopes)))
        
        return cls(permissions=perms)
    
    def to_dict(self) -> Dict[str, List[str]]:
        """Convert to dictionary representation."""
        result: dict[str, list[str]] = {}
        for perm in self.permissions:
            cat_name = perm.category.name
            if cat_name not in result:
                result[cat_name] = []
            result[cat_name].append(perm.scope)
        
        # Sort for deterministic output
        return {k: sorted(v) for k, v in sorted(result.items())}
    
    def intersection(self, other: PermissionSet) -> PermissionSet:
        """Compute intersection of two permission sets.
        
        Returns permissions that exist in both sets (with matching scopes).
        """
        result: set[Permission] = set()
        
        # Group by category
        self_by_cat: dict[PermissionCategory, list[Permission]] = {}
        for p in self.permissions:
            self_by_cat.setdefault(p.category, []).append(p)
        
        other_by_cat: dict[PermissionCategory, list[Permission]] = {}
        for p in other.permissions:
            other_by_cat.setdefault(p.category, []).append(p)
        
        # Find matching categories
        common_cats = set(self_by_cat.keys()) & set(other_by_cat.keys())
        
        for cat in common_cats:
            self_perms = self_by_cat[cat]
            other_perms = other_by_cat[cat]
            
            # Find compatible permissions
            for sp in self_perms:
                for op in other_perms:
                    # Take the more restrictive scope
                    if sp.scope == "*":
                        result.add(Permission(category=cat, scope=op.scope))
                    elif op.scope == "*":
                        result.add(Permission(category=cat, scope=sp.scope))
                    elif sp.matches(op):
                        result.add(Permission(category=cat, scope=op.scope))
                    elif op.matches(sp):
                        result.add(Permission(category=cat, scope=sp.scope))
        
        return PermissionSet(permissions=result)
    
    def union(self, other: PermissionSet) -> PermissionSet:
        """Compute union of two permission sets."""
        return PermissionSet(permissions=self.permissions | other.permissions)
    
    def is_subset(self, other: PermissionSet) -> bool:
        """Check if this permission set is a subset of another."""
        for perm in self.permissions:
            if not any(p.matches(perm) for p in other.permissions):
                return False
        return True
    
    def is_empty(self) -> bool:
        """Check if permission set is empty."""
        return len(self.permissions) == 0
    
    def has_permission(self, category: PermissionCategory, scope: str = "*") -> bool:
        """Check if a specific permission exists."""
        target = Permission(category=category, scope=scope)
        return any(p.matches(target) for p in self.permissions)
    
    def add(self, category: PermissionCategory, scope: str = "*") -> None:
        """Add a permission to the set."""
        self.permissions.add(Permission(category=category, scope=scope))
    
    def remove(self, category: PermissionCategory, scope: str = "*") -> None:
        """Remove a permission from the set."""
        to_remove = [p for p in self.permissions if p.category == category and p.scope == scope]
        self.permissions -= set(to_remove)


def compute_effective_permissions(
    parent_permissions: Optional[PermissionSet],
    profile_permissions: PermissionSet,
    contract_permissions: Optional[PermissionSet],
) -> PermissionSet:
    """Compute effective permissions using intersection: parent ∩ profile ∩ contract.
    
    Args:
        parent_permissions: Permissions inherited from parent task (None = all permissions)
        profile_permissions: Permissions defined by worker profile
        contract_permissions: Permissions required by task contract (None = profile permissions)
    
    Returns:
        Intersection of all three permission sets
    
    Logic:
        - If parent is None, start with profile permissions
        - Otherwise, intersect parent with profile
        - If contract is None, return parent ∩ profile
        - Otherwise, intersect with contract
    """
    # Start with profile as base
    if parent_permissions is None:
        effective = profile_permissions
    else:
        effective = parent_permissions.intersection(profile_permissions)
    
    # Apply contract restrictions
    if contract_permissions is not None:
        effective = effective.intersection(contract_permissions)
    
    return effective


@dataclass
class PermissionContext:
    """Context for permission evaluation during task execution."""
    task_id: str
    worker_profile: str
    parent_task_id: Optional[str] = None
    effective_permissions: PermissionSet = field(default_factory=PermissionSet)
    permission_history: List[Dict[str, Any]] = field(default_factory=list)
    
    def check_permission(self, category: PermissionCategory, scope: str = "*") -> bool:
        """Check if a permission is granted in this context."""
        allowed = self.effective_permissions.has_permission(category, scope)
        
        # Record permission check
        self.permission_history.append({
            "category": category.name,
            "scope": scope,
            "allowed": allowed,
            "task_id": self.task_id,
            "worker_profile": self.worker_profile,
        })
        
        return allowed
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "task_id": self.task_id,
            "worker_profile": self.worker_profile,
            "parent_task_id": self.parent_task_id,
            "effective_permissions": self.effective_permissions.to_dict(),
            "permission_checks": len(self.permission_history),
        }
