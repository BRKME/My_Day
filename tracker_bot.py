#!/usr/bin/env python3
"""
Telegram Task Tracker Bot - PRODUCTION VERSION
Версия 3.0.0

Автор: Task Tracker Team
Лицензия: MIT
"""

import asyncio
import aiohttp
from aiohttp import web
import json
import logging
from datetime import datetime, timedelta
import os
import re
import signal
import sys
import html
import time
import hashlib
import ipaddress
from typing import Dict, List, Optional, Any, Tuple, Set
from collections import OrderedDict
from asyncio import Lock

# ============================================================================
# КОНСТАНТЫ И НАСТРОЙКИ
# ============================================================================

# Размеры и лимиты
MAX_STATE_SIZE = 1000               # Максимальное количество состояний в памяти
STATE_TTL_SECONDS = 86400           # 24 часа TTL для состояний
MAX_TASK_DISPLAY_LENGTH = 30        # Максимальная длина задачи для отображения
PROGRESS_BAR_LENGTH = 10            # Длина прогресс-бара в символах
MAX_CALLBACK_DATA_BYTES = 64        # Лимит Telegram для callback_data
MAX_MESSAGE_LENGTH = 4000           # Максимальная длина сообщения Telegram
TELEGRAM_API_TIMEOUT = 30           # Таймаут запросов к Telegram API
MAX_RETRIES = 3                     # Максимальное количество повторов запросов
RETRY_BASE_DELAY = 1.0              # Базовая задержка между повторами
RATE_LIMIT_REQUESTS = 100           # Максимальное количество запросов в окне
RATE_LIMIT_WINDOW = 60              # Окно rate limiting в секундах

# Telegram IP диапазоны (обновлено 2024)
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

# Регулярные выражения для парсинга
TASK_PATTERN = re.compile(r'•\s*(.+?)(?:\s*\([^)]+\))?\s*$')
SECTION_PATTERNS = {
    'day': re.compile(r'(?:☀️\s*)?(?:Дневные\s+)?задачи?:?\s*(.*?)(?=(?:⛔|🌙|🎯|$))', re.IGNORECASE | re.DOTALL),
    'cant_do': re.compile(r'(?:⛔\s*)?(?:Нельзя\s+)?делать:?\s*(.*?)(?=(?:🌙|🎯|$))', re.IGNORECASE | re.DOTALL),
    'evening': re.compile(r'(?:🌙\s*)?(?:Вечерние\s+)?задачи?:?\s*(.*?)(?=(?:🎯|$))', re.IGNORECASE | re.DOTALL),
}

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


# ============================================================================
# ВСПОМОГАТЕЛЬНЫЕ КЛАССЫ
# ============================================================================

class RateLimiter:
    """Rate limiter для защиты от спама"""
    
    def __init__(self, max_requests: int = RATE_LIMIT_REQUESTS, window: int = RATE_LIMIT_WINDOW):
        self.max_requests = max_requests
        self.window = window
        self.requests: Dict[str, List[float]] = {}
        self.lock = Lock()
    
    async def is_allowed(self, key: str) -> bool:
        """Проверяет, разрешен ли запрос"""
        async with self.lock:
            now = time.time()
            
            if key in self.requests:
                # Удаляем записи старше окна
                self.requests[key] = [
                    timestamp for timestamp in self.requests[key]
                    if now - timestamp < self.window
                ]
            else:
                self.requests[key] = []
            
            # Проверяем лимит
            if len(self.requests[key]) >= self.max_requests:
                return False
            
            self.requests[key].append(now)
            return True


