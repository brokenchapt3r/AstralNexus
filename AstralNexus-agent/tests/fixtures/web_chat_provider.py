"""Explicit OFFLINE provider fixture for the real web worker/CLI/Governor path.

This file is only selected by validation harnesses, never by the web API.
"""
import json
from pathlib import Path
import socket
import time
from unittest.mock import patch


def deny_network(*args, **kwargs):
    raise AssertionError('offline chat fixture forbids all network connections')


from AstralNexus.runtime import llm_client


class OfflineProvider:
    def __init__(self, **kwargs):
        pass

    def chat(self, messages, **kwargs):
        return json.dumps({'mode': 'direct_chat', 'complexity_score': .1, 'parallel_benefit': 0,
                           'required_capabilities': [], 'estimated_nodes': 1, 'needs_artifacts': False,
                           'needs_verification': False, 'reasoning': 'offline read-only lookup',
                           'task_contract_summary': 'read fixture'})

    def stream_chat(self, messages, **kwargs):
        from AstralNexus.tools.builtin_tools import _file_read
        from AstralNexus.tools.context import tool_workspace
        event = kwargs['event_callback']
        event({'event_type': 'llm.text.delta', 'text': '正在读取离线样例…'})
        event({'event_type': 'llm.tool_execution.start', 'calls': [{'name': 'file_read'}]})
        with tool_workspace(Path.cwd()):
            result = _file_read({'path': 'fixture.txt'})
        event({'event_type': 'llm.tool_batch', 'calls': [{'name': 'file_read', 'status': 'success', 'summary': '真实读取 fixture.txt 完成'}]})
        for _ in range(50):
            if kwargs['cancel_check']():
                return ''
            time.sleep(.1)
        return '离线验证回复（固定 Provider，不是实时模型）：' + result + '\n历史消息数：' + str(len(messages))


if __name__ == '__main__':
    from AstralNexus.runtime.workbench_chat_worker import main
    with patch.object(socket, 'create_connection', deny_network), \
            patch.object(socket.socket, 'connect', deny_network), \
            patch.object(llm_client, 'LLMClient', OfflineProvider):
        main()
