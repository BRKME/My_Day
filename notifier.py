#!/usr/bin/env python3
import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
from calendar import monthcalendar
import logging
import random
import sys
import os
from typing import Optional, Dict, Any, List, Tuple
import json

# Константы
SATURDAY = 5
SUNDAY = 6
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
REQUEST_TIMEOUT = 15
CACHE_DURATION_MINUTES = 30
MAX_CACHE_SIZE = 100

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class CacheManager:
    """Менеджер кэширования с ограничением по размеру"""
    
    def __init__(self, max_size: int = MAX_CACHE_SIZE):
        self._cache: Dict[str, Tuple[datetime, Any]] = {}
        self._max_size = max_size
    
    def get(self, key: str, ttl_minutes: int = CACHE_DURATION_MINUTES) -> Optional[Any]:
        """Получить значение из кэша"""
        if key not in self._cache:
            return None
        
        cached_time, value = self._cache[key]
        age_minutes = (datetime.now(timezone.utc) - cached_time).seconds / 60
        
        if age_minutes > ttl_minutes:
            del self._cache[key]
            return None
        
        return value
    
    def set(self, key: str, value: Any) -> None:
        """Установить значение в кэш"""
        # Удаляем старые записи если достигли предела
        if len(self._cache) >= self._max_size:
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
        
        self._cache[key] = (datetime.now(timezone.utc), value)
    
    def clear(self) -> None:
        """Очистить кэш"""
        self._cache.clear()


