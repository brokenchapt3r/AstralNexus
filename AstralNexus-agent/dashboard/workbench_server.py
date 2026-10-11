"""Persistent web client host with the existing monitor embedded at /monitor."""
from __future__ import annotations

import argparse
import atexit
import json
import importlib.util
import logging
import os
from pathlib import Path
import shutil
import threading
import sys
import time
import uuid
from typing import Any, Optional

from AstralNexus.core.runtime_state import read_authoritative_cursor
from AstralNexus.runtime import app_boot, cmdline, delivery_recovery, http_proxy
from AstralNexus.runtime.directory_picker import (
    DirectoryPickerError, mount_directory_picker)
from AstralNexus.runtime.open_in_app import OpenInAppService, sample_ssh_fact
from AstralNexus.runtime.dshutil.launch_environment import (
    LaunchEnvironmentLayer, create_launch_environment_snapshot,
    launched_through_ssh)
from AstralNexus.runtime.language_service import LanguageService
from AstralNexus.runtime.plugin_host import BUILTIN_MANIFESTS, PluginHost
from AstralNexus.runtime.run_coordinator import RunCoordinator
from AstralNexus.runtime.account.platform import OAuthCallbackChannel
from AstralNexus.runtime.authorization import AuthorizationRegistry
from AstralNexus.runtime.typert.loader import TypertLoader
from AstralNexus.runtime.typert.registry import TypertRegistry
from AstralNexus.runtime.workbench import HostLock, WorkbenchStore, WorkflowEngine
from AstralNexus.runtime.workbench_chat import ChatService
from .live_server import LiveDashboardServer


