# Contributor and coding-agent guidance

- Runtime Python lives in `src/AstralNexus`; repository tests live in `tests`.
- TUI source lives in `tui`; Python packaging metadata lives at the repository root.
- Preserve unrelated changes and third-party licenses. Do not commit keys, personal run data, caches or build copies.
- After Python changes, compile or import the changed modules, then run the narrowest relevant pytest target.
- Offline regression uses `python -m pytest tests -m "not live_llm"`. Real model calls need explicit authorization.
- Type checking uses `python scripts/check_mypy.py`; dependencies are listed in `requirements-mypy.txt`.
- TUI changes require `npm run check` and `npm run build` in `tui`.
- Failed validation remains failed until repaired. Report command output and exit codes; distinguish offline framework tests from generated-project business acceptance.
- The repository uses a `src` layout. Use package-relative resources or installed package paths, and locate repository metadata independently of the package directory.
