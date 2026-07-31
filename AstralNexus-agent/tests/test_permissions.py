"""Tests for permission system with intersection logic."""
from AstralNexus.core.permissions import (
    Permission,
    PermissionCategory,
    PermissionSet,
    PermissionContext,
    compute_effective_permissions,
)


class TestPermission:
    """Test individual permission matching."""
    
    def test_permission_matches_same_category_and_scope(self):
        """Permissions with same category and scope should match."""
        p1 = Permission(PermissionCategory.FILE_READ, "*.py")
        p2 = Permission(PermissionCategory.FILE_READ, "*.py")
        assert p1.matches(p2)
    
    def test_permission_wildcard_matches_any_scope(self):
        """Wildcard scope should match any scope in same category."""
        p1 = Permission(PermissionCategory.FILE_READ, "*")
        p2 = Permission(PermissionCategory.FILE_READ, "*.py")
        assert p1.matches(p2)
    
    def test_permission_specific_does_not_match_wildcard(self):
        """Specific scope should not match wildcard."""
        p1 = Permission(PermissionCategory.FILE_READ, "*.py")
        p2 = Permission(PermissionCategory.FILE_READ, "*")
        assert not p1.matches(p2)
    
    def test_permission_different_category_no_match(self):
        """Different categories should never match."""
        p1 = Permission(PermissionCategory.FILE_READ, "*")
        p2 = Permission(PermissionCategory.FILE_WRITE, "*")
        assert not p1.matches(p2)
    
    def test_permission_glob_prefix_matching(self):
        """Glob patterns with prefix should match."""
        p1 = Permission(PermissionCategory.FILE_READ, "/data/*")
        p2 = Permission(PermissionCategory.FILE_READ, "/data/input.csv")
        assert p1.matches(p2)


class TestPermissionSet:
    """Test permission set operations."""
    
    def test_from_dict_creates_permissions(self):
        """PermissionSet should be created from dictionary."""
        data = {
            "FILE_READ": ["*.py", "*.md"],
            "NETWORK_ACCESS": "http://*",
        }
        pset = PermissionSet.from_dict(data)
        
        assert len(pset.permissions) == 3
        assert pset.has_permission(PermissionCategory.FILE_READ, "*.py")
        assert pset.has_permission(PermissionCategory.FILE_READ, "*.md")
        assert pset.has_permission(PermissionCategory.NETWORK_ACCESS, "http://*")
    
    def test_to_dict_roundtrip(self):
        """PermissionSet should serialize and deserialize correctly."""
        pset = PermissionSet()
        pset.add(PermissionCategory.FILE_READ, "*.py")
        pset.add(PermissionCategory.FILE_WRITE, "/output/*")
        
        data = pset.to_dict()
        assert "FILE_READ" in data
        assert "*.py" in data["FILE_READ"]
        assert "FILE_WRITE" in data
        assert "/output/*" in data["FILE_WRITE"]
    
    def test_intersection_takes_more_restrictive(self):
        """Intersection should take the more restrictive scope."""
        pset1 = PermissionSet()
        pset1.add(PermissionCategory.FILE_READ, "*")
        
        pset2 = PermissionSet()
        pset2.add(PermissionCategory.FILE_READ, "*.py")
        
        result = pset1.intersection(pset2)
        
        # Should have *.py (more restrictive)
        assert len(result.permissions) == 1
        assert result.has_permission(PermissionCategory.FILE_READ, "*.py")
    
    def test_intersection_empty_when_no_common_category(self):
        """Intersection should be empty when no common categories."""
        pset1 = PermissionSet()
        pset1.add(PermissionCategory.FILE_READ, "*")
        
        pset2 = PermissionSet()
        pset2.add(PermissionCategory.NETWORK_ACCESS, "*")
        
        result = pset1.intersection(pset2)
        assert result.is_empty()
    
    def test_union_combines_permissions(self):
        """Union should combine all permissions."""
        pset1 = PermissionSet()
        pset1.add(PermissionCategory.FILE_READ, "*.py")
        
        pset2 = PermissionSet()
        pset2.add(PermissionCategory.FILE_WRITE, "*.md")
        
        result = pset1.union(pset2)
        
        assert len(result.permissions) == 2
        assert result.has_permission(PermissionCategory.FILE_READ, "*.py")
        assert result.has_permission(PermissionCategory.FILE_WRITE, "*.md")
    
    def test_is_subset(self):
        """Should check if one set is subset of another."""
        pset1 = PermissionSet()
        pset1.add(PermissionCategory.FILE_READ, "*.py")
        
        pset2 = PermissionSet()
        pset2.add(PermissionCategory.FILE_READ, "*")
        pset2.add(PermissionCategory.FILE_WRITE, "*")
        
        assert pset1.is_subset(pset2)
        assert not pset2.is_subset(pset1)
    
    def test_add_and_remove(self):
        """Should add and remove permissions."""
        pset = PermissionSet()
        pset.add(PermissionCategory.FILE_READ, "*.py")
        
        assert pset.has_permission(PermissionCategory.FILE_READ, "*.py")
        
        pset.remove(PermissionCategory.FILE_READ, "*.py")
        assert not pset.has_permission(PermissionCategory.FILE_READ, "*.py")