class WorkbenchHost:
    def __init__(self, home: Path, lsp_command: list[str] | None = None):
        self.home = Path(home).resolve()
        self.lock = HostLock(self.home)
        self.store = WorkbenchStore(self.home)
        self.engine = WorkflowEngine(self.store)
        self.chat = ChatService(self.store)
        # Delivery orphans.  ``workspace_publish.recover_pending`` shipped with
        # the delivery contract and never had a production caller, so a publish
        # the host was killed in the middle of left a pending journal and a
        # half-published tree that nothing resumed -- live fix4 published 43
        # correct deliverables into the wrong root and only a human reading the
        # journal by hand found out.  Same startup-recovery slot as
        # ``self.plugins.recover()`` below; every outcome is recorded, and an
        # unreadable journal must not stop the sweep.
        self.delivery_recovery = delivery_recovery.sweep(
            self.home, self.store.snapshot().get('sessions') or [])
        for outcome in self.delivery_recovery:
            logging.getLogger('astralnexus.host').warning(
                'delivery recovery: %s delivery_id=%s root=%s%s',
                outcome.get('outcome'), outcome.get('delivery_id'), outcome.get('root'),
                (' error=' + str(outcome.get('error'))) if outcome.get('error') else '')
        # Process-wide outbound proxy policy (DSH B1 util): the launcher
        # resolves, reports, and installs it BEFORE the first plugin mounts,
        # so every outbound HTTP consumer — the account platform requests
        # included — reads the one resolved answer from the published
        # environment. Loopback always bypasses, so the host's own web
        # server, callback route, and local test servers never route.
        self._proxy_diagnostics: list[str] = []
        self._proxy_restore = http_proxy.install_proxy_from_environment(
            create_launch_environment_snapshot(
                [LaunchEnvironmentLayer('process', dict(os.environ))]),
            self._proxy_diagnostics.append)
        self._proxy_restored = False
        for message in self._proxy_diagnostics:
            logging.getLogger('astralnexus.host').warning('proxy: %s', message)
        # Real plugin lifecycle (DSH B1 core loop): declaration, dependency
        # resolution, config validation, start/stop, restart recovery. The web
        # switches below drive this host, not a display-only list.
        self.plugins = PluginHost(self.store)
        self.plugins.seed_builtins(BUILTIN_MANIFESTS)
        # Typert runtime registry (DSH B1): generated reflection/invocation
        # contributions registered by plugins at start, withdrawn at stop.
        self.typert = TypertRegistry()
        self.plugins.services['typert'] = self.typert
        self.plugins.typert_loader = TypertLoader(self.typert)
        # Directory-picking seam (DSH B1 host family): one boot-time sample
        # resolves the backend (native chooser vs browse primitives) exactly
        # once, so the mounted capability stays stable for the host's life —
        # the auto composition's contract. The bind host is loopback-only in
        # main() below, the fact the resolver's first rail reads.
        launch_environment = create_launch_environment_snapshot(
            [LaunchEnvironmentLayer('process', dict(os.environ))])
        self.directory_picker, self.directory_picker_kind = mount_directory_picker(
            bind_host='127.0.0.1',
            ssh=launched_through_ssh(launch_environment),
            env=os.environ)
        self.plugins.services['directory_picker'] = self.directory_picker
        # Open-in-app seam (DSH B1 host family): the resolved application
        # catalog, per-app icons, and the launch endpoint. The catalog
        # resolves lazily once per host life on first request; the SSH fact
        # is sampled once here, as upstream samples it at plugin load. The
        # timeouts are the host composition's config choice (upstream apps
        # supply them through validated yml rows).
        self.open_in_app = OpenInAppService(
            config={'probe_timeout_ms': 5000, 'icon_timeout_ms': 8000,
                    'launch_watch_ms': 2000},
            fence=self._open_in_app_fence,
            internals={'ssh': sample_ssh_fact()})
        # Authorization seam (DSH B1): registry of credential-obtaining flows.
        # The credentials provider is resolved lazily per attempt so the
        # registry survives the opt-in credentials plugin's lifecycle.
        self.authorization = AuthorizationRegistry(
            credentials=lambda: self.plugins.services.get('credentials'))
        self.plugins.services['authorization'] = self.authorization
        # Exact-route channel mounted once at /oauth/callback below; the
        # account plugin registers its per-attempt handler through it
        # (upstream ctx.get("webServer").register).
        self.account_channel = OAuthCallbackChannel()
        self.plugins.services['oauth_callback_channel'] = self.account_channel
        # Boot reconciliation happens at construction: a host that serves
        # requests must already reflect desired plugin state (restart-consistent
        # even when start_services() threads are not used, e.g. test clients).
        self.plugins.recover()
        self.chat.plugin_host = self.plugins
        self.services: dict[tuple[str, str], LanguageService] = {}
        self.service_lock = threading.RLock()
        self.lsp_command = lsp_command
        self.python_command = [sys.executable, '-m', 'pylsp'] if importlib.util.find_spec('pylsp') else None
        self.worker: threading.Thread | None = None
        self.closed = False
        waiting = self.home / 'waiting'
        waiting.mkdir(exist_ok=True)
        self.pointer = self.home / 'selected_run.json'
        self._pointer_lock = threading.Lock()
        selected = self.store.snapshot()['selected']
        if selected:
            self.select(selected)
        self.dashboard = LiveDashboardServer(waiting, pointer_path=self.pointer)
        # Historical traces can be hundreds of MB. The tailer loads the selected
        # run off the HTTP thread; opening chat must not wait for that replay.
        self.app = self.dashboard._app
        self._routes()

    def select(self, ident: str) -> None:
        session = self.store.session(ident)
        self.store.select(ident)
        # This is the client's selected view, never the Agent's checkpoint.
        run = session['run_dir'] or str(self.home / 'waiting')
        if not session['run_dir']:
            waiting = Path(run)
            waiting.mkdir(exist_ok=True)
            (waiting / 'trace.jsonl').touch(exist_ok=True)
        self._write_pointer(str(run))

    def _write_pointer(self, run: str) -> None:
        # Concurrent session registrations used to share one 'selected_run.tmp'
        # name: the writers interleaved content and os.replace could fail with
        # WinError 5 while the target was open, which the PermissionError handler
        # then reported as a misleading "unavailable" 503.  A unique temp name plus
        # a serialized, retrying publish keeps the pointer torn-free and the
        # response honest (R27 concurrency proof).
        payload = json.dumps({'run_dir': run})
        last: OSError | None = None
        with self._pointer_lock:
            temporary = self.pointer.with_name(
                '%s.%s.tmp' % (self.pointer.name, uuid.uuid4().hex))
            temporary.write_text(payload, encoding='utf-8')
            for attempt in range(20):
                try:
                    temporary.replace(self.pointer)
                    return
                except OSError as exc:
                    last = exc
                    time.sleep(0.05 * (attempt + 1))
            try:
                temporary.unlink()
            except OSError:
                pass
            raise OSError('cannot publish selected run pointer: %s' % last)


    def query_lsp(self, session_id: str, args: dict[str, Any]) -> dict[str, Any]:
        session = self.store.session(session_id)
        root = session['workspace']
        suffix = Path(str(args['path'])).suffix.lower()
        language = 'python' if suffix in {'.py', '.pyi'} else 'typescript'
        if suffix not in {'.py', '.pyi', '.ts', '.tsx', '.js', '.jsx'}:
            raise ValueError('no configured language provider for this file extension')
        key = (root, language)
        command = self.python_command if language == 'python' else self.lsp_command
        with self.service_lock:
            service = self.services.get(key)
            if service is None or service.closed or service.process.poll() is not None:
                if service is not None:
                    service.close()
                if not command:
                    raise ValueError('LSP provider is not configured')
                service = LanguageService(Path(root), command)
                self.services[key] = service
        return service.query(str(args['operation']), str(args['path']), int(args.get('line', 0)), int(args.get('character', 0)))

    def authority(self, ident: str) -> dict[str, Any]:
        session = self.store.session(ident)
        if not session['run_dir']:
            return {'coordinator': None, 'cursor': None}
        root = Path(session['run_dir'])
        current = None
        database = root / '.astralnexus_state/runs.sqlite'
        if database.exists():
            import sqlite3
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute('SELECT * FROM runs').fetchall()
                if len(rows) == 1:
                    current = dict(rows[0])
        return {'coordinator': current, 'cursor': read_authoritative_cursor(root)}

    def _credentials_projection(self) -> dict[str, Any]:
        """Read-only credentials-seam projection: metadata only, NEVER values.

        A configuration surface describes a reference without ever seeing its
        secret; this projection keeps that rule at the HTTP boundary.
        """
        provider = self.plugins.services.get('credentials')
        if provider is None:
            return {'available': False}
        return {
            'available': True,
            'filename': provider.spec['filename'],
            'watch': provider.spec['watch'],
            'refs': {ref: provider.describe(ref) for ref in provider.list_refs()},
            'records': provider.list_records(),
        }

    def _authorization_projection(self) -> dict[str, Any]:
        """Read-only view of the authorization seam: registered flows and
        whether an attempt is in flight. Never carries credentials or the
        conversation itself."""
        return {
            'credentials_available': self.plugins.services.get('credentials') is not None,
            'flows': self.authorization.list(),
        }

    def _account_projection(self) -> dict[str, Any]:
        """Read-only view of the account seam: state machine view only, never
        a token. The attempt view may carry the platform-owned authorizeUrl
        (validated, no local secret); the stored grant never crosses."""
        provider = self.plugins.services.get('deepseek-account')
        if provider is None:
            return {'available': False}
        return {'available': True, 'state': provider.get_state()}

    def _open_in_app_fence(self) -> Optional[int]:
        """The open-in-app trust fence: Host/Origin verification defeating
        DNS rebinding and cross-site calls (the connection service's fence,
        subset-ported — its login-token browser authentication belongs to
        the unported dsh-client-connection and this host binds loopback
        only). Returns a rejection status, or None to let the request
        through."""
        from urllib.parse import urlparse
        from flask import request
        loopback = ('localhost', '127.0.0.1', '[::1]', '::1')
        origin = request.headers.get('Origin')
        if origin is not None:
            if urlparse(origin).hostname not in loopback:
                return 403
        host_header = request.headers.get('Host') or ''
        hostname = host_header.rsplit(':', 1)[0] if ':' in host_header \
            else host_header
        if hostname.startswith('[') and hostname.endswith(']'):
            hostname = hostname[1:-1]
        if hostname not in loopback and hostname != '':
            return 403
        return None

    def _proxy_projection(self) -> dict[str, Any]:
        """Read-only view of the outbound proxy policy: routing metadata and
        rejection diagnostics only. A proxy URL may carry credentials, so the
        resolved values NEVER cross the wire — presence booleans instead; the
        bypass list is host names and safe to show."""
        policy = http_proxy.active_policy()
        if policy is None:
            return {'installed': False, 'diagnostics': list(self._proxy_diagnostics)}
        return {
            'installed': True,
            'source': policy.get('source'),
            'http_proxy': policy.get('http_proxy') is not None,
            'https_proxy': policy.get('https_proxy') is not None,
            'no_proxy': policy.get('no_proxy', ''),
            'diagnostics': list(self._proxy_diagnostics),
        }

    def _typert_projection(self) -> dict[str, Any]:
        """Read-only wire-safe view of the typert registry (metadata, never
        schema objects or codec factories)."""
        registry = self.typert

        def descriptor_view(descriptor: dict[str, Any]) -> dict[str, Any]:
            return {field: descriptor.get(field)
                    for field in ('id', 'service', 'namespace', 'method', 'mode')}

        return {
            'schemas': [{'key': key, 'name': record['name'], 'package': record['package'],
                         'face': record['face']} for key, record in registry.schemas.items()],
            'packages': [{'key': record['key'], 'package': record['package'], 'face': record['face']}
                         for record in registry.packages.values()],
            'local_endpoints': [descriptor_view(item) for item in registry.local_store.list()],
            'remote_packages': registry.remote_store.packages(),
            'lookups': registry.lookup_store.keys(),
            'loader_entries': (self.plugins.typert_loader.registered_entries()
                               if self.plugins.typert_loader is not None else []),
        }

    def _routes(self) -> None:
        from flask import jsonify, request
        app = self.app
        app.add_url_rule('/monitor', 'embedded_monitor', app.view_functions['index'])
        app.view_functions['index'] = lambda: Path(__file__).with_name('workbench.html').read_text(encoding='utf-8')

        @app.errorhandler(ValueError)
        @app.errorhandler(KeyError)
        def bad_request(exc):
            return jsonify({'error': str(exc)}), 400

        from AstralNexus.runtime.account.protocol import PlatformAuthError

        @app.errorhandler(PlatformAuthError)
        def account_auth_error(exc):
            # Account failures carry a wire-safe code; an expired credential
            # answers 401 so callers can route it to the sign-in surface.
            status = 401 if exc.code == 'expired' else 400
            return jsonify({'error': str(exc), 'code': exc.code}), status

        @app.errorhandler(DirectoryPickerError)
        def directory_picker_error(exc):
            # Browse primitives fail typed: the closed business code lets a
            # surface map the failure without string matching.
            return jsonify({'error': str(exc), 'code': exc.code,
                            'path': exc.path}), 400

        @app.errorhandler(PermissionError)
        def permission_error(exc):
            # Sandbox boundary (R18实证, 台账 §29): the environment refuses
            # anonymous pipes, so stdio-carried providers (LSP servers and
            # their kin) cannot spawn here. The refusal surfaces as a typed
            # unavailable response — never an unhandled 500, and never a
            # silent success: 错误回传不等于能力可用, but it must stay
            # machine-readable at the HTTP boundary.
            detail = str(exc) or getattr(exc, 'strerror', None) or 'permission denied'
            return jsonify({'error': detail, 'status': 'unavailable'}), 503

        @app.get('/api/host')
        def host_snapshot():
            snapshot = self.store.snapshot()
            if snapshot['selected']:
                snapshot['authority'] = self.authority(snapshot['selected'])
            # Live plugin lifecycle projection (never a hardcoded readiness list).
            snapshot['plugins'] = self.plugins.snapshot()
            snapshot['typert'] = self._typert_projection()
            snapshot['credentials'] = self._credentials_projection()
            snapshot['authorization'] = self._authorization_projection()
            snapshot['account'] = self._account_projection()
            snapshot['proxy'] = self._proxy_projection()
            snapshot['directory_picker'] = {'kind': self.directory_picker_kind}
            snapshot['services'] = [
                {'id': 'monitor', 'status': 'ready'}, {'id': 'sessions', 'status': 'ready'},
                {'id': 'workflow', 'status': 'ready', 'mode': 'argv_pipeline'},
                {'id': 'schedule', 'status': 'ready', 'modes': ['at', 'fixed_interval']},
                {'id': 'lsp', 'status': 'configured' if self.lsp_command or self.python_command else 'unavailable',
                 'languages': {'typescript': bool(self.lsp_command), 'python': bool(self.python_command)}, 'instances': len(self.services)}]
            return jsonify(snapshot)

        @app.get('/api/host/plugin-catalog')
        def plugin_catalog():
            # Read-only inventory: listing never starts a plugin implicitly.
            return jsonify({'items': self.plugins.inventory()})

        @app.get('/api/host/typert')
        def typert_projection():
            # Read-only registry projection: what plugins actually registered.
            return jsonify(self._typert_projection())

        @app.get('/api/host/credentials')
        def credentials_projection():
            # Read-only seam projection: configured/source/writable metadata,
            # never a secret value.
            return jsonify(self._credentials_projection())

        @app.get('/api/host/authorization')
        def authorization_projection():
            # Read-only seam projection: flows and in-flight flags, never the
            # conversation or a credential.
            return jsonify(self._authorization_projection())

        @app.post('/api/host/authorization/<path:key>/cancel')
        def authorization_cancel(key):
            # A transport answers a Cancel button on a second call, with no
            # handle on the running attempt's signal — this is that call.
            return jsonify({'key': key, 'cancelled': self.authorization.cancel(key)})

        @app.get('/api/host/directory-picker')
        def directory_picker_projection():
            # The discriminated capability kind: a surface switches on it and
            # hides the picking affordance for an unknown kind rather than fail.
            return jsonify({'kind': self.directory_picker_kind})

        @app.get('/api/host/directory-picker/list')
        def directory_picker_list():
            if self.directory_picker_kind != 'browse':
                return jsonify({'error': 'the mounted directory-picker capability '
                                         'does not serve listing'}), 400
            capability = self.directory_picker.capability()
            return jsonify(capability['list'](request.args.get('path') or None))

        @app.post('/api/host/directory-picker/create-directory')
        def directory_picker_create():
            if self.directory_picker_kind != 'browse':
                return jsonify({'error': 'the mounted directory-picker capability '
                                         'does not serve creation'}), 400
            body = request.get_json(force=True) or {}
            capability = self.directory_picker.capability()
            created = capability['createDirectory'](
                str(body.get('path', '')), str(body.get('name', '')))
            return jsonify({'path': created})

        @app.post('/api/host/directory-picker/pick')
        def directory_picker_pick():
            # The native backend's single blocking call: it returns when the
            # operator picks a folder or cancels at the OS dialog.
            if self.directory_picker_kind != 'native':
                return jsonify({'error': 'the mounted directory-picker capability '
                                         'does not serve picking'}), 400
            selected = self.directory_picker.capability()['pick']()
            return jsonify({'path': selected})

        def _open_in_app_response(result):
            from flask import Response
            status, headers, body = result
            return Response(body, status=status, headers=headers)

        @app.route('/api/host/open-in-app/apps', methods=['GET', 'POST'])
        def open_in_app_apps():
            return _open_in_app_response(self.open_in_app.handle_apps(request.method))

        @app.route('/api/host/open-in-app/icon/<path:app_id>',
                   methods=['GET', 'POST'])
        def open_in_app_icon(app_id):
            return _open_in_app_response(
                self.open_in_app.handle_icon(request.method, app_id))

        @app.route('/api/host/open-in-app/open', methods=['POST', 'GET'])
        def open_in_app_open():
            try:
                body = request.get_data()
            except Exception:
                body = None
            return _open_in_app_response(self.open_in_app.handle_open(
                request.method, request.content_type or '', body))

        @app.route('/oauth/callback', methods=['GET', 'POST'])
        def oauth_callback():
            # The exact platform callback route; forwarded to the account
            # plugin's live handler. The handler holds this request thread
            # until the attempt settles (upstream holds the ServerResponse).
            from flask import Response
            status, headers, body = self.account_channel.dispatch(
                request.method, request.path, list(request.args.items(multi=True)))
            return Response(body, status=status, headers=headers)

        @app.get('/api/host/account')
        def account_projection():
            return jsonify(self._account_projection())

        @app.post('/api/host/account/sign-in')
        def account_sign_in():
            provider = self.plugins.services.get('deepseek-account')
            if provider is None:
                raise ValueError('the deepseek-account-platform plugin is not enabled')
            body = request.get_json(silent=True) or {}
            client = body.get('client')
            login_source = body.get('login_source')
            if not isinstance(client, dict) or login_source not in ('web', 'qrcode', 'client'):
                raise ValueError('client and a login_source of web|qrcode|client are required')
            # The callback origin is THIS server as the browser reaches it —
            # derived from the request, never supplied by the caller.
            state = provider.start_sign_in(client, request.host_url, login_source)
            return jsonify({'state': state})

        @app.post('/api/host/account/cancel')
        def account_cancel():
            provider = self.plugins.services.get('deepseek-account')
            if provider is None:
                raise ValueError('the deepseek-account-platform plugin is not enabled')
            body = request.get_json(silent=True) or {}
            ident = body.get('id')
            if not isinstance(ident, str) or not ident:
                raise ValueError('id is required')
            return jsonify({'state': provider.cancel_sign_in(ident)})

        @app.post('/api/host/account/sign-out')
        def account_sign_out():
            provider = self.plugins.services.get('deepseek-account')
            if provider is None:
                raise ValueError('the deepseek-account-platform plugin is not enabled')
            body = request.get_json(silent=True) or {}
            client = body.get('client')
            if not isinstance(client, dict):
                raise ValueError('client is required')
            return jsonify({'state': provider.sign_out(client)})

        @app.post('/api/host/plugin-catalog/<ident>')
        def plugin_toggle(ident):
            body = request.get_json(silent=True) or {}
            enabled = body.get('enabled')
            if not isinstance(enabled, bool):
                raise ValueError('enabled must be a boolean')
            return jsonify(self.plugins.set_enabled(ident, enabled))

        @app.get('/api/host/plugin-catalog/<ident>/config')
        def plugin_config_get(ident):
            return jsonify(self.plugins.get_config(ident))

        @app.post('/api/host/plugin-catalog/<ident>/config')
        def plugin_config_put(ident):
            body = request.get_json(silent=True) or {}
            if 'config' not in body:
                raise ValueError('config is required')
            return jsonify(self.plugins.put_config(ident, body['config']))

        @app.post('/api/host/sessions')
        def register():
            body = request.get_json()
            ident = self.store.register_session(str(body.get('title', '')), str(body['workspace']), str(body.get('run_dir', '')))
            self.select(ident)
            return jsonify({'id': ident}), 201

        @app.post('/api/host/select/<ident>')
        def select(ident):
            with self.service_lock:
                self.select(ident)
            return jsonify({'selected': ident})

        @app.post('/api/host/pause/<ident>')
        def pause(ident):
            session = self.store.session(ident)
            current = self.authority(ident)['coordinator']
            if not current:
                raise ValueError('no coordinated Agent run is attached')
            status = RunCoordinator(Path(session['run_dir']), run_id=current['run_id']).request_pause()
            return jsonify({'status': status})

        @app.post('/api/host/workflows')
        def submit():
            body = request.get_json()
            ident = self.store.submit(str(body['session_id']), body['workflow'])
            return jsonify({'id': ident}), 202

        @app.get('/api/host/jobs/<ident>')
        def job(ident):
            with self.store.db() as db:
                row = db.execute('SELECT * FROM jobs WHERE id=?', (ident,)).fetchone()
            if row is None:
                return jsonify({'error': 'unknown job'}), 404
            value = dict(row)
            value['spec'] = json.loads(value['spec'])
            value['result'] = json.loads(value['result']) if value['result'] else None
            return jsonify(value)

        @app.post('/api/host/jobs/<ident>/cancel')
        def cancel(ident):
            self.store.cancel(ident)
            return jsonify({'cancel_requested': True})

        @app.post('/api/host/schedules')
        def schedule():
            body = request.get_json()
            ident = self.store.schedule(str(body['session_id']), body['workflow'], float(body['due']), float(body.get('interval', 0)))
            return jsonify({'id': ident}), 201

        @app.post('/api/host/schedules/<ident>')
        def schedule_enabled(ident):
            self.store.enable_schedule(ident, bool(request.get_json()['enabled']))
            return jsonify({'updated': ident})

        @app.post('/api/host/lsp')
        def lsp():
            body = request.get_json()
            return jsonify(self.query_lsp(str(body['session_id']), body))

        @app.get('/api/host/chat/<session>')
        def conversation(session):
            return jsonify(self.chat.snapshot(session, int(request.args.get('after', 0))))

        @app.get('/api/host/plugins/<session>')
        def tool_plugins(session):
            return jsonify(self.chat.plugins(session))

        @app.post('/api/host/plugins/<session>')
        def save_tool_plugins(session):
            return jsonify(self.chat.set_plugins(session, request.get_json()['disabled']))

        @app.get('/api/host/permissions/<session>')
        def permission_settings(session):
            return jsonify(self.chat.permissions(session))

        @app.post('/api/host/permissions/<session>')
        def save_permission_settings(session):
            return jsonify(self.chat.set_permissions(session, request.get_json()['mode']))

        @app.get('/api/host/models/<session>')
        def model_settings(session):
            return jsonify(self.chat.models(session))

        @app.post('/api/host/models/<session>')
        def save_model_settings(session):
            return jsonify(self.chat.set_models(session, request.get_json()))

        @app.post('/api/host/chat/<session>')
        def chat_submit(session):
            if not self.plugins.is_active('chat'):
                state = next((p for p in self.plugins.snapshot() if p['id'] == 'chat'), None)
                reason = (state or {}).get('last_error') or 'chat 插件当前未处于 active 状态'
                raise ValueError(f'对话插件已停用（{reason}）；在“插件与工具”页启用 chat 后重试')
            body = request.get_json()
            ident = self.chat.submit(session, str(body['message']), str(body['request_id']))
            return jsonify({'id': ident}), 202

        @app.post('/api/host/chat/<session>/<ident>/cancel')
        def chat_cancel(session, ident):
            self.chat.cancel(session, ident)
            return jsonify({'cancel_requested': True})

        @app.post('/api/host/chat/<session>/approval/<ident>')
        def chat_approval(session, ident):
            self.chat.approve(session, ident, str(request.get_json()['choice']))
            return jsonify({'answered': True})

        @app.post('/api/host/chat/<session>/<ident>/monitor')
        def chat_monitor(session, ident):
            with self.store.db() as db:
                row = db.execute('SELECT run_dir FROM chat_turns WHERE id=? AND session_id=?', (ident, session)).fetchone()
                if not row or not row['run_dir'] or not Path(row['run_dir']).is_dir():
                    raise ValueError('this turn has no available Agent run')
                db.execute('UPDATE sessions SET run_dir=? WHERE id=?', (row['run_dir'], session))
            self.select(session)
            return jsonify({'selected': session})

    def start_services(self) -> None:
        if self.worker is not None:
            return
        self.worker = threading.Thread(target=self.engine.serve, daemon=True, name='astralnexus-workflow-host')
        self.worker.start()
        self.chat.start()
        self.dashboard._thread = threading.Thread(target=self.dashboard._tail_trace, daemon=True)
        self.dashboard._thread.start()

    def close(self) -> None:
        if self.closed:
            return
        self.chat.close()
        # Graceful reverse-dependency-order plugin disposal; errors are recorded
        # per plugin, never swallowed into a silent host shutdown.
        self.plugins.shutdown()
        self.engine.stop.set()
        if self.worker:
            self.worker.join(timeout=10)
            if self.worker.is_alive():
                raise RuntimeError('workflow worker has not stopped; retain host lock')
        self.dashboard.stop()
        if self.dashboard._thread:
            self.dashboard._thread.join(timeout=3)
        for service in self.services.values():
            service.close()
        self.lock.close()
        if not self._proxy_restored:
            # restore the process environment exactly as the installer found it
            self._proxy_restored = True
            self._proxy_restore()
        self.closed = True