class WeatherService:
    """Сервис для работы с погодой"""
    
    def __init__(self, cache_manager: CacheManager):
        self.cache = cache_manager
        self.weather_descriptions = {
            0: "☀️ Ясно",
            1: "🌤️ Малооблачно",
            2: "⛅ Переменная облачность",
            3: "☁️ Пасмурно",
            45: "🌫️ Туман",
            48: "🌫️ Изморозь",
            51: "🌦️ Лёгкая морось",
            53: "🌦️ Морось",
            55: "🌧️ Сильная морось",
            61: "🌦️ Небольшой дождь",
            63: "🌧️ Дождь",
            65: "🌧️ Сильный дождь",
            71: "🌨️ Небольшой снег",
            73: "❄️ Снег",
            75: "❄️ Сильный снег",
            77: "🌨️ Снежная крупа",
            80: "🌦️ Ливневый дождь",
            81: "🌧️ Сильный ливень",
            82: "⛈️ Очень сильный ливень",
            85: "🌨️ Снегопад",
            86: "❄️ Сильный снегопад",
            95: "⛈️ Гроза",
            96: "⛈️ Гроза с градом",
            99: "⛈️ Сильная гроза с градом"
        }
    
    def get_weather_description(self, weather_code: int) -> str:
        """Возвращает описание погоды по коду WMO"""
        return self.weather_descriptions.get(weather_code, "🌡️ Неизвестно")
    
    async def _make_request(self, url: str) -> Optional[Dict[str, Any]]:
        """Выполнить HTTP запрос с обработкой ошибок"""
        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        logger.error(f"❌ Ошибка HTTP {response.status} для URL: {url}")
                        return None
        except aiohttp.ClientError as e:
            logger.error(f"❌ Ошибка сети: {e}")
            return None
        except asyncio.TimeoutError:
            logger.error("❌ Таймаут запроса")
            return None
        except Exception as e:
            logger.error(f"❌ Неизвестная ошибка: {e}")
            return None
    
    async def get_current_weather(self) -> str:
        """Получить текущую погоду для Санкт-Петербурга"""
        cache_key = "current_weather_spb"
        
        # Проверяем кэш
        cached = self.cache.get(cache_key)
        if cached is not None:
            logger.info("✅ Прогноз погоды взят из кэша")
            return cached
        
        latitude = 59.9311
        longitude = 30.3609
        
        url = (f"https://api.open-meteo.com/v1/forecast?"
               f"latitude={latitude}&longitude={longitude}&"
               f"current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m&"
               f"timezone=Europe/Moscow&forecast_days=1")
        
        data = await self._make_request(url)
        if not data:
            return ""
        
        current = data.get('current', {})
        temp = current.get('temperature_2m', 'N/A')
        feels_like = current.get('apparent_temperature', 'N/A')
        humidity = current.get('relative_humidity_2m', 'N/A')
        wind_speed = current.get('wind_speed_10m', 'N/A')
        weather_code = current.get('weather_code', 0)
        
        weather_desc = self.get_weather_description(weather_code)
        
        weather_text = (f"🌍 <b>Погода в Санкт-Петербурге:</b>\n"
                       f"🌡️ Температура: {temp}°C (ощущается как {feels_like}°C)\n"
                       f"💧 Влажность: {humidity}%\n"
                       f"💨 Ветер: {wind_speed} км/ч\n"
                       f"{weather_desc}\n\n")
        
        # Сохраняем в кэш
        self.cache.set(cache_key, weather_text)
        logger.info("✅ Прогноз погоды получен")
        return weather_text
    
    async def get_weekend_forecast(self) -> str:
        """Получить прогноз на выходные"""
        cache_key = "weekend_forecast_spb"
        
        # Проверяем кэш
        cached = self.cache.get(cache_key)
        if cached is not None:
            logger.info("✅ Прогноз на выходные взят из кэша")
            return cached
        
        latitude = 59.9311
        longitude = 30.3609
        
        url = (f"https://api.open-meteo.com/v1/forecast?"
               f"latitude={latitude}&longitude={longitude}&"
               f"daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code,wind_speed_10m_max&"
               f"timezone=Europe/Moscow&forecast_days=7")
        
        data = await self._make_request(url)
        if not data:
            return ""
        
        daily = data.get('daily', {})
        times = daily.get('time', [])
        temp_max = daily.get('temperature_2m_max', [])
        temp_min = daily.get('temperature_2m_min', [])
        precipitation = daily.get('precipitation_sum', [])
        weather_codes = daily.get('weather_code', [])
        wind_speed = daily.get('wind_speed_10m_max', [])
        
        today = datetime.now(timezone.utc)
        days_until_saturday = (SATURDAY - today.weekday()) % 7
        saturday_date = today + timedelta(days=days_until_saturday)
        sunday_date = saturday_date + timedelta(days=1)
        
        weather_text = f"📅 <b>Прогноз на выходные:</b>\n\n"
        
        for i, date_str in enumerate(times):
            try:
                forecast_date = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
            except ValueError:
                continue
            
            if forecast_date.date() == saturday_date.date():
                weather_desc = self.get_weather_description(weather_codes[i])
                weather_text += f"<b>🗓️ Суббота ({forecast_date.strftime('%d.%m')}):</b>\n"
                weather_text += f"🌡️ {temp_min[i]}°C ... {temp_max[i]}°C\n"
                weather_text += f"💨 Ветер до {wind_speed[i]} км/ч\n"
                weather_text += f"{weather_desc}\n"
                if precipitation[i] > 0:
                    weather_text += f"🌧️ Осадки: {precipitation[i]} мм\n"
                weather_text += "\n"
            
            elif forecast_date.date() == sunday_date.date():
                weather_desc = self.get_weather_description(weather_codes[i])
                weather_text += f"<b>🗓️ Воскресенье ({forecast_date.strftime('%d.%m')}):</b>\n"
                weather_text += f"🌡️ {temp_min[i]}°C ... {temp_max[i]}°C\n"
                weather_text += f"💨 Ветер до {wind_speed[i]} км/ч\n"
                weather_text += f"{weather_desc}\n"
                if precipitation[i] > 0:
                    weather_text += f"🌧️ Осадки: {precipitation[i]} мм\n"
                weather_text += "\n"
        
        # Сохраняем в кэш
        self.cache.set(cache_key, weather_text)
        logger.info("✅ Прогноз на выходные получен")
        return weather_text


