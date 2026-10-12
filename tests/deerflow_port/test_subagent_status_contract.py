"""Native status contract: enums and terminal behavior must match the published schema.

Original DeerFlow metadata assertions remain in b119_failed_tests/*.before.txt;
those APIs are not implemented by AstralNexus and are not claimed as passing.
"""
from pathlib import Path
import json
import pytest
from AstralNexus.core.state_machine import TaskStatus
from AstralNexus.runtime.turn_terminal import STOP_REASON_DESCRIPTIONS, terminal_for_worker_exit

CONTRACT=Path(str(__import__('AstralNexus').__file__)).resolve().parent/"contracts/subagent_status_contract.json"

def test_contract_file_exists():
    assert CONTRACT.is_file()
    data=json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert data["schema_version"]==1
    assert set(data["valid_status_values"])=={s.value for s in TaskStatus}
    assert set(data["valid_stop_reason_values"])==set(STOP_REASON_DESCRIPTIONS)

@pytest.mark.parametrize("rc,completed,cancelled,at,stopping",[(0,"completed",False,False,False),(1,"failed",False,False,False),(0,"completed",True,True,False),(1,"failed",False,True,True),(1,"failed",False,True,False)])
def test_terminal_outcomes_are_declared(rc,completed,cancelled,at,stopping):
    status,reason=terminal_for_worker_exit(returncode=rc,completed_status=completed,cancelled=cancelled,cancelled_at=at,host_stopping=stopping)
    data=json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert status in data["turn_terminal_status_values"]
    assert reason=="" if status=="completed" else reason in data["valid_stop_reason_values"]