def main() -> None:
    # fail-loud guard (dsh-app-boot contract): any uncaught exception at any
    # point in the process lifetime becomes one labelled stderr diagnostic
    # and a hard exit(1); stdout stays untouched for protocol consumers.
    app_boot.install_fail_loud('dsh-workbench')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--port', type=int, default=5073)
    parser.add_argument('--lsp-command', help='JSON argv; defaults to the project TypeScript LSP installation')
    # the launcher parses only its own flags; everything after them is handed
    # to the tree verbatim through the cmdlineArgs service (dsh-cmdline)
    args, remainder = parser.parse_known_args()
    # layered .env: inherited > host home > harness home, bootstrap-only
    # names refused fail-loud with the remedy (dsh-app-boot loadLayeredEnv)
    app_boot.load_layered_env('dsh-workbench', cwd=str(args.home))
    package = Path(__file__).resolve().parents[3] / '03_验证脚本/.tooling/lsp/node_modules/typescript-language-server/lib/cli.mjs'
    node = shutil.which('node')
    command = json.loads(args.lsp_command) if args.lsp_command else ([node, str(package), '--stdio'] if node and package.is_file() else None)
    host = WorkbenchHost(args.home, command)
    atexit.register(host.close)

    def request_exit(code: int) -> None:
        """Bounded exit request handed to app plugins via appExit."""
        try:
            host.close()
        finally:
            os._exit(code)

    cmdline.provide_cmdline(host.plugins.services, {
        'args': remainder, 'exit': request_exit})
    # startup audit, persistent-host adaptation: inactive entries surface
    # through the warn face while the per-plugin status face keeps authority
    # (the rejecting bin-shaped contract stays in app_boot.audit_startup_entries)
    app_boot.audit_startup_warnings(host.plugins, 'dsh-workbench')
    from werkzeug.serving import make_server
    server = make_server('127.0.0.1', args.port, host.app, threaded=True)
    host.start_services()
    print(f'AstralNexus persistent client: http://127.0.0.1:{server.server_port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        host.close()


if __name__ == '__main__':
    main()