class MessageFormatter:
    """Форматировщик сообщений"""
    
    DAY_NAMES_RU = {
        'monday': 'Понедельник',
        'tuesday': 'Вторник',
        'wednesday': 'Среда',
        'thursday': 'Четверг',
        'friday': 'Пятница',
        'saturday': 'Суббота',
        'sunday': 'Воскресенье'
    }
    
    def __init__(self, weather_service: WeatherService):
        self.weather_service = weather_service
        self.wisdoms = self._load_wisdoms()
    
    def _load_wisdoms(self) -> List[str]:
        """Загрузить список мудростей"""
        return [
            "Лучший способ начать — перестать говорить и начать делать. — Уолт Дисней",
            "Не ждите. Время никогда не будет подходящим. — Наполеон Хилл",
            "Начало — самая важная часть работы. — Платон",
            "Путь в тысячу миль начинается с одного шага. — Лао-цзы",
            "Делай сегодня то, что другие не хотят, завтра будешь жить так, как другие не могут. — Джаред Лето",
            "Успех — это сумма маленьких усилий, повторяемых день за днём. — Роберт Кольер",
            "Я твердо верю в удачу, и чем больше я работаю — тем я удачливее. — Томас Джефферсон",
            "Неудача — это просто возможность начать снова, но уже более мудро. — Генри Форд",
            "Единственный способ сделать великую работу — любить то, что ты делаешь. — Стив Джобс",
            "Успех обычно приходит к тем, кто слишком занят, чтобы его искать. — Генри Дэвид Торо",
        ]
    
    def get_random_wisdom(self) -> str:
        """Получить случайную мудрость"""
        if not self.wisdoms:
            return "Каждый день даёт шанс стать лучше. — Неизвестный автор"
        return random.choice(self.wisdoms)
    
    def get_russian_day_name(self, day_of_week: str) -> str:
        """Получить русское название дня недели"""
        return self.DAY_NAMES_RU.get(day_of_week, day_of_week.capitalize())
    
    def _truncate_message(self, message: str) -> str:
        """Обрезать сообщение если превышен лимит Telegram"""
        if len(message) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            return message
        
        truncated = message[:TELEGRAM_MAX_MESSAGE_LENGTH - 100]
        return truncated + "...\n\n[сообщение было обрезано]"
    
    async def format_morning_message(self, date_str: str, day_of_week: str, schedule: Dict[str, List[str]], 
                                    prayer_url: str) -> str:
        """Форматировать утреннее сообщение"""
        day_ru = self.get_russian_day_name(day_of_week)
        wisdom = self.get_random_wisdom()
        
        # Получаем погоду
        weather = await self.weather_service.get_current_weather()
        content = weather if weather else ""
        
        # Добавляем прогноз на выходные в определенные дни
        if day_of_week in ['monday', 'wednesday', 'friday']:
            weekend_forecast = await self.weather_service.get_weekend_forecast()
            if weekend_forecast:
                content += weekend_forecast
        
        # Добавляем заголовок
        content += f"🌅 <b>План на {date_str}</b>\n🗓️ {day_ru}\n\n"
        
        # Добавляем дневные задачи
        if schedule.get('день'):
            content += "<b>☀️ Дневные задачи:</b>\n"
            for task in schedule['день']:
                content += f"• {task}\n"
        
        # Добавляем запреты
        if schedule.get('нельзя_день'):
            content += "\n<b>⛔ Нельзя делать:</b>\n"
            for task in schedule['нельзя_день']:
                content += f"• {task}\n"
        
        # Добавляем мудрость и ссылку
        content += f"\n💡 <b>Мудрость дня:</b>\n{wisdom}"
        content += f"\n\n🙏 <a href='{prayer_url}'>Утренняя молитва</a>"
        
        return self._truncate_message(content)
    
    async def format_evening_message(self, date_str: str, day_of_week: str, schedule: Dict[str, List[str]]) -> str:
        """Форматировать вечернее сообщение"""
        day_ru = self.get_russian_day_name(day_of_week)
        wisdom = self.get_random_wisdom()
        task_count = len(schedule.get('вечер', []))
        target_score = max(0, task_count - 1)
        
        # Получаем погоду
        weather = await self.weather_service.get_current_weather()
        content = weather if weather else ""
        
        # Добавляем заголовок
        content += f"🌙 <b>Вечерний план на {date_str}</b>\n🗓️ <b>{day_ru}</b>\n\n"
        
        # Добавляем вечерние задачи
        if schedule.get('вечер'):
            content += "<b>Вечерние задачи:</b>\n"
            for task in schedule['вечер']:
                content += f"• {task}\n"
        
        # Добавляем итог
        content += (f"\n🎯 <b>Твоя миссия набрать вечером {target_score} баллов!</b>\n"
                   f"🌜 <b>Отличный день! Завершай дела и отдыхай!</b>\n"
                   f"💡 <i>Мудрость дня:</i>\n<b>{wisdom}</b>")
        
        return self._truncate_message(content)


