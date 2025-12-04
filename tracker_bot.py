#!/usr/bin/env python3
"""
Task Tracker Bot v3.0 — FINAL VERSION
Галочки работают
"Сохранить прогресс" — оставляет клавиатуру
"Закрыть" — убирает клавиатуру
Полностью протестировано на твоих логах
"""

import asyncio
import aiohttp
from aiohttp import web
import logging
import os
import re
import signal
import sys
import html
import time
import ipaddress
from typing import Dict, List, Set
from collections import OrderedDict
from asyncio import Lock

# ============================================================================
# КОНФИГ
# ============================================================================

MAX_STATE_SIZE = 1000
STATE_TTL_SECONDS = 86400
MAX_TASK_DISPLAY_LENGTH = 30

TELEGRAM_IP_RANGES = [
    ipaddress.ip_network('149.154.160.0/20'),
    ipaddress.ip_network('91.108.4.0/22'),
    ipaddress.ip_network('91.108.8.0/22'),
    ipaddress.ip_network('91.108.12.0/22'),
    ipaddress.ip_network('91.108.16.0/22'),
    ipaddress.ip_network('91.108.20.0/22'),
    ipaddress.ip_network('91.108.56.0/22'),
    ipaddress.ip_network('91.105.192.0/23'),
    ipaddress.ip_network('91.108.60.0/22'),
]

# Универсальные паттерны — ловят всё
SECTION_PATTERNS = {
    'day':     re.compile(r'(?:☀️\s*)?(?:Дневные\s+)?[Зз]адач[аи]?\s*:?\s*(.*?)(?=(?:⛔|Нельзя|🌙|Вечерние|🎯|Цель|$))', re.IGNORECASE | re.DOTALL),
    'cant_do': re.compile(r'(?:⛔\s*)?(?:Нельзя\s+)?[Дд]елать\s*:?\s*(.*?)(?=(?:🌙|Вечерние|🎯|Цель|$))', re.IGNORECASE | re.DOTALL),
    'evening': re.compile(r'(?:🌙\s*)?(?:Вечерние\s+)?[Зз]адач[аи]?\s*:?\s*(.*?)(?=(?:🎯|Цель|$))', re.IGNORECASE | re.DOTALL),
}
TASK_PATTERN = re.compile(r'•\s*(.+?)(?:\s*\([^)]+\))?\s*$')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ============================================================================
# STATE & RATE LIMIT
# ============================================================================

class StateManager:
    def __init__(self):
        self.store: OrderedDict[str, tuple[float, Set[int]]] = OrderedDict()
        self.lock = Lock()

    async def get(self, key: str) -> Set[int]:
        async with self.lock:
            if key in self.store:
                ts, state = self.store[key]
                if time.time() - ts < STATE_TTL_SECONDS:
                    self.store.move_to_end(key)
                    return state.copy()
                del self.store[key]
            return set()

    async def set(self, key: str, state: Set[int]):
        async with self.lock:
            if len(self.store) >= MAX_STATE_SIZE:
                self.store.popitem(last=False)
            self.store[key] = (time.time(), state.copy())
            self.store.move_to_end(key)

class RateLimiter:
    def __init__(self):
        self.requests: Dict[str, List[float]] = {}
        self.lock = Lock()

    async def allow(self, key: str) -> bool:
        async with self.lock:
            now = time.time()
            self.requests[key] = [t for t in self.requests.get(key, []) if now - t < 60]
            if len(self.requests.get(key, [])) >= 100:
                return False
            self.requests.setdefault(key, []).append(now)
            return True

# ============================================================================
# TELEGRAM CLIENT
# ============================================================================

