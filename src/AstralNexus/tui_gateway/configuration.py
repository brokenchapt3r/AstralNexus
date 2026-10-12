"""Validated persistent presentation settings; no unsupported write success."""
from __future__ import annotations
import json
import time
from typing import Any
from AstralNexus.runtime.workbench import WorkbenchStore

DEFAULTS: dict[str, Any] = {'busy_input_mode': 'interrupt', 'mouse_tracking': 'off',
    'tui_auto_resume_recent': False, 'tui_agents_nudge': True, 'details_mode': 'collapsed'}


class PresentationSettings:
    def __init__(self, store: WorkbenchStore): self.store = store
    def get(self) -> dict[str, Any]:
        with self.store.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key='tui:display'").fetchone()
        return {**DEFAULTS, **(json.loads(row[0]) if row else {})}
    def set(self, key: str, value: Any) -> dict[str, Any]:
        aliases = {'compact': 'tui_compact', 'mouse': 'mouse_tracking', 'statusbar': 'tui_statusbar'}
        key = aliases.get(key, key)
        display = self.get()
        if key.startswith('details_mode.'):
            section = key.partition('.')[2]
            if section not in {'thinking', 'tools', 'subagents', 'activity'} or value not in {'', 'hidden', 'collapsed', 'expanded'}:
                raise ValueError('invalid detail section or mode')
            sections = dict(display.get('sections') or {})
            if value: sections[section] = value
            else: sections.pop(section, None)
            display['sections'] = sections
        else:
            enums = {'details_mode': {'hidden', 'collapsed', 'expanded'}, 'mouse_tracking': {'off', 'wheel', 'buttons', 'all'},
                'tui_statusbar': {'off', 'on', 'top', 'bottom'}, 'busy_input_mode': {'interrupt', 'queue'}}
            booleans = {'tui_compact', 'bell_on_complete', 'tui_auto_resume_recent', 'tui_agents_nudge', 'inline_diffs', 'streaming'}
            if key in enums:
                if value not in enums[key]: raise ValueError('invalid presentation setting: ' + key)
            elif key in booleans:
                if value in ('on', 'true'): value = True
                elif value in ('off', 'false'): value = False
                if not isinstance(value, bool): raise ValueError('boolean setting required: ' + key)
            else:
                raise ValueError('unsupported presentation setting: ' + key)
            display[key] = value
        with self.store.db() as db:
            for name, item in [('tui:display', json.dumps(display)), ('tui:config_mtime', str(time.time()))]:
                db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (name, item))
        return display
