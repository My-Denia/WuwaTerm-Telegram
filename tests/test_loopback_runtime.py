"""Exercise actual SDK and translation pipeline with loopback-only services."""
from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time

import pytest

from wuwaterm.application import LLMTranslationError, build_translator
from wuwaterm_telegram.bot import BotConfig
from wuwaterm_telegram.channel import _translate_channel_input, channel_post_handler
from wuwaterm_telegram.channel_reply_schema import parse_channel_reply_payload
from wuwaterm_telegram.runtime_keys import CHANNEL_RUNTIME_KEY, CHAT_SETTINGS_KEY, TRANSLATOR_KEY
from wuwaterm_telegram.settings import ChatSettings
from wuwaterm_telegram.telegram_html import translate_html_async

from test_channel import channel_update, make_context


class LoopbackState:
    def __init__(self):
        self.lock = threading.Lock()
        self.updates = deque()
        self.calls = []
        self.model_requests = []
        self.model_mode = 'normal'
        self.model_entered = threading.Event()
        self.release_model = threading.Event()
        self.next_reply_id = 9000

    def enqueue(self, *updates):
        with self.lock:
            self.updates.extend(updates)

    def snapshot(self):
        with self.lock:
            return list(self.calls), list(self.model_requests)


@contextmanager
def loopback_service():
    state = LoopbackState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                from urllib.parse import parse_qs
                body = {key: values[-1] for key, values in parse_qs(raw.decode()).items()}
            method = self.path.rsplit('/', 1)[-1]
            with state.lock:
                state.calls.append((method, body))
            if method == 'completions':
                with state.lock:
                    state.model_requests.append(body)
                    mode = state.model_mode
                source = body['messages'][-1]['content']
                if mode == 'hold':
                    state.model_entered.set()
                    if not state.release_model.wait(10):
                        self.send_error(500)
                        return
                if mode == 'empty_once':
                    with state.lock:
                        state.model_mode = 'normal'
                    result = ''
                elif mode == 'add_tag':
                    result = source.replace('天地', '<i>world</i>')
                elif mode == 'add_entity':
                    result = source.replace('天地', 'world &amp;')
                elif mode == 'corrupt':
                    result = re.sub(r'__WUWA_HTML_[0-9a-f]{16}_[0-9]{4,}__', '', source, count=1)
                elif mode == 'to_zh':
                    result = source.replace('hello', '你好')
                else:
                    # Chinese source: terms reach the model as contextual
                    # references, so the model itself renders them.
                    result = (source.replace('天地', 'world').replace('你好', 'hello')
                              .replace('今汐', 'Jinhsi').replace('声骸', 'Echo'))
                payload = {'choices': [{'message': {'content': result}}]}
            elif method == 'getMe':
                payload = {'ok': True, 'result': {'id': 123456, 'is_bot': True, 'first_name': 'Fixture', 'username': 'fixture_bot'}}
            elif method == 'deleteWebhook':
                payload = {'ok': True, 'result': True}
            elif method == 'getUpdates':
                time.sleep(0.03)
                with state.lock:
                    result = list(state.updates)
                    state.updates.clear()
                payload = {'ok': True, 'result': result}
            elif method in ('sendMessage', 'editMessageText'):
                with state.lock:
                    state.next_reply_id += 1
                    reply_id = int(body.get('message_id', state.next_reply_id))
                chat_id = int(body.get('chat_id', 11))
                payload = {'ok': True, 'result': {'message_id': reply_id, 'date': int(time.time()),
                           'chat': {'id': chat_id, 'type': 'private' if chat_id > 0 else 'supergroup'},
                           'text': body.get('text', '')}}
            else:
                payload = {'ok': True, 'result': True}
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f'http://127.0.0.1:{server.server_port}'
    finally:
        state.release_model.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _wait(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise AssertionError('condition was not reached')


def _private_command(update_id, command):
    text = f'/{command} 今汐'
    return {'update_id': update_id, 'message': {'message_id': update_id,
            'date': int(time.time()), 'chat': {'id': 11, 'type': 'private', 'first_name': 'Fixture'},
            'from': {'id': 11, 'is_bot': False, 'first_name': 'Fixture'}, 'text': text,
            'entities': [{'type': 'bot_command', 'offset': 0, 'length': len(command) + 1}]}}


def _channel_update(update_id, text='今汐', *, edited=False):
    message = {'message_id': 300, 'date': int(time.time()),
               'chat': {'id': -2001, 'type': 'supergroup', 'title': 'Fixture'},
               'sender_chat': {'id': -3001, 'type': 'channel', 'title': 'Fixture'},
               'is_automatic_forward': True, 'text': text}
    if edited:
        message['edit_date'] = int(time.time())
    return {'update_id': update_id, 'edited_message' if edited else 'message': message}


def _start_bot(db, state_dir, base, marker):
    # Only transport configuration is substituted. The builder, SDK, entrypoint,
    # registered handlers, auth, translation, state and shutdown are real.
    program = '''\
import sys
import runpy
import shutil
from pathlib import Path
from telegram.ext import ApplicationBuilder
from wuwaterm.application import SentenceTranslator
from wuwaterm_telegram.channel_reply_index import ChannelReplyIndex
import wuwaterm_telegram.bot as bot
from wuwaterm_telegram.cli import main
base, db, marker = sys.argv[1:]
original_close, original_flush = SentenceTranslator.aclose, ChannelReplyIndex.aflush
async def record_close(self):
    await original_close(self)
    with Path(marker).open('a') as file:
        file.write('aclose\\n')
async def record_flush(self):
    await original_flush(self)
    with Path(marker).open('a') as file:
        file.write('aflush\\n')
SentenceTranslator.aclose = record_close
ChannelReplyIndex.aflush = record_flush
def loopback_builder():
    return ApplicationBuilder().base_url(base + '/bot').base_file_url(base + '/file/bot')
probe = loopback_builder().token('123456:LOCAL_FIXTURE').build()
assert probe.bot.base_url.startswith(base + '/bot')
assert probe.bot.base_file_url.startswith(base + '/file/bot')
bot.ApplicationBuilder = loopback_builder
entry = shutil.which('wuwaterm-telegram')
with Path(marker).open('a') as file:
    file.write(('console' if entry else 'main') + '\\n')
if entry:
    sys.argv = [entry, '--db', db]
    runpy.run_path(entry, run_name='__main__')
else:
    raise SystemExit(main(['--db', db]))
'''
    keep = ('HOME', 'PATH', 'PYTHONPATH', 'PYTHONPYCACHEPREFIX', 'TMPDIR')
    env = {name: os.environ[name] for name in keep if name in os.environ}
    env.update({'TELEGRAM_BOT_TOKEN': '123456:LOCAL_FIXTURE', 'OWNER_USER_ID': '11',
                'WUWATERM_STATE_DIR': str(state_dir), 'WUWATERM_OPENAI_BASE_URL': base + '/v1',
                'WUWATERM_OPENAI_API_KEY': 'local-fixture', 'WUWATERM_OPENAI_MODEL': 'local-fixture'})
    return subprocess.Popen([sys.executable, '-c', program, base, str(db), str(marker)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _stop_bot(process):
    process.send_signal(signal.SIGINT)
    stdout, stderr = process.communicate(timeout=12)
    assert process.returncode == 0, (process.returncode, stdout, stderr)


def test_real_sdk_polling_handlers_and_persistent_channel_restart(sample_db, tmp_path):
    digest_before = hashlib.sha256(sample_db.read_bytes()).hexdigest()
    sample_db.chmod(0o444)
    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    ChatSettings(state_dir / 'chat_settings.json').allow(-2001)
    with loopback_service() as (state, base):
        state.enqueue(*[_private_command(i + 1, name) for i, name in
                        enumerate(('tr', 'term', 'sentence', 'sent'))],
                      _channel_update(5), _channel_update(6))
        marker = state_dir / 'closed-first.txt'
        process = _start_bot(sample_db, state_dir, base, marker)
        try:
            _wait(lambda: len([c for c in state.snapshot()[0] if c[0] == 'sendMessage']) >= 5)
        finally:
            _stop_bot(process)
        assert marker.read_text().splitlines() == [
            'console' if shutil.which('wuwaterm-telegram') else 'main', 'aclose', 'aflush']
        calls, model_requests = state.snapshot()
        assert {'getMe', 'deleteWebhook', 'getUpdates', 'sendMessage'} <= {name for name, _ in calls}
        sent = [body for name, body in calls if name == 'sendMessage']
        assert sum(int(body['chat_id']) == 11 and body['text'] == 'Jinhsi' for body in sent) == 4
        assert sum(int(body['chat_id']) == -2001 and body['text'] == 'Jinhsi' for body in sent) == 1
        assert model_requests == []
        index_path = state_dir / 'channel_replies.json'
        rows, malformed = parse_channel_reply_payload(json.loads(index_path.read_text()))
        assert not malformed and len(rows) == 1
        assert (rows[0].chat_id, rows[0].message_id) == (-2001, 300)
        assert rows[0].reply_message_ids

        # On restart, replay of the original must not send another message;
        # a later edit updates the persisted reply in place.
        before = len(sent)
        state.enqueue(_channel_update(7), _channel_update(8, '声骸', edited=True))
        marker = state_dir / 'closed-restart.txt'
        process = _start_bot(sample_db, state_dir, base, marker)
        try:
            _wait(lambda: any(name == 'editMessageText' for name, _ in state.snapshot()[0]))
        finally:
            _stop_bot(process)
        assert marker.read_text().splitlines() == [
            'console' if shutil.which('wuwaterm-telegram') else 'main', 'aclose', 'aflush']
        calls, model_requests = state.snapshot()
        sent_after = [body for name, body in calls if name == 'sendMessage']
        edits = [body for name, body in calls if name == 'editMessageText']
        assert len(sent_after) == before
        assert len(edits) == 1 and edits[0]['text'] == 'Echo'
        assert int(edits[0]['message_id']) == rows[0].reply_message_ids[0]
        assert model_requests == []
        assert hashlib.sha256(sample_db.read_bytes()).hexdigest() == digest_before


def test_real_model_protects_html_and_content_fallback(monkeypatch, sample_db):
    with loopback_service() as (state, base):
        monkeypatch.setenv('WUWATERM_OPENAI_BASE_URL', base + '/v1')
        monkeypatch.setenv('WUWATERM_OPENAI_API_KEY', 'local-fixture')
        monkeypatch.setenv('WUWATERM_OPENAI_MODEL', 'local-fixture')

        async def run():
            translator = build_translator(sample_db, max_concurrency=1)
            try:
                source = '<b>天地 今汐</b> &amp; <a href="https://example.invalid/?x=1&amp;y=2">你好</a>'
                result = await translate_html_async(translator, source)
                assert result == '<b>world Jinhsi</b> &amp; <a href="https://example.invalid/?x=1&amp;y=2">hello</a>'
                state.model_mode = 'to_zh'
                assert await translate_html_async(translator, '<b>hello</b> &amp; world', to_chinese=True) == '<b>你好</b> &amp; world'
                for mode in ('add_tag', 'add_entity', 'corrupt'):
                    state.model_mode = mode
                    with pytest.raises(LLMTranslationError) as failure:
                        await translate_html_async(translator, '<b>天地</b>')
                    assert (failure.value.reason, failure.value.diagnostic.reason) == ('html_integrity', 'html_integrity')
                state.model_mode = 'empty_once'
                before = len(state.snapshot()[1])
                result = await _translate_channel_input(translator, '天地', '<b>天地</b>',
                                                        to_chinese=False, before_llm_call=lambda: None)
                assert result == ('world', None, 'plain-after-html-failure')
                assert len(state.snapshot()[1]) - before == 2
                state.model_mode = 'empty_once'
                before = len(state.snapshot()[1])
                with pytest.raises(LLMTranslationError) as failure:
                    await _translate_channel_input(translator, '天地', '<b>天地</b>', to_chinese=False,
                                                   before_llm_call=lambda: None, allow_plain_fallback=False)
                assert failure.value.reason == 'invalid_response'
                assert len(state.snapshot()[1]) - before == 1
            finally:
                await translator.aclose()
        asyncio.run(run())


@pytest.mark.parametrize('change', ['revoked', 'stale'])
def test_real_channel_guard_rechecks_after_shared_slot(monkeypatch, sample_db, change):
    with loopback_service() as (state, base):
        monkeypatch.setenv('WUWATERM_OPENAI_BASE_URL', base + '/v1')
        monkeypatch.setenv('WUWATERM_OPENAI_API_KEY', 'local-fixture')
        monkeypatch.setenv('WUWATERM_OPENAI_MODEL', 'local-fixture')
        state.model_mode = 'hold'

        async def run():
            config = BotConfig(owner_user_id=11, llm_max_concurrency=1, channel_max_age_seconds=1 if change == 'stale' else 60)
            context = make_context(sample_db, config=config)
            translator = context.application.bot_data[TRANSLATOR_KEY]
            first = asyncio.create_task(translator.translate_async('命令翻译占用共享并发槽', propagate_errors=True))
            try:
                assert await asyncio.to_thread(state.model_entered.wait, 4)
                update, message = channel_update(text='这是一段需要翻译的频道文本', message_id=4722,
                                                  date=datetime.now(timezone.utc))
                second = asyncio.create_task(channel_post_handler(update, context))
                await asyncio.sleep(0.1)
                assert not second.done()
                if change == 'revoked':
                    context.application.bot_data[CHAT_SETTINGS_KEY].disallow(-2001)
                else:
                    await asyncio.sleep(1.2)
                state.release_model.set()
                await asyncio.gather(first, second)
                assert len(state.snapshot()[1]) == 1
                assert message.replies == []
                outcome = 'authorization_changed_before_llm' if change == 'revoked' else 'stale_before_llm'
                snapshot = context.application.bot_data[CHANNEL_RUNTIME_KEY].snapshot()
                assert snapshot.outcomes[f'skipped:{outcome}'] == 1
            finally:
                state.release_model.set()
                await translator.aclose()
        asyncio.run(run())


def test_real_channel_budget_and_multichunk_admission(monkeypatch, sample_db):
    with loopback_service() as (state, base):
        monkeypatch.setenv('WUWATERM_OPENAI_BASE_URL', base + '/v1')
        monkeypatch.setenv('WUWATERM_OPENAI_API_KEY', 'local-fixture')
        monkeypatch.setenv('WUWATERM_OPENAI_MODEL', 'local-fixture')

        async def run():
            one = make_context(sample_db, config=BotConfig(owner_user_id=11, channel_llm_calls_per_minute=1))
            state.model_mode = 'empty_once'
            update, message = channel_update(text='天地', text_html='<b>天地</b>', message_id=8001)
            await channel_post_handler(update, one)
            assert len(state.snapshot()[1]) == 1
            assert message.replies == []  # no plain retry without a reserved token
            await one.application.bot_data[TRANSLATOR_KEY].aclose()

            long_text = '中' * 4001  # three <=2000-character chunks
            two = make_context(sample_db, config=BotConfig(owner_user_id=11, channel_llm_calls_per_minute=2))
            update, message = channel_update(text=long_text, message_id=8002)
            before = len(state.snapshot()[1])
            await channel_post_handler(update, two)
            assert len(state.snapshot()[1]) == before
            assert message.replies == []
            assert two.application.bot_data[CHANNEL_RUNTIME_KEY].snapshot().outcomes['skipped:llm_budget'] == 1
            await two.application.bot_data[TRANSLATOR_KEY].aclose()

            three = make_context(sample_db, config=BotConfig(owner_user_id=11, channel_llm_calls_per_minute=3))
            update, message = channel_update(text=long_text, message_id=8003)
            before = len(state.snapshot()[1])
            await channel_post_handler(update, three)
            assert len(state.snapshot()[1]) - before == 3
            assert len(message.replies) == 1
            await three.application.bot_data[TRANSLATOR_KEY].aclose()
        asyncio.run(run())


def test_real_channel_queue_full_does_not_call_model(monkeypatch, sample_db):
    with loopback_service() as (state, base):
        monkeypatch.setenv('WUWATERM_OPENAI_BASE_URL', base + '/v1')
        monkeypatch.setenv('WUWATERM_OPENAI_API_KEY', 'local-fixture')
        monkeypatch.setenv('WUWATERM_OPENAI_MODEL', 'local-fixture')
        state.model_mode = 'hold'

        async def run():
            config = BotConfig(owner_user_id=11, llm_max_concurrency=1, channel_max_pending=0)
            context = make_context(sample_db, config=config)
            first_update, first_message = channel_update(text='第一段需要翻译的频道文本', message_id=8101)
            next_update, next_message = channel_update(text='第二段需要翻译的频道文本', message_id=8102)
            first = asyncio.create_task(channel_post_handler(first_update, context))
            try:
                assert await asyncio.to_thread(state.model_entered.wait, 4)
                await channel_post_handler(next_update, context)
                assert len(state.snapshot()[1]) == 1
                assert next_message.replies == []
                assert context.application.bot_data[CHANNEL_RUNTIME_KEY].snapshot().outcomes['skipped:queue_full'] == 1
                state.release_model.set()
                await first
                assert first_message.replies
            finally:
                state.release_model.set()
                await context.application.bot_data[TRANSLATOR_KEY].aclose()
        asyncio.run(run())
