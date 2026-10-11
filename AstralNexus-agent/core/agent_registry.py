from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, ParamSpec, TypeVar, Concatenate, Iterator
from contextvars import ContextVar
from contextlib import contextmanager
from copy import deepcopy
from threading import RLock
from functools import wraps

from AstralNexus.agents.contracts import RegisteredAgent


_P = ParamSpec('_P')
_R = TypeVar('_R')


def registry_locked(function: Callable[Concatenate[AgentRegistry, _P], _R]) -> Callable[Concatenate[AgentRegistry, _P], _R]:
    @wraps(function)
    def locked(self: AgentRegistry, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        with self._lock:
            return function(self, *args, **kwargs)
    return locked


@dataclass
class AgentRole:
    """Defines a role for an agent in the AstralNexus system."""
    
    # Role identifier
    name: str
    
    # Role description
    description: str
    
    # Default tools this role has access to
    default_tools: List[str] = field(default_factory=list)
    
    # Capabilities this role supports
    capabilities: List[str] = field(default_factory=list)
    
    # Input/output contracts
    input_contract: Optional[str] = None
    output_contract: Optional[str] = None


class AgentRegistry:
    """Registry for managing agent roles in the AstralNexus system."""
    
    def __init__(self):
        self._lock = RLock()
        self._roles: Dict[str, AgentRole] = {}
        self._role_factories: Dict[str, Callable[..., RegisteredAgent]] = {}
        self._role_aliases: Dict[str, str] = {}  # alias -> canonical role name
    
    @registry_locked
    def register_role(self, role: AgentRole) -> None:
        """Register a new agent role."""
        self._roles[role.name] = role
    
    @registry_locked
    def register_role_factory(self, role_name: str, factory: Callable[..., RegisteredAgent]) -> None:
        """Register a factory function for creating agents of a specific role."""
        self._role_factories[role_name] = factory
    
    @registry_locked
    def register_role_alias(self, alias: str, canonical_role: str) -> None:
        """Register an alias for a canonical role name.
        
        Allows create_agent('tester') to resolve to the 'verifier' factory.
        
        Args:
            alias: The alias name (e.g. 'tester', 'documenter')
            canonical_role: The canonical role name the alias maps to
        """
        self._role_aliases[alias.lower()] = canonical_role
    
    @registry_locked
    def resolve_role(self, role_name: str) -> str:
        """Resolve a role name through the alias map.
        
        Args:
            role_name: Role name to resolve (case-insensitive)
            
        Returns:
            Canonical role name (lowercased, may be alias-resolved)
        """
        return self._role_aliases.get(role_name.lower(), role_name.lower())
    
    @registry_locked
    def get_role(self, role_name: str) -> Optional[AgentRole]:
        """Get a registered role by name (resolves aliases)."""
        canonical = self.resolve_role(role_name)
        return self._roles.get(canonical) or self._roles.get(role_name)
    
    @registry_locked
    def get_role_factory(self, role_name: str) -> Optional[Callable[..., RegisteredAgent]]:
        """Get a factory function for creating agents of a specific role (resolves aliases)."""
        canonical = self.resolve_role(role_name)
        return self._role_factories.get(canonical) or self._role_factories.get(role_name)
    
    @registry_locked
    def list_roles(self) -> List[str]:
        """List all registered role names."""
        return list(self._roles.keys())
    
    @registry_locked
    def list_aliases(self) -> Dict[str, str]:
        """List all registered role aliases."""
        return dict(self._role_aliases)
    
    def create_agent(self, role_name: str, **kwargs) -> Optional[RegisteredAgent]:
        """Create an agent instance for a specific role (resolves aliases)."""
        # Try canonical name first, then alias-resolved name
        factory = self.get_role_factory(role_name)
        if factory:
            return factory(**kwargs)
        return None

    @registry_locked
    def clone(self) -> AgentRegistry:
        result = AgentRegistry()
        result._roles = deepcopy(self._roles)
        result._role_factories = dict(self._role_factories)
        result._role_aliases = dict(self._role_aliases)
        return result

    def create_worker(self, role_name: str, profile: Any = None, **kwargs) -> Optional[RegisteredAgent]:
        """Instantiate a role and bind the Governor-selected Worker Profile.

        A role factory supplies the implementation; the profile supplies the
        execution contract.  Keeping this operation here prevents callers
        from silently recording a profile while still creating an unconfigured
        default agent.
        """
        worker = self.create_agent(role_name, **kwargs)
        if worker is None:
            return None
        if profile is not None:
            data = profile if isinstance(profile, dict) else {
                "worker_type": getattr(profile, "worker_type", ""),
                "tools": list(getattr(profile, "tools", []) or []),
                "model_preference": getattr(profile, "model_preference", ""),
                "reasoning_level": getattr(profile, "reasoning_level", "medium"),
                "input_contract": getattr(profile, "input_contract", ""),
                "output_contract": getattr(profile, "output_contract", ""),
                "capabilities": list(getattr(profile, "capabilities", []) or []),
            }
            worker.worker_profile = dict(data)
            worker.default_tools = list(data.get("tools", []))
            worker.input_contract = str(data.get("input_contract", ""))
            worker.output_contract = str(data.get("output_contract", ""))
            worker.model_preference = str(data.get("model_preference", ""))
            worker.reasoning_level = str(data.get("reasoning_level", "medium"))
            worker.capabilities = list(data.get("capabilities", getattr(worker, "capabilities", [])))
            try:
                from AstralNexus.runtime.model_selector import get_model_profile
                worker.model_profile = get_model_profile(data.get("model_preference") or None)
            except Exception:
                worker.model_profile = {"model": data.get("model_preference", "")}
        return worker


# Global agent registry instance
agent_registry = AgentRegistry()
_scoped_registry: ContextVar[AgentRegistry | None] = ContextVar('agent_registry', default=None)
_bootstrap_lock = RLock()
_bootstrapped = False
_bootstrap_registry: AgentRegistry | None = None


@contextmanager
def agent_registry_scope(registry: AgentRegistry) -> Iterator[None]:
    token = _scoped_registry.set(registry)
    try: yield
    finally: _scoped_registry.reset(token)


def get_agent_registry() -> AgentRegistry:
    """Use the run catalogue; bootstrap defaults once on explicit first use."""
    global _bootstrapped, _bootstrap_registry
    scoped = _scoped_registry.get()
    if scoped is not None: return scoped
    with _bootstrap_lock, agent_registry._lock:
        if not _bootstrapped:
            if _bootstrap_registry is not None: return _bootstrap_registry
            draft = agent_registry.clone()
            _bootstrap_registry = draft
            try:
                from AstralNexus.core.role_registry import initialize_role_registry
                initialize_role_registry(draft)
                agent_registry._roles = draft._roles
                agent_registry._role_factories = draft._role_factories
                agent_registry._role_aliases = draft._role_aliases
                _bootstrapped = True
            finally:
                _bootstrap_registry = None
    return agent_registry