class StateManager:
    """Управление состоянием с TTL и LRU"""
    
    def __init__(self, max_size: int = MAX_STATE_SIZE, ttl: int = STATE_TTL_SECONDS):
        self.max_size = max_size
        self.ttl = ttl
        self.state_store: OrderedDict[str, Tuple[float, Set[int]]] = OrderedDict()
        self.lock = Lock()
    
    async def get(self, key: str) -> Optional[Set[int]]:
        """Получает состояние по ключу"""
        async with self.lock:
            self._cleanup()
            
            if key in self.state_store:
                timestamp, state = self.state_store[key]
                if time.time() - timestamp < self.ttl:
                    # Обновляем порядок (LRU)
                    self.state_store.move_to_end(key)
                    return state.copy()
            
            return None
    
    async def set(self, key: str, state: Set[int]) -> None:
        """Устанавливает состояние"""
        async with self.lock:
            self._cleanup()
            
            # Удаляем самую старую запись если достигли лимита
            if len(self.state_store) >= self.max_size:
                oldest_key = next(iter(self.state_store))
                del self.state_store[oldest_key]
            
            # Сохраняем состояние с timestamp
            self.state_store[key] = (time.time(), state.copy())
            self.state_store.move_to_end(key)
    
    def _cleanup(self):
        """Очищает устаревшие записи"""
        current_time = time.time()
        expired_keys = [
            key for key, (timestamp, _) in self.state_store.items()
            if current_time - timestamp > self.ttl
        ]
        
        for key in expired_keys:
            del self.state_store[key]


class TelegramAPIClient:
    """Клиент для работы с Telegram API"""
    
    def __init__(self, token: str, chat_id: str):
        if not token or not chat_id:
            raise ValueError("Token and chat_id are required")
        
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        """Контекстный менеджер"""
        timeout = aiohttp.ClientTimeout(total=TELEGRAM_API_TIMEOUT)
        self.session = aiohttp.ClientSession(timeout=timeout)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Контекстный менеджер"""
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def _make_request(self, method: str, endpoint: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Выполняет запрос с повторными попытками"""
        for attempt in range(MAX_RETRIES):
            try:
                url = f"{self.base_url}/{endpoint}"
                
                async with self.session.request(method, url, **kwargs) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        if data.get('ok'):
                            return data.get('result')
                        else:
                            logger.error(f"Telegram API error: {data.get('description', 'Unknown error')}")
                    else:
                        error_text = await response.text()
                        logger.error(f"HTTP error {response.status}: {error_text}")
                    
                    if attempt == MAX_RETRIES - 1:
                        return None
                    
                    wait_time = RETRY_BASE_DELAY * (2 ** attempt)
                    await asyncio.sleep(wait_time)
                    
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"Network error (attempt {attempt + 1}): {e}")
                if attempt == MAX_RETRIES - 1:
                    return None
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                return None
        
        return None
    
    async def send_message(self, text: str, **kwargs) -> Optional[Dict[str, Any]]:
        """Отправляет сообщение"""
        if len(text) > MAX_MESSAGE_LENGTH:
            logger.warning(f"Message too long ({len(text)} chars), truncating")
            text = text[:MAX_MESSAGE_LENGTH - 100] + "\n...[сообщение обрезано]"
        
        payload = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
            **kwargs
        }
        
        return await self._make_request('POST', 'sendMessage', json=payload)
    
    async def edit_message(self, message_id: int, text: str, **kwargs) -> bool:
        """Редактирует сообщение"""
        if len(text) > MAX_MESSAGE_LENGTH:
            logger.warning(f"Edited message too long ({len(text)} chars), truncating")
            text = text[:MAX_MESSAGE_LENGTH - 100] + "\n...[сообщение обрезано]"
        
        payload = {
            'chat_id': self.chat_id,
            'message_id': message_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
            **kwargs
        }
        
        result = await self._make_request('POST', 'editMessageText', json=payload)
        return result is not None
    
    async def answer_callback_query(self, callback_query_id: str, **kwargs) -> bool:
        """Отвечает на callback query"""
        payload = {
            'callback_query_id': callback_query_id,
            **kwargs
        }
        
        result = await self._make_request('POST', 'answerCallbackQuery', json=payload)
        return result is not None
    
    async def set_webhook(self, url: str) -> bool:
        """Устанавливает webhook URL"""
        payload = {
            'url': url,
            'drop_pending_updates': True,
            'max_connections': 40,
        }
        
        result = await self._make_request('POST', 'setWebhook', json=payload)
        return result is not None


