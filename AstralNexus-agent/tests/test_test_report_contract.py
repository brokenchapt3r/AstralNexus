from copy import deepcopy
from datetime import datetime
import hashlib
import json

import pytest

from AstralNexus.core.governor import GlobalGovernor
from AstralNexus.core.task_graph import TaskGraph
from AstralNexus.runtime.execution_store import ExecutionStore
from AstralNexus.runtime.test_report_contract import v1_case_counts, has_recorded_report_execution
from AstralNexus.runtime.trace import TraceRecorder


def report():
    return {'schema': 'astralnexus.test-results/v1', 'generated_at': datetime.now().isoformat(),
            'cases': 2, 'totals': {'collected': 2, 'passed': 1, 'xpassed': 1, 'failed': 0},
            'tests': [{'nodeid': 'health', 'outcome': 'passed'}, {'nodeid': 'flow', 'outcome': 'xpassed'}]}


@pytest.mark.parametrize('mutation', ['total', 'duplicate', 'failure', 'unknown', 'boolean', 'top_count'])
def test_v1_disagreement_never_becomes_pass(mutation):
    data = report()
    if mutation == 'total': data['totals']['collected'] = 3
    if mutation == 'duplicate': data['tests'][1]['nodeid'] = 'health'
    if mutation == 'failure': data['tests'][0]['outcome'] = 'failed'
    if mutation == 'unknown': data['tests'][0]['outcome'] = 'unverified'
    if mutation == 'boolean': data['totals']['passed'] = True
    if mutation == 'top_count': data['passed'] = 99
    assert not v1_case_counts(data)[0]




