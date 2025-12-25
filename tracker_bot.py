#!/usr/bin/env python3
"""
Telegram бот для отслеживания выполнения задач - ФИНАЛЬНАЯ ВЕРСИЯ
Исправления:
1. Система штрафов через Telegram API
2. Исправление 161% → 100%
3. Убраны звёздочки из итогов
4. Эмодзи 📊 вместо 🌙
"""

import asyncio
import aiohttp
from aiohttp import web
import json
import logging
from datetime import datetime, timedelta
import os
import re

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class TaskTrackerBot:
    def __init__(self):
        self.telegram_token = os.getenv('TELEGRAM_TOKEN', '')
        if not self.telegram_token:
            raise ValueError("❌ TELEGRAM_TOKEN не найден в переменных окружения!")
        
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID', '')
        if not self.chat_id:
            raise ValueError("❌ TELEGRAM_CHAT_ID не найден в переменных окружения!")
        
        self.stats_file = "stats.json"
        self.message_state_file = "message_states.json"
        self.last_update_id = 0
        
        # Хранилище текущего состояния для каждого сообщения
        # {message_id: {'morning': [0,1,2], 'day': [0], 'evening': [], 'original_text': '...'}}
        self.message_state = self.load_message_states()
        
    def parse_tasks(self, message_text):
        """Парсит задачи из сообщения notifier.py"""
        tasks = {
            'morning': [],  # Оставляем для обратной совместимости, но не используем
            'day': [],
            'cant_do': [],  # Новая секция "Нельзя делать"
            'evening': []
        }
        
        lines = message_text.split('\n')
        current_section = None
        
        for line in lines:
            line = line.strip()
            
            # Определяем секцию (убираем HTML теги для проверки)
            clean_line = line.replace('<b>', '').replace('</b>', '').replace('<i>', '').replace('</i>', '')
            
            # НАЧАЛО СЕКЦИЙ (включаем парсинг)
            if ('📋' in clean_line or '☀️' in clean_line) and 'Дневн' in clean_line:
                current_section = 'day'
                continue
            elif any(marker in clean_line for marker in ['⛔', '⛔️', 'Нельзя делать']):
                current_section = 'cant_do'
                continue
            elif ('🌙' in clean_line and 'Вечерн' in clean_line) or ('📋' in clean_line and 'Вечерн' in clean_line) or 'Вечерние задачи' in clean_line:
                current_section = 'evening'
                continue
            
            # КОНЕЦ СЕКЦИЙ (выключаем парсинг)
            elif any(marker in clean_line for marker in [
                'Мудрость дня',
                '🙏 Утренняя молитва',
                '🎉 СЕГОДНЯ',
                '📅 События',
                'Занятия детей'  # НОВОЕ: пропускаем расписание детей
            ]):
                current_section = None
                continue
            
            # Собираем задачи
            if current_section and line.startswith('•'):
                task_text = line[1:].strip()  # Убираем •
                if task_text:
                    tasks[current_section].append(task_text)
        
        logger.info(f"📋 Распарсено задач: день={len(tasks['day'])}, нельзя={len(tasks['cant_do'])}, вечер={len(tasks['evening'])}")
        return tasks
    
    def create_checklist_keyboard(self, tasks, completed):
        """Создаёт inline-клавиатуру с задачами"""
        keyboard = []
        
        # Дневные задачи
        if tasks['day']:
            keyboard.append([{'text': '☀️ ДНЕВНЫЕ ЗАДАЧИ', 'callback_data': 'header'}])
            for idx, task in enumerate(tasks['day']):
                is_done = idx in completed.get('day', [])
                emoji = '⭐' if is_done else '☆'
                # Обрезаем длинный текст для кнопки
                short_task = task[:35] + '...' if len(task) > 35 else task
                keyboard.append([{
                    'text': f'{emoji} {idx+1}. {short_task}',
                    'callback_data': f'toggle_day_{idx}'
                }])
        
        # Нельзя делать
        if tasks['cant_do']:
            keyboard.append([{'text': '⛔ НЕЛЬЗЯ ДЕЛАТЬ', 'callback_data': 'header'}])
            for idx, task in enumerate(tasks['cant_do']):
                is_done = idx in completed.get('cant_do', [])
                emoji = '⭐' if is_done else '☆'
                short_task = task[:32] + '...' if len(task) > 32 else task
                keyboard.append([{
                    'text': f'{emoji} {idx+1}. НЕ {short_task}',
                    'callback_data': f'toggle_cant_do_{idx}'
                }])
        
        # Вечерние задачи  
        if tasks['evening']:
            keyboard.append([{'text': '🌙 ВЕЧЕРНИЕ ЗАДАЧИ', 'callback_data': 'header'}])
            for idx, task in enumerate(tasks['evening']):
                is_done = idx in completed.get('evening', [])
                emoji = '⭐' if is_done else '☆'
                short_task = task[:35] + '...' if len(task) > 35 else task
                keyboard.append([{
                    'text': f'{emoji} {idx+1}. {short_task}',
                    'callback_data': f'toggle_evening_{idx}'
                }])
        
        # Кнопки управления
        keyboard.append([
            {'text': '💾 Сохранить', 'callback_data': 'save_progress'},
            {'text': '❌ Отмена', 'callback_data': 'cancel_update'}
        ])
        
        return {'inline_keyboard': keyboard}
    
    def format_checklist_message(self, tasks, completed):
        """Форматирует текст сообщения с чек-листом"""
        msg = "✅ <b>Отметь выполненные задачи:</b>\n"
        
        total_tasks = 0
        total_done = 0
        
        if tasks['day']:
            msg += "\n☀️ <b>ДНЕВНЫЕ:</b>\n"
            for idx, task in enumerate(tasks['day']):
                emoji = '⭐' if idx in completed.get('day', []) else '☆'
                msg += f"{emoji} {task}\n"
                total_tasks += 1
                if idx in completed.get('day', []):
                    total_done += 1
        
        if tasks['cant_do']:
            msg += "\n⛔ <b>НЕЛЬЗЯ ДЕЛАТЬ:</b>\n"
            for idx, task in enumerate(tasks['cant_do']):
                emoji = '⭐' if idx in completed.get('cant_do', []) else '☆'
                msg += f"{emoji} НЕ {task}\n"
                total_tasks += 1
                if idx in completed.get('cant_do', []):
                    total_done += 1
        
        if tasks['evening']:
            msg += "\n🌙 <b>ВЕЧЕРНИЕ:</b>\n"
            for idx, task in enumerate(tasks['evening']):
                emoji = '⭐' if idx in completed.get('evening', []) else '☆'
                msg += f"{emoji} {task}\n"
                total_tasks += 1
                if idx in completed.get('evening', []):
                    total_done += 1
        
        # Прогресс
        percentage = int((total_done / total_tasks * 100)) if total_tasks > 0 else 0
        bar = self.get_progress_bar(percentage)
        msg += f"\n📊 <b>Прогресс:</b> {bar} {total_done}/{total_tasks} ({percentage}%)\n"
        
        return msg
    
    def update_original_message_with_progress(self, original_text, tasks, completed):
        """ЭТАП 3: Обновляет исходное сообщение с прогресс-барами"""
        lines = original_text.split('\n')
        
        # ШАГ 1: ОЧИСТКА - удаляем ВСЕ старые прогресс-бары и галочки
        cleaned_lines = []
        for line in lines:
            stripped = line.strip()
            
            # Пропускаем старые прогресс-бары
            if stripped.startswith('📊') or stripped.startswith('🎯 Общий прогресс'):
                continue
            
            # Убираем старые звёздочки из задач
            if line.startswith('•') and '⭐' in line:
                # Удаляем все звёздочки и восстанавливаем оригинал
                cleaned = line.replace('⭐ ', '').replace(' ⭐', '')
                # Убираем лишние пробелы
                parts = cleaned.split('•', 1)
                if len(parts) == 2:
                    cleaned = '• ' + parts[1].strip()
                cleaned_lines.append(cleaned)
            else:
                cleaned_lines.append(line)
        
        # ШАГ 2: ДОБАВЛЕНИЕ - добавляем новые прогресс-бары и галочки
        updated_lines = []
        current_section = None
        task_counters = {'morning': 0, 'day': 0, 'cant_do': 0, 'evening': 0}
        
        for line in cleaned_lines:
            clean_line = line.replace('<b>', '').replace('</b>', '').replace('<i>', '').replace('</i>', '')
            
            # Определяем секцию
            if ('📋' in clean_line or '☀️' in clean_line) and 'Дневн' in clean_line:
                current_section = 'day'
                updated_lines.append(line)
                continue
            elif 'Вечерние задачи' in clean_line or ('🌙' in clean_line and 'Вечерн' in clean_line) or ('📋' in clean_line and 'Вечерн' in clean_line):
                current_section = 'evening'
                updated_lines.append(line)
                continue
            elif any(marker in clean_line for marker in ['⛔', '⛔️', 'Нельзя делать']):
                current_section = 'cant_do'
                updated_lines.append(line)
                continue
            elif 'мудрость' in clean_line.lower() and 'дня' in clean_line.lower():
                current_section = None
                
                total_done = 0
                total_tasks = 0
                
                for section in ['morning', 'day', 'evening']:
                    if len(tasks[section]) > 0:
                        total_done += len(completed.get(section, []))
                        total_tasks += len(tasks[section])
                
                if total_tasks > 0:
                    total_perc = int((total_done / total_tasks * 100))
                    total_bar = self.get_progress_bar(total_perc, length=10)
                    updated_lines.append(f"🎯 <b>Общий прогресс:</b> {total_bar} {total_done}/{total_tasks} ({total_perc}%)")
                
                updated_lines.append("")
                updated_lines.append(line)
                continue
            
            # Обрабатываем задачи
            if current_section and line.startswith('•'):
                idx = task_counters[current_section]
                is_done = idx in completed.get(current_section, [])
                
                # Получаем чистый текст задачи (без • и звёздочек)
                task_text = line[1:].strip()  # Убираем •
                task_text = task_text.replace('⭐ ', '').replace(' ⭐', '').replace('⭐', '')  # Убираем ВСЕ звёздочки
                task_text = task_text.replace('☆ ', '').replace(' ☆', '').replace('☆', '')  # И пустые тоже
                task_text = task_text.strip()  # Убираем лишние пробелы
                
                if is_done:
                    # Только жёлтая звёздочка, без •
                    updated_lines.append(f"⭐ {task_text}")
                else:
                    # Пустая звёздочка, без •
                    updated_lines.append(f"☆ {task_text}")
                
                task_counters[current_section] += 1
            else:
                updated_lines.append(line)
        
        return '\n'.join(updated_lines)
    
    def load_stats(self):
        """Загружает статистику из файла"""
        try:
            if os.path.exists(self.stats_file):
                with open(self.stats_file, 'r', encoding='utf-8') as f:
                    content = f.read()
                    # Убираем _info и _format если они есть
                    data = json.loads(content)
                    # Фильтруем только реальные даты
                    stats = {k: v for k, v in data.items() if k not in ['_info', '_format'] and '-' in k}
                    return stats
            return {}
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки статистики: {e}")
            return {}
    
    def save_stats(self, stats):
        """Сохраняет статистику в файл"""
        try:
            with open(self.stats_file, 'w', encoding='utf-8') as f:
                json.dump(stats, f, ensure_ascii=False, indent=2)
            logger.info("✅ Статистика сохранена")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения статистики: {e}")
            return False
    
    def load_message_states(self):
        """Загружает состояния сообщений из файла"""
        try:
            if os.path.exists(self.message_state_file):
                with open(self.message_state_file, 'r', encoding='utf-8') as f:
                    # Преобразуем строковые ключи обратно в int
                    data = json.load(f)
                    return {int(k): v for k, v in data.items()}
            return {}
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки состояний сообщений: {e}")
            return {}
    
    def save_message_states(self):
        """Сохраняет состояния сообщений в файл"""
        try:
            # Преобразуем int ключи в строки для JSON
            data = {str(k): v for k, v in self.message_state.items()}
            with open(self.message_state_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info("✅ Состояния сообщений сохранены")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения состояний сообщений: {e}")
            return False
    
    def get_today_key(self):
        """Возвращает ключ для сегодняшнего дня"""
        return datetime.now().strftime("%Y-%m-%d")
    
    def calculate_percentage(self, completed, total):
        """Вычисляет процент выполнения"""
        if total == 0:
            return 0
        return int((len(completed) / total) * 100)
    
    def get_progress_bar(self, percentage, length=8):
        """Создаёт прогресс-бар"""
        filled = int((percentage / 100) * length)
        return '▓' * filled + '░' * (length - filled)
    
    def get_motivation(self, percentage):
        """Возвращает мотивационное сообщение"""
        if percentage >= 90:
            return "🏆 <b>Идеально! Так держать!</b>"
        elif percentage >= 70:
            return "💪 <b>Отличная работа!</b>"
        elif percentage >= 50:
            return "👍 <b>Хороший результат!</b>"
        else:
            return "💡 <b>Завтра будет ещё лучше!</b>"
    
    async def send_penalty_message(self, cant_do_count, failed_tasks):
        """НОВОЕ: Отправляет штрафное сообщение сразу после сохранения"""
        try:
            pushups = cant_do_count * 30
            penalty_msg = f"⚠️ <b>ВНИМАНИЕ: ШТРАФ!</b>\n\n"
            penalty_msg += f"Сегодня у тебя {cant_do_count} срыв{'а' if cant_do_count > 1 else ''} в НЕЛЬЗЯ:\n"
            
            for task in failed_tasks:
                # Убираем HTML теги и "НЕ" из начала
                clean_task = task.replace('<i>', '').replace('</i>', '').replace('<b>', '').replace('</b>', '')
                clean_task = clean_task.split('(')[0].strip()  # Убираем пояснения в скобках
                if clean_task.startswith('НЕ ') or clean_task.startswith('Не '):
                    clean_task = clean_task[3:].strip()
                penalty_msg += f"• {clean_task}\n"
            
            penalty_msg += f"\n<b>Завтра в утренних задачах будет:</b>\n"
            penalty_msg += f"🏋️ Отжимания {pushups} раз <i>(Штраф за {cant_do_count} срыв{'а' if cant_do_count > 1 else ''})</i>\n\n"
            penalty_msg += f"Держись крепче! 💪"
            
            await self.send_telegram_message(penalty_msg)
            logger.info(f"⚠️ Отправлено штрафное сообщение: {pushups} отжиманий")
            
        except Exception as e:
            logger.error(f"❌ Ошибка отправки штрафного сообщения: {e}")
    
    async def send_daily_summary(self):
        """ЭТАП 4: Отправляет итоги дня в 23:00"""
        stats = self.load_stats()
        today_key = self.get_today_key()
        
        if today_key not in stats:
            logger.info("📊 Нет данных за сегодня для итогов")
            return
        
        today_data = stats[today_key]
        
        # ОТЛАДКА: Логируем что приходит в today_data
        logger.info(f"📊 DEBUG today_data: {today_data}")
        logger.info(f"📊 DEBUG points={today_data.get('points')}, max_points={today_data.get('max_points')}")
        
        # Формируем сообщение
        message = f"📊 <b>ИТОГИ ДНЯ - {datetime.now().strftime('%d.%m.%Y')}</b>\n\n"
        message += "━━━━━━━━━━━━━━━━━━\n\n"
        
        # Статистика по периодам
        morning = today_data.get('morning', {})
        day = today_data.get('day', {})
        evening = today_data.get('evening', {})
        
        # ОТЛАДКА: Логируем каждую секцию
        logger.info(f"📊 DEBUG morning: completed={morning.get('completed', [])}, total={morning.get('total', 0)}")
        logger.info(f"📊 DEBUG day: completed={day.get('completed', [])}, total={day.get('total', 0)}")
        logger.info(f"📊 DEBUG evening: completed={evening.get('completed', [])}, total={evening.get('total', 0)}")
        
        if morning.get('total', 0) > 0:
            morning_done = len(morning.get('completed', []))
            morning_total = morning.get('total', 0)
            perc = int((morning_done / morning_total * 100))
            bar = self.get_progress_bar(perc)
            message += f"🌅 Утро: {bar} {morning_done}/{morning_total} ({perc}%)\n"
        
        if day.get('total', 0) > 0:
            day_done = len(day.get('completed', []))
            day_total = day.get('total', 0)
            perc = int((day_done / day_total * 100))
            bar = self.get_progress_bar(perc)
            message += f"☀️ День: {bar} {day_done}/{day_total} ({perc}%)\n"
        
        overall_done = today_data.get('points', 0)
        overall_total = today_data.get('max_points', 0)
        overall_perc = int((overall_done / overall_total * 100)) if overall_total > 0 else 0
        overall_bar = self.get_progress_bar(overall_perc)
        message += f"🎯 Общая: {overall_bar} {overall_done}/{overall_total} ({overall_perc}%)\n"
        
        message += "\n━━━━━━━━━━━━━━━━━━\n"
        message += f"🎯 <b>РЕЗУЛЬТАТ ДНЯ:</b>\n"
        message += f"💯 {overall_done}/{overall_total} задач ({overall_perc}%)\n"
        message += f"🏆 Баллы: {overall_done} из {overall_total}\n\n"
        
        # ИСПРАВЛЕНО: Убраны звёздочки, добавлена мотивация (используем актуальный overall_perc!)
        message += self.get_motivation(overall_perc)
        
        message += "\n\nЗавтра будет ещё лучше! 💪"
        
        # Отправляем
        await self.send_telegram_message(message)
        logger.info(f"📊 Итоги дня отправлены: {today_data.get('percentage', 0)}%")
    
    async def send_weekly_summary(self):
        """ЭТАП 4: Отправляет итоги недели в воскресенье 23:00"""
        stats = self.load_stats()
        
        # Получаем последние 7 дней
        today = datetime.now()
        week_data = []
        
        for i in range(6, -1, -1):
            day = today - timedelta(days=i)
            day_key = day.strftime("%Y-%m-%d")
            day_name = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'][day.weekday()]
            
            if day_key in stats:
                percentage = stats[day_key].get('percentage', 0)
                week_data.append({
                    'name': day_name,
                    'percentage': percentage,
                    'date': day.strftime('%d.%m')
                })
            else:
                week_data.append({
                    'name': day_name,
                    'percentage': 0,
                    'date': day.strftime('%d.%m')
                })
        
        # Формируем сообщение
        week_start = (today - timedelta(days=6)).strftime('%d.%m')
        week_end = today.strftime('%d.%m')
        
        message = f"📈 <b>ИТОГИ НЕДЕЛИ</b>\n"
        message += f"{week_start} - {week_end}.{today.year}\n\n"
        message += "━━━━━━━━━━━━━━━━━━\n\n"
        
        total_percentage = 0
        streak = 0
        current_streak = 0
        
        for day_data in week_data:
            perc = day_data['percentage']
            bar = self.get_progress_bar(perc)
            message += f"{day_data['name']}: {bar} {perc}%\n"
            
            total_percentage += perc
            
            # Считаем streak (дни подряд с 70%+)
            if perc >= 70:
                current_streak += 1
                streak = max(streak, current_streak)
            else:
                current_streak = 0
        
        avg_percentage = int(total_percentage / 7) if week_data else 0
        
        message += "\n━━━━━━━━━━━━━━━━━━\n"
        message += f"📊 Средний результат: {avg_percentage}%\n"
        message += f"🔥 Дней подряд 70%+: {streak}\n\n"
        
        if avg_percentage >= 80:
            message += "🏆 Отличная неделя!\nТак держать! 💪"
        elif avg_percentage >= 70:
            message += "✨ Хорошая неделя!\nПродолжай в том же духе! 💪"
        elif avg_percentage >= 60:
            message += "👍 Неплохая неделя!\nЕщё чуть-чуть! 💪"
        else:
            message += "📈 Есть над чем работать!\nСледующая неделя будет лучше! 💪"
        
        await self.send_telegram_message(message)
        logger.info(f"📊 Итоги недели отправлены: средний {avg_percentage}%")
    
    async def check_schedule(self):
        """Проверяет расписание для отправки итогов"""
        now = datetime.now()
        
        # Итоги дня в 23:00
        if now.hour == 23 and now.minute == 0:
            logger.info("⏰ Время для итогов дня")
            await self.send_daily_summary()
            
            # Итоги недели в воскресенье
            if now.weekday() == 6:  # Воскресенье
                logger.info("⏰ Время для итогов недели")
                await asyncio.sleep(60)  # Подождём минуту после итогов дня
                await self.send_weekly_summary()
    
    async def send_telegram_message(self, message):
        """Отправляет сообщение в Telegram"""
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as response:
                    if response.status == 200:
                        logger.info("✅ Сообщение отправлено")
                        return True
                    else:
                        logger.error(f"❌ Ошибка отправки: {response.status}")
                        return False
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return False
    
    async def edit_message(self, message_id, text, reply_markup=None):
        """Редактирует сообщение"""
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/editMessageText"
            payload = {
                'chat_id': self.chat_id,
                'message_id': message_id,
                'text': text,
                'parse_mode': 'HTML'
            }
            
            if reply_markup:
                payload['reply_markup'] = reply_markup
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as response:
                    if response.status == 200:
                        logger.info("✅ Сообщение обновлено")
                        return True
                    else:
                        error_text = await response.text()
                        logger.error(f"❌ Ошибка обновления: {response.status} - {error_text}")
                        return False
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return False
    
    async def answer_callback_query(self, callback_query_id, text=None):
        """Отвечает на callback query"""
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/answerCallbackQuery"
            payload = {'callback_query_id': callback_query_id}
            
            if text:
                payload['text'] = text
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as response:
                    return response.status == 200
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return False
    
    async def process_callback(self, callback_data, callback_query_id, message_id, message_text):
        """Обрабатывает callback от кнопок"""
        logger.info(f"📞 Получен callback: {callback_data}")
        
        if callback_data == 'update_progress':
            # Показываем чек-лист
            await self.show_checklist(message_id, message_text)
            await self.answer_callback_query(callback_query_id, "Отметь выполненные задачи ✅")
        
        elif callback_data.startswith('toggle_'):
            # Переключаем задачу
            # Формат: toggle_day_0, toggle_evening_5, toggle_cant_do_1
            if '_cant_do_' in callback_data:
                # Обрабатываем cant_do отдельно (два подчёркивания)
                task_idx = int(callback_data.split('_')[-1])
                period = 'cant_do'
            else:
                # Обычный формат: toggle_day_0
                parts = callback_data.split('_')
                period = parts[1]  # day/evening
                task_idx = int(parts[2])
            
            await self.toggle_task(message_id, period, task_idx)
            await self.answer_callback_query(callback_query_id)
        
        elif callback_data == 'save_progress':
            # Сохраняем прогресс
            await self.save_progress(message_id)
            await self.answer_callback_query(callback_query_id, "✅ Прогресс сохранён!")
        
        elif callback_data == 'cancel_update':
            # Отменяем обновление
            await self.cancel_update(message_id)
            await self.answer_callback_query(callback_query_id, "❌ Отменено")
        
        elif callback_data == 'header':
            # Заголовки не кликабельны
            await self.answer_callback_query(callback_query_id)
    
    async def show_checklist(self, message_id, original_message):
        """Показывает чек-лист для отметки задач"""
        
        # Если состояние уже существует, используем сохранённый оригинал
        if message_id in self.message_state:
            # Используем уже сохранённые данные
            state = self.message_state[message_id]
            text = self.format_checklist_message(state['tasks'], state['completed'])
            keyboard = self.create_checklist_keyboard(state['tasks'], state['completed'])
            await self.edit_message(message_id, text, keyboard)
            return
        
        # Первый вызов - парсим задачи из оригинального сообщения
        tasks = self.parse_tasks(original_message)
        
        # ПРОВЕРКА: если задач нет - сообщение слишком старое или повреждено
        total_tasks = len(tasks['morning']) + len(tasks['day']) + len(tasks['cant_do']) + len(tasks['evening'])
        if total_tasks == 0:
            error_text = (
                "⚠️ <b>Ошибка:</b> Не удалось загрузить задачи.\n\n"
                "Это сообщение слишком старое (>2 часов) или повреждено.\n"
                "Подожди следующего утреннего/вечернего сообщения."
            )
            keyboard = {
                'inline_keyboard': [
                    [{'text': '❌ Закрыть', 'callback_data': 'cancel_update'}]
                ]
            }
            await self.edit_message(message_id, error_text, keyboard)
            return
        
        # Загружаем существующий прогресс за сегодня
        today_key = self.get_today_key()
        stats = self.load_stats()
        
        # Проверяем есть ли уже данные за сегодня
        if today_key in stats:
            # Загружаем существующие выполненные задачи
            existing = stats[today_key]
            completed = {
                'morning': existing.get('morning', {}).get('completed', []),
                'day': existing.get('day', {}).get('completed', []),
                'cant_do': existing.get('cant_do', {}).get('completed', []),
                'evening': existing.get('evening', {}).get('completed', [])
            }
            logger.info(f"📊 Загружен существующий прогресс за {today_key}")
        else:
            # Новый день, начинаем с нуля
            completed = {'morning': [], 'day': [], 'cant_do': [], 'evening': []}
        
        # Сохраняем состояние
        self.message_state[message_id] = {
            'tasks': tasks,
            'completed': completed,
            'original_text': original_message,  # Сохраняем ЧИСТЫЙ оригинал
            'clean_original': original_message  # Дублируем для безопасности
        }
        
        # Сохраняем в файл
        self.save_message_states()
        
        # Формируем сообщение и клавиатуру
        text = self.format_checklist_message(tasks, completed)
        keyboard = self.create_checklist_keyboard(tasks, completed)
        
        await self.edit_message(message_id, text, keyboard)
    
    async def toggle_task(self, message_id, period, task_idx):
        """Переключает статус задачи"""
        if message_id not in self.message_state:
            logger.error(f"❌ Состояние для сообщения {message_id} не найдено")
            return
        
        state = self.message_state[message_id]
        completed = state['completed'][period]
        
        # Переключаем
        if task_idx in completed:
            completed.remove(task_idx)
            logger.info(f"☐ Задача {period}[{task_idx}] снята")
        else:
            completed.append(task_idx)
            logger.info(f"☑ Задача {period}[{task_idx}] отмечена")
        
        # Сохраняем в файл
        self.save_message_states()
        
        # Обновляем сообщение
        text = self.format_checklist_message(state['tasks'], state['completed'])
        keyboard = self.create_checklist_keyboard(state['tasks'], state['completed'])
        await self.edit_message(message_id, text, keyboard)
    
    async def save_progress(self, message_id):
        """Сохраняет прогресс в stats.json"""
        if message_id not in self.message_state:
            logger.error(f"❌ Состояние для сообщения {message_id} не найдено")
            return
        
        state = self.message_state[message_id]
        today_key = self.get_today_key()
        
        # Загружаем статистику
        stats = self.load_stats()
        
        # ВАЖНО: Объединяем с существующими данными за сегодня!
        if today_key in stats:
            # Уже есть данные за сегодня - объединяем
            existing = stats[today_key]
            
            # Объединяем выполненные задачи (убираем дубликаты)
            for period in ['morning', 'day', 'cant_do', 'evening']:
                existing_completed = set(existing.get(period, {}).get('completed', []))
                new_completed = set(state['completed'][period])
                # Объединяем множества
                combined_completed = list(existing_completed | new_completed)
                
                # Обновляем
                state['completed'][period] = combined_completed
                
            logger.info(f"📊 Объединены данные за {today_key}")
        
        # Считаем общие показатели
        total_completed = (
            len(state['completed']['morning']) +
            len(state['completed']['day']) +
            len(state['completed']['evening'])
        )
        total_tasks = (
            len(state['tasks']['morning']) +
            len(state['tasks']['day']) +
            len(state['tasks']['evening'])
        )
        
        percentage = int((total_completed / total_tasks * 100)) if total_tasks > 0 else 0
        
        stats[today_key] = {
            'morning': {
                'completed': state['completed']['morning'],
                'total': len(state['tasks']['morning'])
            },
            'day': {
                'completed': state['completed']['day'],
                'total': len(state['tasks']['day'])
            },
            'cant_do': {
                'completed': state['completed']['cant_do'],
                'total': len(state['tasks']['cant_do'])
            },
            'evening': {
                'completed': state['completed']['evening'],
                'total': len(state['tasks']['evening'])
            },
            'percentage': percentage,
            'points': total_completed,
            'max_points': total_tasks,
            'penalty': len(state['completed']['cant_do']) > 0
        }
        
        # Сохраняем в файл
        save_success = self.save_stats(stats)
        logger.info(f"💾 Save stats result: {save_success}")
        
        if save_success:
            # НОВОЕ: Отправляем штрафное сообщение если есть срывы
            cant_do_count = len(state['completed']['cant_do'])
            if cant_do_count > 0:
                # Получаем названия задач НЕЛЬЗЯ
                cant_do_tasks = state['tasks']['cant_do']
                failed_tasks = [cant_do_tasks[i] for i in state['completed']['cant_do']]
                
                # Отправляем штрафное сообщение
                await self.send_penalty_message(cant_do_count, failed_tasks)
            
            # ЭТАП 3: Обновляем исходное сообщение с прогресс-барами
            # ВАЖНО: используем clean_original, а НЕ original_text!
            clean_text = state.get('clean_original', state['original_text'])
            
            # КРИТИЧНО: парсим задачи ИЗ ТЕКУЩЕГО СООБЩЕНИЯ (не из state!)
            # Потому что вечернее сообщение содержит только вечерние задачи
            current_tasks = self.parse_tasks(clean_text)
            
            updated_text = self.update_original_message_with_progress(
                clean_text,
                current_tasks,  # Используем текущие, а не state['tasks']
                state['completed']
            )
            
            # Создаём клавиатуру с ОБЕИМИ кнопками
            keyboard = {
                'inline_keyboard': [
                    [{'text': '🔄 Обновить прогресс', 'callback_data': 'update_progress'}],
                    [{'text': '🙏 Утренняя молитва', 'url': 'https://brkme.github.io/OK_My_Day_Shedule/prayer.html'}],
                    [{'text': '🏢 Принципы карьеры', 'url': 'https://brkme.github.io/OK_My_Day_Shedule/career.html'}]
                ]
            }
            
            await self.edit_message(message_id, updated_text, keyboard)
            
            # НЕ перезаписываем clean_original - он остаётся чистым!
            # Обновляем только original_text для отображения
            self.message_state[message_id]['original_text'] = updated_text
            
            # Сохраняем в файл
            self.save_message_states()
            
            # Логируем (без отправки нового сообщения)
            logger.info(f"💾 Прогресс сохранён: {percentage}%")
    
    async def cancel_update(self, message_id):
        """Отменяет обновление, возвращает исходное сообщение"""
        if message_id in self.message_state:
            original_text = self.message_state[message_id]['original_text']
            
            # Создаём клавиатуру с ОБЕИМИ кнопками
            keyboard = {
                'inline_keyboard': [
                    [{'text': '🔄 Обновить прогресс', 'callback_data': 'update_progress'}],
                    [{'text': '🙏 Утренняя молитва', 'url': 'https://brkme.github.io/OK_My_Day_Shedule/prayer.html'}],
                    [{'text': '🏢 Принципы карьеры', 'url': 'https://brkme.github.io/OK_My_Day_Shedule/career.html'}]
                ]
            }
            
            await self.edit_message(message_id, original_text, keyboard)
            
            # При отмене - очищаем состояние
            if message_id in self.message_state:
                del self.message_state[message_id]
                # Сохраняем в файл
                self.save_message_states()
    
    async def get_updates(self):
        """Получает обновления от Telegram (long polling)"""
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/getUpdates"
            params = {
                'offset': self.last_update_id + 1,
                'timeout': 30
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=40) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get('result', [])
                    return []
        except Exception as e:
            logger.error(f"❌ Ошибка получения обновлений: {e}")
            return []
    
    async def health_check(self, request):
        """HTTP endpoint для Railway health check"""
        return web.Response(text="OK", status=200)
    
    async def webhook_handler(self, request):
        """Обработчик webhook от Telegram"""
        try:
            update = await request.json()
            
            # ЛОГИРУЕМ ВСЕ WEBHOOK ДЛЯ ОТЛАДКИ
            logger.info(f"🔔 Webhook получен: {list(update.keys())}")
            
            # Обрабатываем обычное сообщение или channel_post
            message = update.get('message') or update.get('channel_post')
            
            if message:
                chat_id = str(message.get('chat', {}).get('id', ''))
                chat_title = message.get('chat', {}).get('title', 'Private')
                chat_type = message.get('chat', {}).get('type', 'unknown')
                
                logger.info(f"📩 Сообщение из чата: ID={chat_id}, Title={chat_title}, Type={chat_type}")
                logger.info(f"🔑 Ожидаемый chat_id: {self.chat_id}")
                
                # Проверяем что это наш чат
                if chat_id == self.chat_id and 'text' in message:
                    message_text = message['text']
                    
                    logger.info(f"✅ Chat ID совпал! Проверяю текст...")
                    
                    # Проверяем что в сообщении есть задачи
                    if any(keyword in message_text for keyword in ['☀️', '📋', '⛔', '🌙', 'Дневн', 'Нельзя', 'Вечерн']):
                        logger.info("📨 Получено сообщение с задачами")
                        
                        # Парсим задачи
                        tasks = self.parse_tasks(message_text)
                        
                        # Создаём клавиатуру
                        keyboard = self.create_checklist_keyboard(tasks, {})
                        
                        # Формируем текст
                        response_text = self.format_checklist_message(tasks, {})
                        
                        # Отправляем ответ с кнопками
                        await self.send_message(response_text, keyboard)
                    else:
                        logger.warning(f"⚠️ Нет ключевых слов в сообщении: {message_text[:50]}...")
                else:
                    logger.warning(f"⚠️ Чат не совпадает или нет текста. chat_id={chat_id}, expected={self.chat_id}, has_text={'text' in message}")
            
            # Обрабатываем callback_query
            elif 'callback_query' in update:
                callback_query = update['callback_query']
                callback_data = callback_query.get('data', '')
                callback_query_id = callback_query.get('id', '')
                message = callback_query.get('message', {})
                message_id = message.get('message_id', 0)
                message_text = message.get('text', '')
                
                logger.info(f"📞 Получен callback: {callback_data}")
                await self.process_callback(callback_data, callback_query_id, message_id, message_text)
            
            return web.Response(text='OK')
        except Exception as e:
            logger.error(f"❌ Ошибка webhook: {e}", exc_info=True)
            return web.Response(status=500)
    
    async def run(self):
        """Основной цикл бота"""
        logger.info("🤖 Tracker Bot запущен!")
        logger.info("📊 Слушаю обновления...")
        
        # Запускаем HTTP сервер для Railway
        app = web.Application()
        app.router.add_get('/', self.health_check)
        app.router.add_get('/health', self.health_check)
        app.router.add_post('/webhook', self.webhook_handler)  # ← WEBHOOK!
        
        port = int(os.environ.get('PORT', 8080))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logger.info(f"🌐 HTTP сервер запущен на порту {port}")
        
        # Устанавливаем webhook
        railway_domain = os.environ.get('RAILWAY_PUBLIC_DOMAIN')
        if railway_domain:
            webhook_url = f"https://{railway_domain}/webhook"
            async with aiohttp.ClientSession() as session:
                url = f"https://api.telegram.org/bot{self.telegram_token}/setWebhook"
                payload = {'url': webhook_url}
                async with session.post(url, json=payload) as response:
                    result = await response.json()
                    if result.get('ok'):
                        logger.info(f"✅ Webhook установлен: {webhook_url}")
                    else:
                        logger.error(f"❌ Ошибка webhook: {result}")
        
        last_schedule_check = datetime.now()
        
        # Основной цикл - только для проверки расписания
        while True:
            try:
                # Проверяем расписание каждую минуту
                now = datetime.now()
                if (now - last_schedule_check).seconds >= 60:
                    await self.check_schedule()
                    last_schedule_check = now
                
                await asyncio.sleep(60)  # Спим минуту
                
            except Exception as e:
                logger.error(f"❌ Ошибка в главном цикле: {e}")
                await asyncio.sleep(5)

if __name__ == "__main__":
    bot = TaskTrackerBot()
    asyncio.run(bot.run())