class MessageParser:
    """Парсер сообщений с задачами"""
    
    @staticmethod
    def sanitize_text(text: str) -> str:
        """Очищает и экранирует текст"""
        text = ' '.join(text.split())
        return html.escape(text)
    
    @staticmethod
    def parse_tasks(message_text: str) -> Dict[str, List[str]]:
        """Парсит задачи из сообщения"""
        tasks = {
            'day': [],
            'cant_do': [],
            'evening': []
        }
        
        safe_text = MessageParser.sanitize_text(message_text)
        
        for section, pattern in SECTION_PATTERNS.items():
            match = pattern.search(safe_text)
            if match:
                section_text = match.group(1).strip()
                if section_text:
                    for line in section_text.split('\n'):
                        line = line.strip()
                        if line.startswith('•'):
                            task_match = TASK_PATTERN.search(line)
                            if task_match:
                                task_text = task_match.group(1).strip()
                                if task_text:
                                    tasks[section].append(task_text)
        
        logger.info(f"Parsed tasks - Day: {len(tasks['day'])}, Can't do: {len(tasks['cant_do'])}, Evening: {len(tasks['evening'])}")
        return tasks
    
    @staticmethod
    def truncate_task(task: str, max_length: int = MAX_TASK_DISPLAY_LENGTH) -> str:
        """Обрезает задачу для отображения"""
        if len(task) <= max_length:
            return task
        
        truncated = task[:max_length - 3]
        last_space = truncated.rfind(' ')
        
        if last_space > max_length - 10:
            truncated = truncated[:last_space]
        
        return truncated + '...'


# ============================================================================
# ОСНОВНОЙ КЛАСС БОТА
# ============================================================================

