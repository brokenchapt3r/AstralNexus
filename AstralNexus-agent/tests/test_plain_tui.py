from AstralNexus.tui_gateway import entry, plain


def test_plain_client_has_no_ansi_animation_or_model_call(tmp_path, monkeypatch, capsys):
    gateway = entry.Gateway(home=tmp_path)
    monkeypatch.setattr(entry, 'Gateway', lambda: gateway)
    commands = iter(['/plan', '/quit'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(commands))
    monkeypatch.setattr(gateway, 'submit', lambda *args: (_ for _ in ()).throw(AssertionError('no model requested')))
    assert plain.run_plain() == 0
    output = capsys.readouterr().out
    assert '\x1b' not in output and '尚未绑定' in output
    assert gateway._disconnected


def test_screen_reader_entry_does_not_require_node(monkeypatch):
    from AstralNexus.cli import native_tui
    monkeypatch.setenv('ASTRALNEXUS_TUI_SCREEN_READER', 'true')
    monkeypatch.setattr(native_tui, 'resolve_node_executable', lambda: None)
    monkeypatch.setattr(plain, 'run_plain', lambda: 0)
    assert native_tui.native_tui_available()
    assert native_tui.launch_native_tui() == 0


def test_plain_model_settings_checkpoint_and_bad_input_are_offline(tmp_path, monkeypatch, capsys):
    gateway = entry.Gateway(home=tmp_path)
    monkeypatch.setattr(entry, 'Gateway', lambda: gateway)
    commands = iter(['/model local-main', '/submodel local-sub', '/checkpoint before', '/checkpoints', '/restore missing', '/quit'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(commands))
    monkeypatch.setattr(gateway, 'submit', lambda *args: (_ for _ in ()).throw(AssertionError('no model requested')))
    assert plain.run_plain() == 0
    output = capsys.readouterr().out
    assert 'local-main' in output and 'local-sub' in output and '操作未执行' in output
    assert '\x1b' not in output
    selected = gateway.store.host.session(gateway.current_session_id)
    from AstralNexus.runtime.session_settings import SessionSettings
    assert SessionSettings(gateway.store.host).models(selected['id'])['effective_sub'] == 'local-sub'