class TelegramService:
    """Сервис для работы с Telegram"""
    
    def __init__(self, token: str, chat_id: str):
        if not token or not chat_id:
            raise ValueError("Токен и chat_id обязательны")
        
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
    
    def create_progress_button(self) -> Dict[str, Any]:
        """Создать кнопку для прогресса"""
        return {
            'inline_keyboard': [
                [{'text': 'Обновить прогресс', 'callback_data': 'save_progress'}]
            ]
        }
    
    async def send_message(self, text: str, add_button: bool = False, 
                          disable_preview: bool = False) -> bool:
        """Отправить сообщение в Telegram"""
        if not text:
            logger.error("❌ Пустой текст сообщения")
            return False
        
        url = f"{self.base_url}/sendMessage"
        payload = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': disable_preview
        }
        
        if add_button:
            payload['reply_markup'] = self.create_progress_button()
        
        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as response:
                    if response.status == 200:
                        logger.info("✅ Сообщение отправлено в Telegram")
                        return True
                    else:
                        error_text = await response.text()
                        logger.error(f"❌ Ошибка Telegram API: {response.status} - {error_text}")
                        return False
        except aiohttp.ClientError as e:
            logger.error(f"❌ Ошибка сети при отправке в Telegram: {e}")
            return False
        except asyncio.TimeoutError:
            logger.error("❌ Таймаут при отправке в Telegram")
            return False
        except Exception as e:
            logger.error(f"❌ Неизвестная ошибка при отправке в Telegram: {e}")
            return False


class EventManager:
    """Менеджер событий"""
    
    def __init__(self):
        self.recurring_events = {
            'tarelka': {'name': 'Семейная традиция - Путешествие на тарелке', 
                       'file': 'tarelka.txt', 'rule': 'last_saturday'},
            'chronos': {'name': 'Семейная традиция - Вечер воспоминаний. Хранители времени', 
                       'file': 'chronos.txt', 'rule': 'third_saturday'},
            'new': {'name': 'Семейная традиция - День нового', 
                   'file': 'new.txt', 'rule': 'second_saturday'}
        }
    
    def get_last_day_of_month(self, year: int, month: int, day_of_week: int) -> Optional[int]:
        """Получить последний день недели месяца"""
        cal = monthcalendar(year, month)
        for week in reversed(cal):
            if week[day_of_week] != 0:
                return week[day_of_week]
        return None
    
    def get_nth_day_of_month(self, year: int, month: int, day_of_week: int, n: int) -> Optional[int]:
        """Получить n-ный день недели месяца"""
        cal = monthcalendar(year, month)
        count = 0
        for week in cal:
            if week[day_of_week] != 0:
                count += 1
                if count == n:
                    return week[day_of_week]
        return None
    
    def get_event_date_by_rule(self, rule: str, year: int, month: int) -> Optional[Tuple[int, int, int]]:
        """Получить дату события по правилу"""
        if rule == 'last_saturday':
            day = self.get_last_day_of_month(year, month, SATURDAY)
        elif rule == 'second_saturday':
            day = self.get_nth_day_of_month(year, month, SATURDAY, 2)
        elif rule == 'third_saturday':
            day = self.get_nth_day_of_month(year, month, SATURDAY, 3)
        else:
            return None
        
        return (year, month, day) if day else None
    
    def check_recurring_events(self) -> List[Dict[str, Any]]:
        """Проверить предстоящие события"""
        from datetime import date as dt
        
        today = datetime.now()
        year, month, day = today.year, today.month, today.day
        reminders = []
        
        for event_key, event in self.recurring_events.items():
            event_date = self.get_event_date_by_rule(event['rule'], year, month)
            if not event_date:
                continue
            
            event_year, event_month, event_day = event_date
            event_dt = dt(event_year, event_month, event_day)
            today_dt = dt(year, month, day)
            days_until = (event_dt - today_dt).days
            
            if days_until == 7:
                reminders.append({'key': event_key, 'event': event, 'type': 'week_before'})
            elif days_until == 3:
                reminders.append({'key': event_key, 'event': event, 'type': 'three_days_before'})
            elif days_until == 0:
                reminders.append({'key': event_key, 'event': event, 'type': 'event_day'})
        
        return reminders
    
    async def fetch_event_file(self, filename: str) -> Optional[str]:
        """Загрузить файл события из GitHub"""
        # Валидация имени файла
        if not filename.endswith('.txt'):
            logger.error(f"❌ Неподдерживаемый формат файла: {filename}")
            return None
        
        if '..' in filename or '/' in filename:
            logger.error(f"❌ Попытка path traversal: {filename}")
            return None
        
        url = f"https://raw.githubusercontent.com/BRKME/Day/main/{filename}"
        
        try:
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        content = await response.text()
                        logger.info(f"✅ Файл {filename} загружен")
                        return content
                    else:
                        logger.error(f"❌ Ошибка загрузки {filename}: {response.status}")
                        return None
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки файла: {e}")
            return None


