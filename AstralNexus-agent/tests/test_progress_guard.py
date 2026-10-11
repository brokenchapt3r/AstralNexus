from AstralNexus.runtime.progress_guard import ProgressGuard


def read(guard, name, content="unchanged", files=None):
    return guard.observe({"id": "provider-id", "function": {
        "name": "file_read", "arguments": {"path": name},
    }}, {"status": "success", "content": content}, files or {})


def test_repeated_successful_read_cycle_replans_then_hands_off():
    guard = ProgressGuard()
    actions = []
    for _ in range(8):
        for name in ["schema", "main", "contract"]:
            result = read(guard, name)
            if result:
                actions.append(result["action"])
    assert actions[:2] == ["replan", "handoff"]


def test_long_work_with_new_evidence_is_not_a_stall():
    guard = ProgressGuard()
    for revision in range(100):
        assert read(guard, "main", str(revision)) is None


def test_file_change_resets_cycle_and_new_evidence_resets_escalation():
    guard = ProgressGuard()
    read(guard, "main")
    assert read(guard, "main")["action"] == "replan"
    assert read(guard, "main", files={"main": {"sha256": "new"}}) is None
    assert read(guard, "main", files={"main": {"sha256": "new"}})["action"] == "replan"
    assert read(guard, "test", "new failure", files={"main": {"sha256": "new"}}) is None
