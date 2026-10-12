"""One persistent model setting contract shared by host chat and TUI."""
from __future__ import annotations
import json
import re
from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from AstralNexus.runtime.workbench import WorkbenchStore


class SessionSettings:
    def __init__(self, store: WorkbenchStore): self.store = store

    def models(self, session: str) -> dict[str, Any]:
        from .model_selector import AVAILABLE_MODELS, get_model_config
        self.store.session(session)
        with self.store.db() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', ('models:' + session,)).fetchone()
        saved = json.loads(row['value']) if row else {'main_model': '', 'sub_model': ''}
        default = str(get_model_config()['model'])
        main = saved['main_model'] or default
        return {**saved, 'effective_main': main, 'effective_sub': saved['sub_model'] or main,
                'runtime_default': default, 'catalog': [{'id': key, 'name': value['name']} for key, value in AVAILABLE_MODELS.items()]}

    def set_models(self, session: str, values: dict[str, Any]) -> dict[str, Any]:
        self.store.session(session)
        normalized = {}
        for key in ('main_model', 'sub_model'):
            value = values.get(key, '')
            if not isinstance(value, str) or (value and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,199}', value)):
                raise ValueError('invalid model id: ' + key)
            if value.lower() == 'qwen3.8-max':
                raise ValueError('qwen3.8-max remains disabled for this workbench')
            normalized[key] = value
        with self.store.db() as db:
            db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('models:' + session, json.dumps(normalized)))
        return self.models(session)