class TestComputeEffectivePermissions:
    """Test permission intersection logic: parent ∩ profile ∩ contract."""
    
    def test_no_parent_uses_profile(self):
        """Without parent, effective = profile."""
        profile = PermissionSet()
        profile.add(PermissionCategory.FILE_READ, "*")
        profile.add(PermissionCategory.FILE_WRITE, "*")
        
        result = compute_effective_permissions(
            parent_permissions=None,
            profile_permissions=profile,
            contract_permissions=None,
        )
        
        assert len(result.permissions) == 2
        assert result.has_permission(PermissionCategory.FILE_READ, "*")
        assert result.has_permission(PermissionCategory.FILE_WRITE, "*")
    
    def test_parent_restricts_profile(self):
        """Parent permissions should restrict profile."""
        parent = PermissionSet()
        parent.add(PermissionCategory.FILE_READ, "/safe/*")
        
        profile = PermissionSet()
        profile.add(PermissionCategory.FILE_READ, "*")
        profile.add(PermissionCategory.FILE_WRITE, "*")
        
        result = compute_effective_permissions(
            parent_permissions=parent,
            profile_permissions=profile,
            contract_permissions=None,
        )
        
        # Should only have FILE_READ /safe/* (more restrictive)
        assert len(result.permissions) == 1
        assert result.has_permission(PermissionCategory.FILE_READ, "/safe/*")
        assert not result.has_permission(PermissionCategory.FILE_WRITE, "*")
    
    def test_contract_further_restricts(self):
        """Contract should further restrict parent ∩ profile."""
        parent = PermissionSet()
        parent.add(PermissionCategory.FILE_READ, "*")
        parent.add(PermissionCategory.FILE_WRITE, "*")
        
        profile = PermissionSet()
        profile.add(PermissionCategory.FILE_READ, "*")
        profile.add(PermissionCategory.FILE_WRITE, "*")
        
        contract = PermissionSet()
        contract.add(PermissionCategory.FILE_READ, "*.py")
        
        result = compute_effective_permissions(
            parent_permissions=parent,
            profile_permissions=profile,
            contract_permissions=contract,
        )
        
        # Should only have FILE_READ *.py
        assert len(result.permissions) == 1
        assert result.has_permission(PermissionCategory.FILE_READ, "*.py")
        assert not result.has_permission(PermissionCategory.FILE_WRITE, "*")
    
    def test_empty_intersection_when_incompatible(self):
        """Should return empty set when permissions are incompatible."""
        parent = PermissionSet()
        parent.add(PermissionCategory.FILE_READ, "/safe/*")
        
        profile = PermissionSet()
        profile.add(PermissionCategory.NETWORK_ACCESS, "*")
        
        result = compute_effective_permissions(
            parent_permissions=parent,
            profile_permissions=profile,
            contract_permissions=None,
        )
        
        assert result.is_empty()
    
    def test_all_three_intersect(self):
        """Should intersect all three: parent ∩ profile ∩ contract."""
        parent = PermissionSet()
        parent.add(PermissionCategory.FILE_READ, "/data/*")
        
        profile = PermissionSet()
        profile.add(PermissionCategory.FILE_READ, "*")
        
        contract = PermissionSet()
        contract.add(PermissionCategory.FILE_READ, "/data/input/*")
        
        result = compute_effective_permissions(
            parent_permissions=parent,
            profile_permissions=profile,
            contract_permissions=contract,
        )
        
        # Most restrictive: /data/input/*
        assert len(result.permissions) == 1
        assert result.has_permission(PermissionCategory.FILE_READ, "/data/input/*")


class TestPermissionContext:
    """Test permission context during task execution."""
    
    def test_check_permission_records_history(self):
        """Permission checks should be recorded in history."""
        pset = PermissionSet()
        pset.add(PermissionCategory.FILE_READ, "*.py")
        
        ctx = PermissionContext(
            task_id="task-1",
            worker_profile="researcher",
            effective_permissions=pset,
        )
        
        # Check allowed permission
        allowed = ctx.check_permission(PermissionCategory.FILE_READ, "*.py")
        assert allowed
        
        # Check denied permission
        denied = ctx.check_permission(PermissionCategory.FILE_WRITE, "*.py")
        assert not denied
        
        # Verify history
        assert len(ctx.permission_history) == 2
        assert ctx.permission_history[0]["allowed"] is True
        assert ctx.permission_history[1]["allowed"] is False
    
    def test_to_dict_serialization(self):
        """Context should serialize to dictionary."""
        pset = PermissionSet()
        pset.add(PermissionCategory.FILE_READ, "*")
        
        ctx = PermissionContext(
            task_id="task-1",
            worker_profile="coder",
            parent_task_id="parent-1",
            effective_permissions=pset,
        )
        
        data = ctx.to_dict()
        
        assert data["task_id"] == "task-1"
        assert data["worker_profile"] == "coder"
        assert data["parent_task_id"] == "parent-1"
        assert "FILE_READ" in data["effective_permissions"]
        assert data["permission_checks"] == 0
