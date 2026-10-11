"""Plain keyboard client for screen readers; same Gateway and persistent state."""
from __future__ import annotations
import sys
import threading
from typing import Any
from AstralNexus.tui_gateway import entry


def run_plain() -> int:
    gateway = entry.Gateway()
    session = gateway.session(None) if gateway.current_session_id else gateway.create_session()
    lock = threading.Lock()
    last_progress = ''
    def render(kind: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> None:
        nonlocal last_progress
        payload = payload or {}
        text = ''
        if kind == 'message.delta':
            text = str(payload.get('text') or '')
        elif kind == 'message.complete':
            text = '\n本轮对话结束；/delivery 查看正式交付状态。\n'
        elif kind == 'tool.start':
            text = f"\n工具：{payload.get('name')} · {payload.get('args_text', '')}\n"
        elif kind == 'tool.complete':
            text = f"\n工具结果：{payload.get('name')} · {payload.get('error') or '已返回'}\n{payload.get('result_text', '')}\n"
        elif kind == 'run.progress':
            progress = str(payload.get('text') or '')
            if progress != last_progress:
                text, last_progress = '\n' + progress + '\n', progress
        elif kind in {'error', 'runtime.event'}:
            text = '\n' + str(payload.get('message') or payload.get('summary') or payload.get('title') or '') + '\n'
        elif kind == 'approval.request':
            text = f"\n请求批准：{payload.get('description')}\n{payload.get('command')}\n/approve {payload.get('request_id')} once|session|deny\n"
        elif kind == 'approval.expire':
            text = f"\n批准请求已取消或超时：{payload.get('request_id')}\n"
        if text:
            with lock:
                sys.stdout.write(text)
                sys.stdout.flush()
    session.event_sink = render
    print('天枢纯文本模式。/plan /evidence /changes /delivery；/model /submodel；/checkpoint /checkpoints；/stop 停止；/quit 退出。')
    try:
        while True:
            try:
                line = input('> ').strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                gateway.dispatch('session.interrupt', {'session_id': session.session_id})
                print('\n已请求协作式中断。')
                continue
            if not line: continue
            try:
                if line == '/quit': break
                command, _, arg = line.partition(' ')
                if command in {'/model', '/submodel'}:
                    values = gateway.dispatch('models.get', {'session_id': session.session_id})
                    if arg:
                        values['main_model' if command == '/model' else 'sub_model'] = '' if arg == '-' else arg
                        values = gateway.dispatch('models.set', {'session_id': session.session_id,
                            'main_model': values['main_model'], 'sub_model': values['sub_model']})
                    print(f"主模型：{values['effective_main']}；子模型：{values['effective_sub']}；设置用于下一轮。")
                elif command == '/checkpoint':
                    print(gateway.dispatch('checkpoints.create', {'session_id': session.session_id, 'label': arg}))
                elif command == '/checkpoints':
                    print(gateway.dispatch('checkpoints.list', {'session_id': session.session_id}))
                elif command == '/restore' and arg.startswith('--checkpoint '):
                    result = gateway.dispatch('checkpoints.restore', {'session_id': session.session_id,
                        'checkpoint_id': arg[len('--checkpoint '):].strip()})
                    session.event_sink = None
                    response = gateway.dispatch('session.resume', {'session_id': result['session_id']})
                    session = gateway.session(result['session_id'])
                    session.event_sink = render
                    print('已恢复已捕获文件，并进入对话分支；原始会话与收据保留。')
                    for message in response['messages']: print(message['role'] + '：' + message['text'])
                elif command in {'/accept', '/reject', '/restore'}:
                    if not arg: raise ValueError(f'用法：{command} <change-id>')
                    print(gateway.dispatch('changes.accept' if command == '/accept' else 'changes.restore',
                                          {'session_id': session.session_id, 'change_id': arg}))
                elif line == '/stop':
                    gateway.dispatch('session.interrupt', {'session_id': session.session_id})
                    print('已请求协作式中断。')
                elif line in {'/plan', '/evidence', '/changes', '/delivery'}:
                    print(gateway.dispatch('inspection.get', {'session_id': session.session_id, 'kind': line[1:]})['text'])
                elif line.startswith('/approve '):
                    parts = line.split()
                    if len(parts) != 3: print('用法：/approve <request-id> once|session|deny')
                    else: print(gateway.dispatch('approval.respond', {'session_id': session.session_id, 'request_id': parts[1], 'choice': parts[2]}))
                elif line.startswith('/'):
                    print(gateway.dispatch('slash.exec', {'session_id': session.session_id, 'command': line}).get('output', ''))
                else:
                    gateway.dispatch('prompt.submit', {'session_id': session.session_id, 'text': line})
            except ValueError as error:
                print('操作未执行：' + str(error))
    finally:
        gateway.disconnect()
    return 0