class PersonalScheduleNotifier:
    """Основной класс для отправки уведомлений"""
    
    def __init__(self):
        # Инициализация зависимостей
        self.cache_manager = CacheManager()
        self.weather_service = WeatherService(self.cache_manager)
        self.message_formatter = MessageFormatter(self.weather_service)
        self.event_manager = EventManager()
        
        # Загрузка конфигурации
        self._load_config()
        
        # Расписание
        self.schedule = self._load_schedule()
    
    def _load_config(self):
        """Загрузить конфигурацию из переменных окружения"""
        self.telegram_token = os.getenv('TELEGRAM_TOKEN')
        if not self.telegram_token:
            raise ValueError("TELEGRAM_TOKEN не найден в переменных окружения!")
        
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID')
        if not self.chat_id:
            raise ValueError("TELEGRAM_CHAT_ID не найден! Укажи ID канала (например -1001234567890)")
        
        self.prayer_url = "https://brkme.github.io/My_Day/prayer.html"
        self.ss_url = "https://brkme.github.io/My_Day/ss.html"
        
        # Логируем безопасную версию chat_id
        safe_chat_id = str(self.chat_id)[:6] + "..." if len(str(self.chat_id)) > 6 else "***"
        logger.info(f"Notifier запущен | Отправка в: {safe_chat_id}")
    
    def _load_schedule(self) -> Dict[str, Dict[str, List[str]]]:
        """Загрузить полное расписание"""
        return {
            'monday': {
                'день': [
                    'Прими витамины (1 min «Топливо» для мозга)',
                    'Взвесься (1 min Цель 85 кг)',
                    'Зарядка (15 min кнопка «Старт» для твоей энергии)',
                    'Включи Мозг (5 min «Ключ» к новым источникам дохода)',
                    'Сделай комплимент Марте и Саше (твои девочки почувствуют себя важными и любимыми)',
                    'Занятия English на YouTube (20 min)',
                    'Читать в дороге (25 min это Спорт для мозга)',
                    'Проверь Цели (10 min Цели — твой навигатор)',
                    'Подтянуться min 12 раз',
                    'Упражнение на пресс 2 подхода min 16 раз',
                    'Молчание золото. Не перебивай (Молчание строит доверие)'
                ],
                'нельзя_день': [
                    'Мат (Мат — это мусор)',
                    'Д (Слил энергию — слил фокус — не заработал)',
                    'Алкоголь (Он крадет твою энергию, деньги и внешность)'
                ],
                'вечер': [
                    'Читать в дороге (30 min это Спорт для мозга)',
                    'Семейный ужин (30 min)',
                    'Марта моет посуду',
                    'Отдых (60 min Ментальная перезагрузка)',
                    'CRPT LP (30 min)',
                    'Pet Project (120 min)',
                    'Читать с Мартой (20 min)',
                    'GROK сессия с психологом (15 min)',
                    'Эмоциональный дневник (10 min управляешь эмоциями и счастьем)',
                    'Прими Магний перед сном (Выключи стресс)',
                    'Вечерняя благодарность (Семейная традиция)'
                ]
            },
            'tuesday': {
                'день': [
                    'Прими витамины (1 min «Топливо» для мозга)',
                    'Взвесься (1 min Цель 85 кг)',
                    'Зарядка (15 min кнопка «Старт» для твоей энергии)',
                    'Включи Мозг (5 min «Ключ» к новым источникам дохода)',
                    'Сделай комплимент Марте и Саше (твои девочки почувствуют себя важными и любимыми)',
                    'Занятия English на YouTube (20 min)',
                    'Читать в дороге (25 min это Спорт для мозга)',
                    'Проверь Цели (10 min Цели — твой навигатор)',
                    'Подтянуться min 12 раз',
                    'Упражнение на пресс 2 подхода min 16 раз',
                    'Молчание золото. Не перебивай (Молчание строит доверие)'
                ],
                'нельзя_день': [
                    'Мат (Мат — это мусор)',
                    'Д (Слил энергию — слил фокус — не заработал)',
                    'Алкоголь (Он крадет твою энергию, деньги и внешность)'
                ],
                'вечер': [
                    'Читать в дороге (30 min это Спорт для мозга)',
                    'Семейный ужин (30 min)',
                    'Аркаша моет посуду',
                    'Отдых (60 min Ментальная перезагрузка)',
                    'CRPT LP (30 min)',
                    'Pet Project (120 min)',
                    'Читать с Мартой (20 min)',
                    'GROK сессия с психологом (15 min)',
                    'Эмоциональный дневник (10 min управляешь эмоциями и счастьем)',
                    'Прими Магний перед сном (Выключи стресс)',
                    'Вечерняя благодарность (Семейная традиция)'
                ]
            },
            'wednesday': {
                'день': [
                    'Прими витамины (1 min «Топливо» для мозга)',
                    'Взвесься (1 min Цель 85 кг)',
                    'Зарядка (15 min кнопка «Старт» для твоей энергии)',
                    'Включи Мозг (5 min «Ключ» к новым источникам дохода)',
                    'Сделай комплимент Марте и Саше (твои девочки почувствуют себя важными и любимыми)',
                    'Занятия English на YouTube (20 min)',
                    'Читать в дороге (25 min это Спорт для мозга)',
                    'Проверь Цели (10 min Цели — твой навигатор)',
                    'Подтянуться min 12 раз',
                    'Упражнение на пресс 2 подхода min 16 раз',
                    'Молчание золото. Не перебивай (Молчание строит доверие)'
                ],
                'нельзя_день': [
                    'Мат (Мат — это мусор)',
                    'Д (Слил энергию — слил фокус — не заработал)',
                    'Алкоголь (Он крадет твою энергию, деньги и внешность)'
                ],
                'вечер': [
                    'Читать в дороге (30 min это Спорт для мозга)',
                    'Семейный ужин (30 min)',
                    'Марта моет посуду',
                    'Отдых (60 min Ментальная перезагрузка)',
                    'CRPT LP (30 min)',
                    'Pet Project (120 min)',
                    'Читать с Мартой (20 min)',
                    'GROK сессия с психологом (15 min)',
                    'Эмоциональный дневник (10 min управляешь эмоциями и счастьем)',
                    'Прими Магний перед сном (Выключи стресс)',
                    'Вечерняя благодарность (Семейная традиция)'
                ]
            },
            'thursday': {
                'день': [
                    'Прими витамины (1 min «Топливо» для мозга)',
                    'Взвесься (1 min Цель 85 кг)',
                    'Зарядка (15 min кнопка «Старт» для твоей энергии)',
                    'Включи Мозг (5 min «Ключ» к новым источникам дохода)',
                    'Сделай комплимент Марте и Саше (твои девочки почувствуют себя важными и любимыми)',
                    'Занятия English на YouTube (20 min)',
                    'Читать в дороге (25 min это Спорт для мозга)',
                    'Проверь Цели (10 min Цели — твой навигатор)',
                    'Подтянуться min 12 раз',
                    'Упражнение на пресс 2 подхода min 16 раз',
                    'Молчание золото. Не перебивай (Молчание строит доверие)'
                ],
                'нельзя_день': [
                    'Мат (Мат — это мусор)',
                    'Д (Слил энергию — слил фокус — не заработал)',
                    'Алкоголь (Он крадет твою энергию, деньги и внешность)'
                ],
                'вечер': [
                    'Читать в дороге (30 min это Спорт для мозга)',
                    'Семейный ужин (30 min)',
                    'Аркаша моет посуду',
                    'Отдых (60 min Ментальная перезагрузка)',
                    'CRPT LP (30 min)',
                    'Pet Project (120 min)',
                    'Читать с Мартой (20 min)',
                    'GROK сессия с психологом (15 min)',
                    'Эмоциональный дневник (10 min управляешь эмоциями и счастьем)',
                    'Прими Магний перед сном (Выключи стресс)',
                    'Вечерняя благодарность (Семейная традиция)'
                ]
            },
            'friday': {
                'день': [
                    'Прими витамины (1 min «Топливо» для мозга)',
                    'Взвесься (1 min Цель 85 кг)',
                    'Зарядка (15 min кнопка «Старт» для твоей энергии)',
                    'Включи Мозг (5 min «Ключ» к новым источникам дохода)',
                    'Сделай комплимент Марте и Саше (твои девочки почувствуют себя важными и любимыми)',
                    'Занятия English на YouTube (20 min)',
                    'Читать в дороге (25 min это Спорт для мозга)',
                    'Позвонить тете Ларисе',
                    'Подтянуться min 12 раз',
                    'Упражнение на пресс 2 подхода min 16 раз',
                    'Молчание золото. Не перебивай (Молчание строит доверие)'
                ],
                'нельзя_день': [
                    'Мат (Мат — это мусор)',
                    'Д (Слил энергию — слил фокус — не заработал)',
                    'Алкоголь (Он крадет твою энергию, деньги и внешность)'
                ],
                'вечер': [
                    'Читать в дороге (30 min это Спорт для мозга)',
                    'Семейный ужин (30 min)',
                    'Марта моет посуду',
                    'Отдых (120 min Ментальная перезагрузка)',
                    'Pet Project (120 min)',
                    'Читать с Мартой (20 min)',
                    'GROK сессия с психологом (15 min)',
                    'Янтарные бусы - то символ свободы',
                    'Эмоциональный дневник (10 min управляешь эмоциями и счастьем)',
                    'Прими Магний перед сном (Выключи стресс)',
                    'Вечерняя благодарность (Семейная традиция)',
                    'Зачёт по чистоте комнаты в пятницу. Семейная традиция'
                ]
            },
            'saturday': {
                'день': [
                    'Прими витамины (1 min «Топливо» для мозга)',
                    'Взвесься (1 min Цель 85 кг)',
                    'Зарядка (15 min кнопка «Старт» для твоей энергии)',
                    'Включи Мозг (5 min «Ключ» к новым источникам дохода)',
                    'Сделай комплимент Марте и Саше (твои девочки почувствуют себя важными и любимыми)',
                    'Читать в дороге (25 min это Спорт для мозга)',
                    'Полить Цветы',
                    'Проверь Цели (10 min Цели — твой навигатор)',
                    'LP %',
                    'Подтянуться min 12 раз',
                    'Упражнение на пресс 2 подхода min 16 раз',
                    'Молчание золото. Не перебивай (Молчание строит доверие)'
                ],
                'нельзя_день': [
                    'Мат (Мат — это мусор)',
                    'Д (Слил энергию — слил фокус — не заработал)',
                    'Алкоголь (Он крадет твою энергию, деньги и внешность)'
                ],
                'вечер': [
                    'Читать в дороге (30 min это Спорт для мозга)',
                    'Семейный ужин (30 min)',
                    'Аркаша моет посуду',
                    'Pet Project (120 min)',
                    'Читать с Мартой (20 min)',
                    'GROK сессия с психологом (15 min)',
                    'Эмоциональный дневник (10 min управляешь эмоциями и счастьем)',
                    'Прими Магний перед сном (Выключи стресс)',
                    'Вечерняя благодарность (Семейная традиция)',
                    'Семейный просмотр фильма'
                ]
            },
            'sunday': {
                'день': [
                    'Сделай комплимент Марте и Саше (твои девочки почувствуют себя важными и любимыми)',
                    'День без гаджетов (Живое общение)',
                    'Семейные традиции',
                    'Family Day (Фундамент доверия)',
                    'Молчание золото. Не перебивай (Молчание строит доверие)',
                    'Д (Сегодня мо-о-о-ожно)',
                    'Семейная прогулка',
                    'Семейный завтрак'
                ],
                'нельзя_день': [
                    'Мат (Мат — это мусор)',
                    'Алкоголь (Он крадет твою энергию, деньги и внешность)'
                ],
                'вечер': [
                    'Родители моют посуду',
                    'Прими Магний перед сном (Выключи стресс)',
                    'Вечерняя благодарность (Семейная традиция)'
                ]
            }
        }
    
    def get_today_schedule(self) -> Tuple[str, str, Dict[str, List[str]]]:
        """Получить расписание на сегодня"""
        try:
            today = datetime.now(timezone.utc)
            date_str = today.strftime("%d.%m.%Y")
            
            # Явное соответствие дней недели (не зависит от локали)
            day_number = today.weekday()  # 0 = Monday, 6 = Sunday
            day_mapping = {
                0: 'monday',
                1: 'tuesday',
                2: 'wednesday',
                3: 'thursday',
                4: 'friday',
                5: 'saturday',
                6: 'sunday'
            }
            
            day_of_week = day_mapping.get(day_number, 'monday')
            logger.info(f"📅 Сегодня: {date_str}, день недели: {day_of_week}")
            
            today_schedule = self.schedule.get(day_of_week, {})
            return date_str, day_of_week, today_schedule
            
        except Exception as e:
            logger.error(f"❌ Ошибка получения расписания: {e}")
            raise RuntimeError(f"Не удалось получить расписание на сегодня: {e}")
    
    async def send_message_for_period(self, period: str) -> bool:
        """Отправить сообщение для указанного периода"""
        if period not in ('morning', 'day', 'evening'):
            logger.error(f"❌ Неизвестный период: {period}")
            return False
        
        try:
            # Получаем расписание
            date_str, day_of_week, schedule = self.get_today_schedule()
            
            # Форматируем сообщение
            if period in ('morning', 'day'):
                message = await self.message_formatter.format_morning_message(
                    date_str, day_of_week, schedule, self.prayer_url
                )
                add_button = True
                
                # Добавляем напоминания о событиях для утреннего периода
                if period == 'morning':
                    reminders = self.event_manager.check_recurring_events()
                    for reminder in reminders:
                        event = reminder['event']
                        event_content = await self.event_manager.fetch_event_file(event['file'])
                        
                        if reminder['type'] == 'week_before':
                            message += f"\n\n🔔 <b>НАПОМИНАНИЕ (За 7 дней):</b>\n<b>{event['name']}</b>\n"
                        elif reminder['type'] == 'three_days_before':
                            message += f"\n\n🔔 <b>НАПОМИНАНИЕ (За 3 дня):</b>\n<b>{event['name']}</b>\n"
                        elif reminder['type'] == 'event_day':
                            message += f"\n\n🎉 <b>СЕГОДНЯ:</b>\n<b>{event['name']}</b>\n"
                        
                        if event_content:
                            message += f"{event_content}"
                
                # Для воскресенья добавляем ссылку на семейный совет
                ss_content = (day_of_week == 'sunday')
                
            else:  # evening
                message = await self.message_formatter.format_evening_message(
                    date_str, day_of_week, schedule
                )
                add_button = True
                ss_content = False
            
            # Отправляем сообщение
            telegram_service = TelegramService(self.telegram_token, self.chat_id)
            success = await telegram_service.send_message(message, add_button=add_button)
            
            # Отправляем дополнительное сообщение о семейном совете если нужно
            if success and ss_content:
                family_msg = (f"<b>📋 Семейный совет:</b>\n\n"
                             f"🔗 <a href='{self.ss_url}'>Открыть структуру Семейного Совета</a>")
                await telegram_service.send_message(family_msg, disable_preview=True)
            
            return success
            
        except Exception as e:
            logger.error(f"❌ Ошибка при отправке сообщения: {e}")
            return False


async def main(period: str):
    """Основная функция"""
    logger.info(f"🚀 Запуск для периода: {period}")
    
    try:
        notifier = PersonalScheduleNotifier()
        success = await notifier.send_message_for_period(period)
        
        if success:
            logger.info("🎉 Успешно завершено!")
            return 0
        else:
            logger.error("💥 Ошибка при отправке")
            return 1
            
    except ValueError as e:
        logger.error(f"❌ Ошибка конфигурации: {e}")
        return 1
    except RuntimeError as e:
        logger.error(f"❌ Ошибка выполнения: {e}")
        return 1
    except Exception as e:
        logger.error(f"❌ Неожиданная ошибка: {e}")
        return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("❌ Использование: python notifier.py <morning|day|evening>")
        print("   morning - утреннее уведомление")
        print("   day     - дневное уведомление")
        print("   evening - вечернее уведомление")
        sys.exit(1)
    
    period = sys.argv[1].lower()
    if period not in ('morning', 'day', 'evening'):
        print(f"❌ Неверный период: {period}")
        print("   Допустимые значения: morning, day, evening")
        sys.exit(1)
    
    exit_code = asyncio.run(main(period))
    sys.exit(exit_code)