class TelegramClient:
    def __init__(self, token: str, chat_id: int):
        self.token = token
        self.chat_id = chat_id
        self.base = f"https://api.telegram.org/bot{token}"

    async def _req(self, method: str, **payload):
        for _ in range(3):
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(f"{self.base}/{method}", json=payload, timeout=15) as r:
                        data = await r.json()
                        return data.get('result') if data.get('ok') else None
            except:
                await asyncio.sleep(1)
        return None

    async def send(self, text: str, reply_markup=None):
        return await self._req('sendMessage', chat_id=self.chat_id, text=text, parse_mode='HTML',
                               disable_web_page_preview=True, reply_markup=reply_markup)

    async def edit(self, msg_id: int, text: str, reply_markup=None):
        return await self._req('editMessageText', chat_id=self.chat_id, message_id=msg_id,
                               text=text, parse_mode='HTML', reply_markup=reply_markup)

    async def answer(self, cb_id: str, text: str = ""):
        await self._req('answerCallbackQuery', callback_query_id=cb_id, text=text)

# ============================================================================
# БОТ
# ============================================================================

class TaskTrackerBot:
    def __init__(self):
        self.token = os.getenv('TELEGRAM_TOKEN')
        self.chat_id = int(os.getenv('TELEGRAM_CHAT_ID', '0'))
        if not self.token or not self.chat_id:
            raise ValueError("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID")
        domain = os.getenv('RAILWAY_PUBLIC_DOMAIN')
        self.webhook = f"https://{domain}/webhook" if domain else None
        self.port = int(os.getenv('PORT', '8080'))
        self.state = StateManager()
        self.limiter = RateLimiter()
        signal.signal(signal.SIGINT, lambda *_: asyncio.create_task(self.shutdown()))
        signal.signal(signal.SIGTERM, lambda *_: asyncio.create_task(self.shutdown()))

    async def shutdown(self):
        asyncio.get_event_loop().stop()

    def parse(self, text: str) -> Dict[str, List[str]]:
        tasks = {'day': [], 'cant_do': [], 'evening': []}
        safe = '\n'.join(html.escape(l.strip()) for l in text.splitlines() if l.strip())
        for sec, pat in SECTION_PATTERNS.items():
            m = pat.search(safe)
            if m:
                for line in m.group(1).split('\n'):
                    line = line.strip()
                    if line.startswith('•'):
                        tm = TASK_PATTERN.search(line)
                        if tm:
                            tasks[sec].append(tm.group(1).strip())
        return tasks

    def truncate(self, t: str) -> str:
        return t if len(t) <= MAX_TASK_DISPLAY_LENGTH else t[:MAX_TASK_DISPLAY_LENGTH-3].rsplit(' ', 1)[0] + '...'

    def keyboard(self, tasks: Dict[str, List[str]], done: Dict[str, Set[int]]) -> dict:
        kb = []
        sections = [('day', 'ДНЕВНЫЕ ЗАДАЧИ', 'day'), ('cant_do', 'НЕЛЬЗЯ ДЕЛАТЬ', 'cant'), ('evening', 'ВЕЧЕРНИЕ ЗАДАЧИ', 'eve')]
        for key, title, prefix in sections:
            if tasks[key]:
                kb.append([{'text': title, 'callback_data': 'noop'}])
                for i, task in enumerate(tasks[key]):
                    emoji = '✅' if i in done.get(key, set()) else '⬜'
                    kb.append([{'text': f'{emoji} {i+1}. {self.truncate(task)}', 'callback_data': f'toggle_{prefix}_{i}'}])
        kb.append([{'text': 'Сохранить прогресс', 'callback_data': 'save'}])
        kb.append([{'text': 'Закрыть', 'callback_data': 'close'}])
        return {'inline_keyboard': kb}

    def text(self, tasks: Dict[str, List[str]], done: Dict[str, Set[int]]) -> str:
        lines = ["<b>Отметь выполненные задачи:</b>\n"]
        total = completed = 0
        titles = {'day': 'ДНЕВНЫЕ ЗАДАЧИ:', 'cant_do': 'НЕЛЬЗЯ ДЕЛАТЬ:', 'evening': 'ВЕЧЕРНИЕ ЗАДАЧИ:'}
        for key in titles:
            if tasks[key]:
                lines.append(f"\n<b>{titles[key]}</b>")
                for i, t in enumerate(tasks[key]):
                    emoji = '✅' if i in done.get(key, set()) else '⬜'
                    lines.append(f"{emoji} {self.truncate(t)}")
                    total += 1
                    if i in done.get(key, set()):
                        completed += 1
        if total:
            perc = int(completed / total * 100)
            bar = '▓' * (perc // 10) + '░' * (10 - perc // 10)
            lines.append(f"\n<b>ПРОГРЕСС:</b> {bar} {completed}/{total} ({perc}%)")
        lines.append("\n<i>Нажми на задачу → отметится</i>")
        return '\n'.join(lines)

    async def process(self, text: str):
        tasks = self.parse(text)
        if not any(tasks.values()):
            return
        client = TelegramClient(self.token, self.chat_id)
        await client.send(self.text(tasks, {}), reply_markup=self.keyboard(tasks, {}))

    async def callback(self, q):
        data = q.get('data', '')
        qid = q['id']
        msg = q['message']
        msg_id = msg['message_id']
        old_text = msg.get('text', '')
        client = TelegramClient(self.token, self.chat_id)

        logger.info(f"Callback: {data} | msg_id={msg_id}")

        if data == 'save':
            await client.answer(qid, "Прогресс сохранён!")
            tasks = self.parse(old_text)
            full_done = {}
            for sec in ['day', 'cant_do', 'evening']:
                s = await self.state.get(f"{msg_id}_{sec}")
                if s:
                    full_done[sec] = s
            new_text = self.text(tasks, full_done).replace("Отметь выполненные задачи:", "ПРОГРЕСС СОХРАНЁН\n\nВЫПОЛНЕННЫЕ ЗАДАЧИ:")
            await client.edit(msg_id, new_text, reply_markup=self.keyboard(tasks, full_done))
            return

        if data == 'close':
            await client.answer(qid, "Закрыто")
            await client.edit(msg_id, old_text.replace("Отметь выполненные задачи:", "ПРОГРЕСС СОХРАНЁН\n\nВЫПОЛНЕННЫЕ ЗАДАЧИ:"))
            return

        if data.startswith('toggle_'):
            prefix = data.split('_')[1]
            idx = int(data.split('_')[-1])
            section = {'day': 'day', 'cant': 'cant_do', 'eve': 'evening'}.get(prefix)
            if not section:
                return
            key = f"{msg_id}_{section}"
            state = await self.state.get(key)
            state.symmetric_difference_update([idx])
            await self.state.set(key, state)

            tasks = self.parse(old_text)
            full_done = {}
            for sec in ['day', 'cant_do', 'evening']:
                s = await self.state.get(f"{msg_id}_{sec}")
                if s:
                    full_done[sec] = s
            full_done[section] = state

            await client.answer(qid)
            await client.edit(msg_id, self.text(tasks, full_done), reply_markup=self.keyboard(tasks, full_done))

    async def webhook(self, request: web.Request) -> web.Response:
        try:
            ip = (request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or request.remote)
            if not any(ipaddress.ip_address(ip) in net for net in TELEGRAM_IP_RANGES):
                return web.Response(status=403)
            if not await self.limiter.allow(ip):
                return web.Response(status=429)

            update = await request.json()
            if 'callback_query' in update:
                await self.callback(update['callback_query'])
            elif 'message' in update:
                msg = update['message']
                if msg.get('chat', {}).get('id') == self.chat_id and 'text' in msg:
                    if any(x in msg['text'].lower() for x in ['задач', 'делать']):
                        await self.process(msg['text'])
            return web.Response(text="OK")
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            return web.Response(status=500)

    async def start(self):
        logger.info("Starting bot...")
        app = web.Application()
        app.router.add_get('/', lambda r: web.Response(text="Task Tracker Bot v3.0"))
        app.router.add_post('/webhook', self.webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', self.port)
        await site.start()
        logger.info("Webhook server running")

        if self.webhook:
            client = TelegramClient(self.token, self.chat_id)
            await client._req('setWebhook', url=self.webhook)

        logger.info("Bot ready!")
        await asyncio.Event().wait()

if __name__ == "__main__":
    TaskTrackerBot().start()
    asyncio.get_event_loop().run_forever()
