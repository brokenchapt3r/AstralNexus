"""Windows resolve() device prefixes must not change ownership identity."""
import os
from pathlib import Path

import pytest

from AstralNexus.tools.builtin_tools import _inside_workspace
from AstralNexus.tools.context import tool_owned_paths, tool_workspace, resolve_tool_path
from AstralNexus.tools.executor import ToolExecutor


@pytest.mark.skipif(os.name != 'nt', reason='Windows path namespace')
@pytest.mark.parametrize('prefixed_side', ['candidate', 'root'])
def test_extended_prefix_keeps_same_namespace_without_allowing_escape(tmp_path, monkeypatch, prefixed_side):
    owned = tmp_path / 'owned'
    owned.mkdir()
    candidate = owned / 'result.txt'
    original = Path.resolve
    def mixed_resolve(self, *args, **kwargs):
        resolved = original(self, *args, **kwargs)
        if (prefixed_side == 'candidate' and self.name == 'result.txt') or (prefixed_side == 'root' and self == owned):
            return Path('\\\\?\\' + str(resolved))
        return resolved
    executor = ToolExecutor(workspace_root=tmp_path, owned_paths=['owned'])
    monkeypatch.setattr(Path, 'resolve', mixed_resolve)
    with tool_workspace(tmp_path), tool_owned_paths(['owned']):
        assert executor._write_scope_violation({'path': str(candidate)}) is None
        assert _inside_workspace(str(candidate)).name == 'result.txt'
        assert executor._write_scope_violation({'path': str(tmp_path / 'outside.txt')})
        with pytest.raises(ValueError, match='inside'):
            _inside_workspace(str(tmp_path.parent / 'escape.txt'))


@pytest.mark.skipif(os.name != 'nt', reason='Windows path namespace')
@pytest.mark.parametrize('name', ['directory.', 'directory ', 'CON', 'NUL.txt'])
def test_extended_special_names_are_not_reinterpreted(tmp_path, monkeypatch, name):
    extended = Path('\\\\?\\' + str(tmp_path / name))
    monkeypatch.setattr(Path, 'resolve', lambda self, **kwargs: extended)
    assert resolve_tool_path(tmp_path / name) == extended