class TaskTrackerBot:
    """Основной класс Telegram бота для отслеживания задач"""
    
    def __init__(self):
        logger.info("=" * 60)
        logger.info("Initializing Task Tracker Bot v3.0.0")
        logger.info("=" * 60)
        
        # Загрузка конфигурации
        self._load_configuration()
        
        # Инициализация компонентов
        self.state_manager = StateManager()
        self.message_parser = MessageParser()
        self.rate_limiter = RateLimiter()
        
        # Статус и управление
        self.start_time = time.time()
        self.shutdown_event = asyncio.Event()
        
        # Настройка обработчиков сигналов
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        
        logger.info("✅ Bot initialized successfully")
    
    def _load_configuration(self):
        """Загружает и валидирует конфигурацию"""
        self.telegram_token = os.getenv('TELEGRAM_TOKEN')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID')
        
        if not self.telegram_token:
            raise ValueError("TELEGRAM_TOKEN environment variable is required")
        
        if not self.chat_id:
            raise ValueError("TELEGRAM_CHAT_ID environment variable is required")
        
        # Валидация токена
        token_pattern = r'^\d{9,10}:[A-Za-z0-9_-]{35}$'
        if not re.match(token_pattern, self.telegram_token):
            raise ValueError("Invalid Telegram token format")
        
        # Валидация chat_id
        try:
            self.chat_id_int = int(self.chat_id)
            if self.chat_id_int == 0:
                raise ValueError("Chat ID cannot be zero")
        except ValueError:
            raise ValueError("Chat ID must be a valid integer")
        
        # Опциональные переменные
        self.port = int(os.getenv('PORT', '8080'))
        self.webhook_url = os.getenv('RAILWAY_PUBLIC_DOMAIN')
        if self.webhook_url:
            self.webhook_url = f"https://{self.webhook_url}/webhook"
        
        # Логирование конфигурации
        logger.info(f"Configuration loaded:")
        logger.info(f"  • Port: {self.port}")
        logger.info(f"  • Chat ID: {self.chat_id_int}")
        logger.info(f"  • Webhook URL: {self.webhook_url or 'Not set'}")
    
    @staticmethod
    def _validate_ip_address(ip_str: str) -> bool:
        """Проверяет, принадлежит ли IP адрес диапазонам Telegram"""
        try:
            ip = ipaddress.ip_address(ip_str)
            return any(ip in network for network in TELEGRAM_IP_RANGES)
        except ValueError:
            return False
    
    @staticmethod
    def create_progress_bar(percentage: int, length: int = PROGRESS_BAR_LENGTH) -> str:
        """Создает текстовый прогресс-бар"""
        percentage = max(0, min(100, percentage))
        filled = int((percentage / 100) * length)
        return '▓' * filled + '░' * (length - filled)
    
    def _create_callback_data(self, action: str, section: str, idx: int) -> str:
        """Создает callback_data с проверкой длины"""
        callback_data = f"{action}_{section}_{idx}"
        
        # Если данные слишком длинные, используем хэш
        if len(callback_data.encode('utf-8')) > MAX_CALLBACK_DATA_BYTES:
            hash_part = hashlib.md5(callback_data.encode()).hexdigest()[:8]
            callback_data = f"t_{hash_part}_{idx}"
        
        return callback_data
    
    def create_checklist_keyboard(self, tasks: Dict[str, List[str]], 
                                 completed: Dict[str, Set[int]]) -> Dict[str, Any]:
        """Создает инлайн-клавиатуру с чеклистом"""
        keyboard = []
        
        sections = [
            ('day', '☀️ ДНЕВНЫЕ ЗАДАЧИ', 'day'),
            ('cant_do', '⛔ НЕЛЬЗЯ ДЕЛАТЬ', 'cant'),
            ('evening', '🌙 ВЕЧЕРНИЕ ЗАДАЧИ', 'eve'),
        ]
        
        for section_key, header_text, callback_prefix in sections:
            if tasks[section_key]:
                keyboard.append([{
                    'text': header_text,
                    'callback_data': f'header_{callback_prefix}'
                }])
                
                for idx, task in enumerate(tasks[section_key]):
                    emoji = '✅' if idx in completed.get(section_key, set()) else '⬜'
                    display_task = self.message_parser.truncate_task(task)
                    
                    callback_data = self._create_callback_data('toggle', callback_prefix, idx)
                    
                    keyboard.append([{
                        'text': f'{emoji} {idx + 1}. {display_task}',
                        'callback_data': callback_data
                    }])
        
        keyboard.append([
            {'text': '💾 Сохранить прогресс', 'callback_data': 'save_progress'},
            {'text': '❌ Отменить', 'callback_data': 'cancel_update'}
        ])
        
        return {'inline_keyboard': keyboard}
    
    def format_checklist_message(self, tasks: Dict[str, List[str]], 
                                completed: Dict[str, Set[int]]) -> str:
        """Форматирует сообщение с чеклистом и прогресс-баром"""
        message_lines = ["<b>📋 Отметь выполненные задачи:</b>\n"]
        
        total_tasks = 0
        completed_tasks = 0
        
        section_titles = {
            'day': '☀️ ДНЕВНЫЕ ЗАДАЧИ:',
            'cant_do': '⛔ НЕЛЬЗЯ ДЕЛАТЬ:',
            'evening': '🌙 ВЕЧЕРНИЕ ЗАДАЧИ:',
        }
        
        for section_key, section_title in section_titles.items():
            if tasks[section_key]:
                message_lines.append(f"\n<b>{section_title}</b>")
                
                for idx, task in enumerate(tasks[section_key]):
                    emoji = '✅' if idx in completed.get(section_key, set()) else '⬜'
                    message_lines.append(f"{emoji} {task}")
                    
                    total_tasks += 1
                    if idx in completed.get(section_key, set()):
                        completed_tasks += 1
        
        if total_tasks > 0:
            percentage = int((completed_tasks / total_tasks) * 100)
            progress_bar = self.create_progress_bar(percentage)
            
            message_lines.append(f"\n<b>📊 ПРОГРЕСС:</b>")
            message_lines.append(f"{progress_bar} {completed_tasks}/{total_tasks} ({percentage}%)")
        
        message_lines.append("\n<i>Нажмите на задачу, чтобы отметить её выполненной</i>")
        
        return '\n'.join(message_lines)
    
    async def process_callback_query(self, callback_data: str, callback_query_id: str,
                                    message_id: int, message_text: str) -> bool:
        """Обрабатывает callback query от пользователя"""
        try:
            # Rate limiting
            rate_key = f"callback_{message_id}"
            if not await self.rate_limiter.is_allowed(rate_key):
                logger.warning(f"Rate limit exceeded for message {message_id}")
                async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
                    await client.answer_callback_query(
                        callback_query_id,
                        text="Слишком много запросов. Попробуйте через минуту.",
                        show_alert=True
                    )
                return False
            
            if callback_data == 'save_progress':
                await self._handle_save_progress(callback_query_id, message_id, message_text)
                
            elif callback_data == 'cancel_update':
                await self._handle_cancel_update(callback_query_id, message_id)
                
            elif callback_data.startswith('toggle_'):
                await self._handle_toggle_task(callback_data, callback_query_id, message_id, message_text)
                
            elif callback_data.startswith('header_'):
                async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
                    await client.answer_callback_query(callback_query_id)
                
            else:
                logger.warning(f"Unknown callback data: {callback_data}")
                async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
                    await client.answer_callback_query(
                        callback_query_id,
                        text="Неизвестная команда",
                        show_alert=True
                    )
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing callback: {e}", exc_info=True)
            
            try:
                async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
                    await client.answer_callback_query(
                        callback_query_id,
                        text="Произошла ошибка при обработке запроса",
                        show_alert=True
                    )
            except:
                pass
            
            return False
    
    async def _handle_save_progress(self, callback_query_id: str, message_id: int, message_text: str):
        """Обрабатывает сохранение прогресса"""
        async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
            await client.answer_callback_query(
                callback_query_id,
                text="✅ Прогресс сохранён!",
                show_alert=False
            )
            
            updated_text = message_text.replace(
                "<b>📋 Отметь выполненные задачи:</b>",
                "<b>✅ ПРОГРЕСС СОХРАНЁН</b>\n\n<b>📋 ВЫПОЛНЕННЫЕ ЗАДАЧИ:</b>"
            )
            
            await client.edit_message(
                message_id,
                updated_text,
                reply_markup=None
            )
            
            logger.info(f"Progress saved for message {message_id}")
    
    async def _handle_cancel_update(self, callback_query_id: str, message_id: int):
        """Обрабатывает отмену обновления"""
        async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
            await client.answer_callback_query(
                callback_query_id,
                text="❌ Обновление отменено",
                show_alert=False
            )
            
            await client.edit_message(
                message_id,
                "❌ ОБНОВЛЕНИЕ ОТМЕНЕНО",
                reply_markup=None
            )
    
    async def _handle_toggle_task(self, callback_data: str, callback_query_id: str,
                                 message_id: int, message_text: str):
        """Обрабатывает переключение состояния задачи"""
        try:
            parts = callback_data.split('_')
            if len(parts) != 3:
                raise ValueError(f"Invalid callback data format: {callback_data}")
            
            _, section_code, idx_str = parts
            task_idx = int(idx_str)
            
            section_map = {'day': 'day', 'cant': 'cant_do', 'eve': 'evening'}
            section = section_map.get(section_code)
            
            if not section:
                raise ValueError(f"Unknown section code: {section_code}")
            
            # Получаем текущее состояние
            state_key = f"{message_id}_{section}"
            current_state = await self.state_manager.get(state_key)
            
            if current_state is None:
                current_state = set()
            
            # Обновляем состояние
            new_state = current_state.copy()
            if task_idx in new_state:
                new_state.remove(task_idx)
            else:
                new_state.add(task_idx)
            
            # Сохраняем новое состояние
            await self.state_manager.set(state_key, new_state)
            
            # Парсим задачи
            tasks = self.message_parser.parse_tasks(message_text)
            
            # Создаем обновленное сообщение
            completed_tasks = {section: new_state}
            updated_text = self.format_checklist_message(tasks, completed_tasks)
            updated_keyboard = self.create_checklist_keyboard(tasks, completed_tasks)
            
            # Редактируем сообщение
            async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
                await client.answer_callback_query(callback_query_id)
                await client.edit_message(
                    message_id,
                    updated_text,
                    reply_markup=updated_keyboard
                )
            
        except (ValueError, IndexError) as e:
            logger.error(f"Error parsing callback data: {e}")
            async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
                await client.answer_callback_query(
                    callback_query_id,
                    text="Ошибка обработки. Попробуйте ещё раз.",
                    show_alert=True
                )
    
    async def process_schedule_message(self, message_text: str) -> bool:
        """Обрабатывает входящее сообщение с расписанием"""
        try:
            # Парсим задачи из сообщения
            tasks = self.message_parser.parse_tasks(message_text)
            
            # Проверяем, есть ли задачи
            if not any(tasks.values()):
                logger.info("No tasks found in message")
                return False
            
            # Создаем чеклист
            checklist_text = self.format_checklist_message(tasks, {})
            checklist_keyboard = self.create_checklist_keyboard(tasks, {})
            
            # Отправляем чеклист
            async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
                result = await client.send_message(
                    checklist_text,
                    reply_markup=checklist_keyboard
                )
            
            if result:
                logger.info(f"Checklist sent successfully (message_id: {result.get('message_id')})")
                return True
            else:
                logger.error("Failed to send checklist")
                return False
            
        except Exception as e:
            logger.error(f"Error processing schedule message: {e}", exc_info=True)
            return False
    
    async def setup_webhook(self) -> bool:
        """Настраивает webhook для Telegram"""
        if not self.webhook_url:
            logger.info("Webhook URL not configured, skipping webhook setup")
            return False
        
        try:
            async with TelegramAPIClient(self.telegram_token, self.chat_id) as client:
                logger.info(f"Setting webhook to: {self.webhook_url}")
                success = await client.set_webhook(self.webhook_url)
                
                if success:
                    logger.info("✅ Webhook configured successfully")
                else:
                    logger.error("❌ Failed to configure webhook")
                
                return success
                
        except Exception as e:
            logger.error(f"Error setting up webhook: {e}")
            return False
    
    async def handle_webhook_request(self, request: web.Request) -> web.Response:
        """Обрабатывает входящие webhook запросы"""
        try:
            # Проверяем IP адрес
            peername = request.transport.get_extra_info('peername')
            if peername:
                client_ip, _ = peername
                if not self._validate_ip_address(client_ip):
                    logger.warning(f"Blocked request from unauthorized IP: {client_ip}")
                    return web.Response(text='Unauthorized', status=403)
            
            # Rate limiting
            rate_key = f"webhook_{datetime.now().strftime('%H:%M')}"
            if not await self.rate_limiter.is_allowed(rate_key):
                logger.warning("Rate limit exceeded for webhook")
                return web.Response(text='Too Many Requests', status=429)
            
            # Парсим JSON данные
            try:
                data = await request.json()
            except json.JSONDecodeError:
                logger.error("Invalid JSON in webhook request")
                return web.Response(text='Invalid JSON', status=400)
            
            # Обработка callback query
            if 'callback_query' in data:
                callback = data['callback_query']
                callback_data = callback.get('data', '')
                callback_id = callback.get('id', '')
                message = callback.get('message', {})
                message_id = message.get('message_id')
                message_text = message.get('text', '')
                
                if callback_data and callback_id and message_id is not None:
                    await self.process_callback_query(
                        callback_data, callback_id, message_id, message_text
                    )
            
            # Обработка сообщений с задачами
            elif 'message' in data:
                message = data['message']
                text = message.get('text', '')
                chat_id = message.get('chat', {}).get('id')
                
                if chat_id == self.chat_id_int:
                    if any(marker in text for marker in ['•', '☀️', '🌙', '⛔', 'Дневные', 'Вечерние']):
                        await self.process_schedule_message(text)
            
            return web.Response(text='OK')
            
        except Exception as e:
            logger.error(f"Error handling webhook: {e}", exc_info=True)
            return web.Response(text='Internal Server Error', status=500)
    
    def _handle_signal(self, signum, frame):
        """Обработчик сигналов завершения"""
        logger.info(f"Received signal {signum}, initiating shutdown...")
        self.shutdown_event.set()
    
    async def start_http_server(self) -> web.AppRunner:
        """Запускает HTTP сервер для обработки запросов"""
        app = web.Application(client_max_size=10*1024*1024)
        
        app.router.add_get('/', self._handle_root_request)
        app.router.add_get('/health', self._handle_health_request)
        app.router.add_post('/webhook', self.handle_webhook_request)
        
        runner = web.AppRunner(app)
        await runner.setup()
        
        site = web.TCPSite(runner, '0.0.0.0', self.port)
        await site.start()
        
        logger.info(f"✅ HTTP server started on port {self.port}")
        return runner
    
    async def _handle_root_request(self, request: web.Request) -> web.Response:
        """Обработчик корневого маршрута"""
        uptime = str(timedelta(seconds=int(time.time() - self.start_time)))
        return web.Response(
            text=f"""Task Tracker Bot v3.0.0
Chat ID: {self.chat_id_int}
Uptime: {uptime}
Endpoints:
  • /health - Health check
  • /webhook - Telegram webhook endpoint
"""
        )
    
    async def _handle_health_request(self, request: web.Request) -> web.Response:
        """Обработчик health check"""
        health_data = {
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'uptime': time.time() - self.start_time,
            'version': '3.0.0'
        }
        
        return web.Response(
            text=json.dumps(health_data, ensure_ascii=False, indent=2),
            content_type='application/json',
            status=200
        )
    
    async def run(self):
        """Основной цикл работы бота"""
        logger.info("🚀 Starting Task Tracker Bot...")
        
        runner = None
        try:
            # Запускаем HTTP сервер
            runner = await self.start_http_server()
            
            # Настраиваем webhook если указан URL
            if self.webhook_url:
                await self.setup_webhook()
                logger.info("✅ Operating in webhook mode")
            else:
                logger.info("⚠️ Webhook not configured (set RAILWAY_PUBLIC_DOMAIN for webhook mode)")
            
            logger.info("✅ Bot is fully operational and ready")
            logger.info("=" * 60)
            
            # Ждем сигнала завершения
            await self.shutdown_event.wait()
            
            logger.info("Shutdown initiated, cleaning up...")
            
        except Exception as e:
            logger.error(f"Error in main loop: {e}", exc_info=True)
            
        finally:
            # Корректное завершение
            if runner:
                logger.info("Stopping HTTP server...")
                await runner.cleanup()
            
            uptime = str(timedelta(seconds=int(time.time() - self.start_time)))
            logger.info(f"✅ Bot shutdown complete (uptime: {uptime})")


# ============================================================================
# ТОЧКА ВХОДА
# ============================================================================

def main():
    """Точка входа приложения"""
    try:
        # Проверяем обязательные переменные
        if not os.getenv('TELEGRAM_TOKEN'):
            logger.error("TELEGRAM_TOKEN environment variable is required")
            sys.exit(1)
        
        if not os.getenv('TELEGRAM_CHAT_ID'):
            logger.error("TELEGRAM_CHAT_ID environment variable is required")
            sys.exit(1)
        
        # Создаем и запускаем бота
        bot = TaskTrackerBot()
        asyncio.run(bot.run())
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
