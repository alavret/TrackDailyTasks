#!/usr/bin/env python3
"""
Скрипт для отслеживания задач и автоматизации управления пользователями Yandex 360.

Режимы запуска:
    --auto              Бесконечный цикл с периодическими проверками по расписанию из track_cron.txt
    --run <modules>     Выполнение указанных модулей через запятую (license,delete_users)

Примеры:
    python track_tasks.py --auto
    python track_tasks.py --run license
    python track_tasks.py --run license,delete_users

Модули:
    Модули располагаются в том же каталоге и имеют имена вида module_<name>.py
    Каждый модуль должен иметь функцию run(settings), которая выполняет проверку.
    
Формат track_cron.txt (cron):
    Поля: минуты часы день_месяца месяц день_недели модули
    
    Примеры:
        0 0/3 ? * MON-FRI delete_users,license
        15 10 * * ? license
        0 9 ? * ПН-ПТ report
    
    Поддерживаемые модификаторы: * ? - , / L W #
    Дни недели: MON-SUN или ПН-ВС (русские аббревиатуры)
"""

import os
import sys
import argparse
import importlib
import time
import calendar
import threading
from datetime import datetime
from typing import List, Dict, Set, Tuple
from dotenv import load_dotenv

from common import (
    logger,
    get_settings,
    reset_module_run_status,
    set_module_run_status,
    save_run_status_to_csv,
)


# Интервал перечитывания файла расписания track_cron.txt (в минутах)
CRON_REFRESH = 5


class ModuleLockManager:
    """
    Менеджер блокировок для предотвращения одновременного запуска одного модуля.
    
    Обеспечивает, что каждый модуль может выполняться только в одном экземпляре.
    Если модуль уже запущен, повторный запуск будет пропущен.
    """
    
    def __init__(self):
        self._locks: Dict[str, threading.Lock] = {}
        self._running: Dict[str, bool] = {}
        self._manager_lock = threading.Lock()
    
    def _get_lock(self, module_name: str) -> threading.Lock:
        """Получает или создаёт блокировку для модуля."""
        with self._manager_lock:
            if module_name not in self._locks:
                self._locks[module_name] = threading.Lock()
                self._running[module_name] = False
            return self._locks[module_name]
    
    def try_acquire(self, module_name: str) -> bool:
        """
        Пытается захватить блокировку для модуля.
        
        Args:
            module_name: Имя модуля
            
        Returns:
            bool: True если блокировка успешно захвачена, False если модуль уже запущен
        """
        lock = self._get_lock(module_name)
        acquired = lock.acquire(blocking=False)
        if acquired:
            with self._manager_lock:
                self._running[module_name] = True
        return acquired
    
    def release(self, module_name: str):
        """
        Освобождает блокировку модуля.
        
        Args:
            module_name: Имя модуля
        """
        lock = self._get_lock(module_name)
        with self._manager_lock:
            self._running[module_name] = False
        try:
            lock.release()
        except RuntimeError:
            # Блокировка уже была освобождена
            pass
    
    def is_running(self, module_name: str) -> bool:
        """
        Проверяет, запущен ли модуль в данный момент.
        
        Args:
            module_name: Имя модуля
            
        Returns:
            bool: True если модуль запущен
        """
        with self._manager_lock:
            return self._running.get(module_name, False)
    
    def get_running_modules(self) -> List[str]:
        """
        Возвращает список запущенных модулей.
        
        Returns:
            List[str]: Список имён запущенных модулей
        """
        with self._manager_lock:
            return [name for name, running in self._running.items() if running]


# Глобальный менеджер блокировок модулей
module_lock_manager = ModuleLockManager()


# Mapping of day names to numbers (Sunday = 0, Monday = 1, ..., Saturday = 6)
# Note: Python's weekday() uses Monday=0, Sunday=6, so we need to convert
DAY_NAMES_TO_NUM = {
    'SUN': 0, 'MON': 1, 'TUE': 2, 'WED': 3, 'THU': 4, 'FRI': 5, 'SAT': 6,
    'SUNDAY': 0, 'MONDAY': 1, 'TUESDAY': 2, 'WEDNESDAY': 3, 
    'THURSDAY': 4, 'FRIDAY': 5, 'SATURDAY': 6,
    # Russian abbreviations
    'ПН': 1, 'ПОН': 1, 'ПОНЕДЕЛЬНИК': 1,
    'ВТ': 2, 'ВТО': 2, 'ВТОРНИК': 2,
    'СР': 3, 'СРЕ': 3, 'СРЕДА': 3,
    'ЧТ': 4, 'ЧЕТ': 4, 'ЧЕТВЕРГ': 4,
    'ПТ': 5, 'ПЯТ': 5, 'ПЯТНИЦА': 5,
    'СБ': 6, 'СУБ': 6, 'СУББОТА': 6,
    'ВС': 0, 'ВОС': 0, 'ВОСКРЕСЕНЬЕ': 0,
}

MONTH_NAMES_TO_NUM = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
    'JANUARY': 1, 'FEBRUARY': 2, 'MARCH': 3, 'APRIL': 4, 'JUNE': 6,
    'JULY': 7, 'AUGUST': 8, 'SEPTEMBER': 9, 'OCTOBER': 10, 'NOVEMBER': 11, 'DECEMBER': 12,
}


def replace_names_with_numbers(field: str, names_map: dict) -> str:
    """Заменяет имена (дней недели, месяцев) на числа в поле cron."""
    result = field.upper()
    # Sort by length descending to replace longer names first
    for name in sorted(names_map.keys(), key=len, reverse=True):
        result = result.replace(name, str(names_map[name]))
    return result


def parse_cron_field(field: str, min_val: int, max_val: int, names_map: dict = None) -> Set[int]:
    """
    Парсит поле cron и возвращает множество допустимых значений.
    
    Поддерживает:
        * (все значения)
        ? (любое значение, используется для day-of-month/day-of-week)
        - (диапазоны: 1-5)
        , (списки: 1,3,5)
        / (шаг: */5 или 0/15)
    """
    if names_map:
        field = replace_names_with_numbers(field, names_map)
    
    field = field.upper()
    
    # * и ? означают все допустимые значения
    if field in ('*', '?'):
        return set(range(min_val, max_val + 1))
    
    values = set()
    
    # Разбиваем по запятой для обработки списков
    for part in field.split(','):
        part = part.strip()
        
        if not part:
            continue
        
        # Обработка шага (/)
        if '/' in part:
            base, step_str = part.split('/', 1)
            step = int(step_str)
            
            if base in ('*', '?'):
                start = min_val
                end = max_val
            elif '-' in base:
                range_parts = base.split('-', 1)
                start = int(range_parts[0])
                end = int(range_parts[1])
            else:
                start = int(base) if base else min_val
                end = max_val
            
            for v in range(start, end + 1, step):
                if min_val <= v <= max_val:
                    values.add(v)
        
        # Обработка диапазона (-)
        elif '-' in part:
            range_parts = part.split('-', 1)
            start = int(range_parts[0])
            end = int(range_parts[1])
            for v in range(start, end + 1):
                if min_val <= v <= max_val:
                    values.add(v)
        
        # Одиночное значение
        else:
            try:
                v = int(part)
                if min_val <= v <= max_val:
                    values.add(v)
            except ValueError:
                pass
    
    return values if values else set(range(min_val, max_val + 1))


def parse_cron_field_day_of_month(field: str, dt: datetime) -> bool:
    """
    Проверяет соответствие дня месяца полю cron.
    
    Поддерживает специальные модификаторы:
        L   - последний день месяца
        W   - ближайший рабочий день
        L-n - n-й день с конца месяца
        LW  - последний рабочий день месяца
    """
    field = field.upper()
    
    if field in ('*', '?'):
        return True
    
    day = dt.day
    year = dt.year
    month = dt.month
    last_day = calendar.monthrange(year, month)[1]
    
    # Обработка модификатора L
    if 'L' in field:
        if field == 'L':
            return day == last_day
        elif field == 'LW':
            # Последний рабочий день месяца
            last_weekday = last_day
            last_date_weekday = calendar.weekday(year, month, last_day)
            if last_date_weekday == 5:  # Суббота
                last_weekday = last_day - 1
            elif last_date_weekday == 6:  # Воскресенье
                last_weekday = last_day - 2
            return day == last_weekday
        elif field.startswith('L-'):
            # L-n: n-й день с конца
            offset = int(field[2:])
            return day == last_day - offset
    
    # Обработка модификатора W (ближайший рабочий день)
    if 'W' in field:
        target_day = int(field.replace('W', ''))
        target_weekday = calendar.weekday(year, month, target_day)
        
        if target_weekday == 5:  # Суббота -> пятница
            actual_day = target_day - 1
        elif target_weekday == 6:  # Воскресенье -> понедельник
            actual_day = target_day + 1
        else:
            actual_day = target_day
        
        # Не выходить за пределы месяца
        if actual_day < 1:
            actual_day = 3  # Если 1-е - суббота, переходим на понедельник 3-е
        elif actual_day > last_day:
            actual_day = last_day - 2  # Если последний - воскресенье
        
        return day == actual_day
    
    # Обычное поле
    valid_days = parse_cron_field(field, 1, 31)
    return day in valid_days


def parse_cron_field_day_of_week(field: str, dt: datetime) -> bool:
    """
    Проверяет соответствие дня недели полю cron.
    
    Поддерживает:
        L   - последний день недели в месяце (например, 6L - последняя суббота)
        #   - N-й день недели в месяце (например, 6#3 - третья суббота)
    
    Note: В cron воскресенье = 0 или 7, в Python weekday() понедельник = 0
    """
    field = replace_names_with_numbers(field.upper(), DAY_NAMES_TO_NUM)
    
    if field in ('*', '?'):
        return True
    
    # Преобразование из Python weekday (Mon=0) в cron (Sun=0)
    python_weekday = dt.weekday()  # Monday = 0, Sunday = 6
    cron_weekday = (python_weekday + 1) % 7  # Sunday = 0, Monday = 1, ..., Saturday = 6
    
    year = dt.year
    month = dt.month
    day = dt.day
    
    # Обработка модификатора # (N-й день недели в месяце)
    if '#' in field:
        dow_str, nth_str = field.split('#', 1)
        target_dow = int(dow_str)
        nth = int(nth_str)
        
        if cron_weekday != target_dow:
            return False
        
        # Считаем, какой по счёту это день недели в месяце
        count = 0
        for d in range(1, day + 1):
            if (calendar.weekday(year, month, d) + 1) % 7 == target_dow:
                count += 1
        
        return count == nth
    
    # Обработка модификатора L (последний день недели в месяце)
    if 'L' in field:
        target_dow = int(field.replace('L', ''))
        
        if cron_weekday != target_dow:
            return False
        
        # Проверяем, что это последний такой день недели в месяце
        last_day = calendar.monthrange(year, month)[1]
        for d in range(day + 1, last_day + 1):
            if (calendar.weekday(year, month, d) + 1) % 7 == target_dow:
                return False
        return True
    
    # Обычное поле
    valid_days = parse_cron_field(field, 0, 7, DAY_NAMES_TO_NUM)
    # 7 также означает воскресенье
    if 7 in valid_days:
        valid_days.add(0)
    
    return cron_weekday in valid_days


def matches_cron_expression(dt: datetime, cron_expr: str) -> bool:
    """
    Проверяет, соответствует ли datetime выражению cron.
    
    Формат cron:
        минуты часы день_месяца месяц день_недели
    
    Args:
        dt: Дата и время для проверки
        cron_expr: Выражение cron (5 полей)
    
    Returns:
        bool: True если время соответствует выражению
    """
    parts = cron_expr.strip().split()
    
    if len(parts) == 5:
        minutes, hours, day_of_month, month, day_of_week = parts
    else:
        logger.warning(f"Некорректный формат cron: {cron_expr}")
        return False
    
    # Проверка минут
    if dt.minute not in parse_cron_field(minutes, 0, 59):
        return False
    
    # Проверка часов
    if dt.hour not in parse_cron_field(hours, 0, 23):
        return False
    
    # Проверка месяца
    if dt.month not in parse_cron_field(month, 1, 12, MONTH_NAMES_TO_NUM):
        return False
    
    # Проверка дня: особая логика для day_of_month и day_of_week
    # Если оба указаны (не ? и не *), то должно совпадать хотя бы одно
    # Если только один указан, проверяем только его
    dom_is_any = day_of_month.upper() in ('*', '?')
    dow_is_any = day_of_week.upper() in ('*', '?')
    
    if dom_is_any and dow_is_any:
        return True
    elif dom_is_any:
        return parse_cron_field_day_of_week(day_of_week, dt)
    elif dow_is_any:
        return parse_cron_field_day_of_month(day_of_month, dt)
    else:
        # Оба указаны - достаточно совпадения одного из них
        return (parse_cron_field_day_of_month(day_of_month, dt) or 
                parse_cron_field_day_of_week(day_of_week, dt))


def parse_cron_file(filepath: str) -> List[Tuple[str, List[str]]]:
    """
    Парсит файл расписания cron.
    
    Формат файла:
        <cron_expression> <modules>
        
    Где modules - список модулей через запятую.
    
    Примеры:
        0 0/3 ? * MON-FRI delete_users,license
        15 10 * * ? license
    
    Returns:
        List[Tuple[str, List[str]]]: Список кортежей (cron_expression, [modules])
    """
    entries = []
    
    if not os.path.exists(filepath):
        logger.warning(f"Файл расписания не найден: {filepath}")
        return entries
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                
                # Пропускаем пустые строки и комментарии
                if not line or line.startswith('#'):
                    continue
                
                parts = line.split()
                
                # Формат: 5 полей cron + модули (ровно 6 частей)
                if len(parts) != 6:
                    logger.warning(f"Строка {line_num}: некорректный формат (ожидается 6 полей) - {line}")
                    continue
                
                # 5 полей cron + модули
                cron_expr = ' '.join(parts[:5])
                modules_str = parts[5]
                
                # Парсим модули (через запятую)
                modules = [m.strip().lower() for m in modules_str.split(',') if m.strip()]
                
                if modules:
                    entries.append((cron_expr, modules))
                    logger.debug(f"Загружено расписание: {cron_expr} -> {modules}")
                else:
                    logger.warning(f"Строка {line_num}: не указаны модули - {line}")
    
    except Exception as e:
        logger.error(f"Ошибка чтения файла расписания {filepath}: {e}")
    
    return entries


def get_modules_to_run_now(cron_entries: List[Tuple[str, List[str]]], dt: datetime) -> Set[str]:
    """
    Возвращает множество модулей, которые должны быть запущены в указанное время.
    
    Args:
        cron_entries: Список записей cron из файла расписания
        dt: Текущее время
    
    Returns:
        Set[str]: Множество имён модулей для запуска
    """
    modules_to_run = set()
    
    for cron_expr, modules in cron_entries:
        if matches_cron_expression(dt, cron_expr):
            for module in modules:
                modules_to_run.add(module)
                logger.debug(f"Модуль {module} соответствует расписанию: {cron_expr}")
    
    return modules_to_run


def discover_available_modules() -> dict:
    """
    Обнаруживает все доступные модули в текущем каталоге.
    
    Ищет файлы с именами module_*.py и проверяет наличие функции run().
    
    Returns:
        dict: Словарь {имя_модуля: модуль}
    """
    modules = {}
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    for filename in os.listdir(script_dir):
        if filename.startswith('module_') and filename.endswith('.py'):
            module_name = filename[7:-3]  # Убираем "module_" и ".py"
            full_module_name = filename[:-3]  # Убираем только ".py"
            
            try:
                module = importlib.import_module(full_module_name)
                
                # Проверяем наличие функции run
                if hasattr(module, 'run') and callable(getattr(module, 'run')):
                    modules[module_name] = module
                    logger.debug(f"Обнаружен модуль: {module_name}")
                else:
                    logger.warning(f"Модуль {filename} не имеет функции run(). Пропуск.")
                    
            except ImportError as e:
                logger.error(f"Ошибка импорта модуля {filename}: {e}")
            except Exception as e:
                logger.error(f"Ошибка при загрузке модуля {filename}: {type(e).__name__}: {e}")
    
    return modules


def get_modules_from_settings(settings) -> list:
    """
    Получает список модулей из настроек (RUN_MODULES в .env).
    
    Args:
        settings: Объект настроек SettingParams
    
    Returns:
        list: Список имён модулей
    """
    if not settings.run_modules:
        logger.warning("RUN_MODULES не установлен в .env файле.")
        return []
    
    return settings.run_modules


def run_single_module(settings, module_name: str, module, use_locks: bool = True) -> bool:
    """
    Выполняет один модуль с обработкой блокировок.
    
    Args:
        settings: Настройки скрипта
        module_name: Имя модуля
        module: Объект модуля
        use_locks: Использовать блокировки
        
    Returns:
        bool: True если модуль выполнен успешно
    """
    # Проверяем, не запущен ли уже этот модуль
    if use_locks:
        if not module_lock_manager.try_acquire(module_name):
            logger.warning(f"Модуль '{module_name}' уже запущен. Пропуск.")
            return False
    
    try:
        logger.info(f"Запуск модуля: {module_name}")
        
        # Сброс статуса перед запуском и сохранение в CSV
        reset_module_run_status(settings, module_name)
        save_run_status_to_csv(settings, module_name)
        
        success = module.run(settings)
        if not success:
            logger.error(f"Модуль {module_name} завершился с ошибкой.")
            set_module_run_status(settings, module_name, success=False, error="Модуль вернул False")
        else:
            logger.info(f"Модуль {module_name} успешно завершён.")
            set_module_run_status(settings, module_name, success=True)
        
        # Сохранение статуса после выполнения
        save_run_status_to_csv(settings, module_name)
        return success
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        logger.error(f"Исключение при выполнении модуля {module_name}: {error_msg}")
        set_module_run_status(settings, module_name, success=False, error=error_msg)
        save_run_status_to_csv(settings, module_name)
        return False
    finally:
        # Освобождаем блокировку после завершения модуля
        if use_locks:
            module_lock_manager.release(module_name)
            logger.debug(f"Блокировка модуля '{module_name}' освобождена")


def run_module_async(settings, module_name: str, module, use_locks: bool = True) -> threading.Thread:
    """
    Запускает модуль асинхронно в отдельном потоке.
    
    Args:
        settings: Настройки скрипта
        module_name: Имя модуля
        module: Объект модуля
        use_locks: Использовать блокировки
        
    Returns:
        threading.Thread: Поток, в котором выполняется модуль
    """
    thread = threading.Thread(
        target=run_single_module,
        args=(settings, module_name, module, use_locks),
        name=f"module-{module_name}",
        daemon=True
    )
    thread.start()
    return thread


def run_modules(settings, module_names: list, available_modules: dict, 
                use_locks: bool = True, async_mode: bool = False) -> bool:
    """
    Выполняет указанные модули.
    
    Args:
        settings: Настройки скрипта
        module_names: Список имён модулей для выполнения
        available_modules: Словарь доступных модулей
        use_locks: Использовать блокировки для предотвращения 
                   одновременного запуска одного модуля (по умолчанию True)
        async_mode: Запускать модули асинхронно в отдельных потоках (по умолчанию False).
                    В асинхронном режиме функция возвращает True сразу после запуска потоков,
                    не дожидаясь их завершения.
        
    Returns:
        bool: True если все модули выполнены/запущены успешно, False при ошибках
    """
    threads = []
    all_success = True
    
    for module_name in module_names:
        module_name = module_name.strip().lower()
        
        if module_name not in available_modules:
            logger.warning(f"Модуль '{module_name}' не найден. Доступные модули: {', '.join(available_modules.keys())}")
            continue
        
        module = available_modules[module_name]
        
        if async_mode:
            # Асинхронный запуск в отдельном потоке
            # Проверяем блокировку заранее, чтобы не создавать лишние потоки
            if use_locks and module_lock_manager.is_running(module_name):
                logger.warning(f"Модуль '{module_name}' уже запущен. Пропуск.")
                continue
            
            thread = run_module_async(settings, module_name, module, use_locks)
            threads.append((module_name, thread))
        else:
            # Синхронный запуск
            success = run_single_module(settings, module_name, module, use_locks)
            if not success:
                all_success = False
    
    if async_mode and threads:
        logger.info(f"Запущено {len(threads)} модулей асинхронно: {', '.join(name for name, _ in threads)}")
    
    return all_success


def run_modules_and_wait(settings, module_names: list, available_modules: dict,
                         use_locks: bool = True, timeout: float = None) -> bool:
    """
    Запускает модули асинхронно и ожидает их завершения.
    
    Args:
        settings: Настройки скрипта
        module_names: Список имён модулей для выполнения
        available_modules: Словарь доступных модулей
        use_locks: Использовать блокировки
        timeout: Максимальное время ожидания каждого потока (None = без ограничения)
        
    Returns:
        bool: True если все модули завершились успешно
    """
    threads = []
    
    for module_name in module_names:
        module_name = module_name.strip().lower()
        
        if module_name not in available_modules:
            logger.warning(f"Модуль '{module_name}' не найден. Доступные модули: {', '.join(available_modules.keys())}")
            continue
        
        module = available_modules[module_name]
        
        if use_locks and module_lock_manager.is_running(module_name):
            logger.warning(f"Модуль '{module_name}' уже запущен. Пропуск.")
            continue
        
        thread = run_module_async(settings, module_name, module, use_locks)
        threads.append((module_name, thread))
    
    if threads:
        logger.info(f"Запущено {len(threads)} модулей: {', '.join(name for name, _ in threads)}")
        logger.info("Ожидание завершения всех модулей...")
        
        for module_name, thread in threads:
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning(f"Модуль '{module_name}' не завершился в течение таймаута")
        
        logger.info("Все модули завершены.")
    
    return True


def run_auto_mode(settings, available_modules: dict):
    """
    Запускает бесконечный цикл с проверками по расписанию из track_cron.txt.
    
    Расписание загружается из файла track_cron.txt в формате Quartz cron.
    Каждую секунду проверяется, какие модули должны быть запущены.
    Отслеживается последнее время запуска каждого модуля для предотвращения
    повторных запусков в пределах одной минуты.
    """
    logger.info("=" * 80)
    logger.info("Запуск в режиме AUTO (cron-based)")
    logger.info("=" * 80)
    
    # Путь к файлу расписания
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cron_file = os.path.join(script_dir, "track_cron.txt")
    
    # Загружаем расписание
    cron_entries = parse_cron_file(cron_file)
    
    if not cron_entries:
        logger.error(f"Не найдено записей в файле расписания: {cron_file}")
        logger.error("Создайте файл track_cron.txt с расписанием в формате:")
        logger.error("  <секунды> <минуты> <часы> <день_месяца> <месяц> <день_недели> [год] <модули>")
        logger.error("Пример:")
        logger.error("  0 0 */3 ? * MON-FRI delete_users,license")
        logger.error("  0 15 10 * * ? license")
        sys.exit(1)
    
    # Собираем все модули из расписания
    all_scheduled_modules = set()
    for _, modules in cron_entries:
        all_scheduled_modules.update(modules)
    
    logger.info(f"Загружено записей расписания: {len(cron_entries)}")
    logger.info(f"Модули в расписании: {', '.join(sorted(all_scheduled_modules))}")
    
    # Логируем расписание
    for cron_expr, modules in cron_entries:
        logger.info(f"  {cron_expr} -> {', '.join(modules)}")
    
    # Проверяем, что все модули существуют
    missing_modules = [m for m in all_scheduled_modules if m not in available_modules]
    if missing_modules:
        logger.warning(f"Следующие модули не найдены и будут пропущены: {', '.join(missing_modules)}")
    
    # Отслеживаем последнее время запуска каждого модуля (для предотвращения дублей)
    # Ключ: имя модуля, значение: время последнего запуска (с точностью до минуты)
    last_run_times: Dict[str, str] = {}
    
    # Время последнего чтения файла расписания
    last_cron_refresh = datetime.now()
    
    logger.info("Ожидание событий по расписанию...")
    logger.info("Модули запускаются асинхронно (несколько модулей могут работать одновременно)")
    logger.info(f"Файл расписания будет перечитываться каждые {CRON_REFRESH} минут")
    
    # Счётчик для периодического вывода статуса
    status_counter = 0
    STATUS_INTERVAL = 60  # Выводить статус каждые 60 секунд
    
    while True:
        now = datetime.now()
        current_minute_key = now.strftime('%Y-%m-%d %H:%M')
        
        # Проверяем, нужно ли перечитать файл расписания
        minutes_since_refresh = (now - last_cron_refresh).total_seconds() / 60
        if minutes_since_refresh >= CRON_REFRESH:
            logger.info(f"Перечитывание файла расписания {cron_file}...")
            
            new_cron_entries = parse_cron_file(cron_file)
            if new_cron_entries:
                cron_entries = new_cron_entries
                # Обновляем список модулей в расписании
                all_scheduled_modules = set()
                for _, modules in cron_entries:
                    all_scheduled_modules.update(modules)
                logger.info(f"Загружено записей расписания: {len(cron_entries)}")
                logger.info(f"Модули в расписании: {', '.join(sorted(all_scheduled_modules))}")
                # Логируем расписание
                for cron_expr, modules in cron_entries:
                    logger.info(f"  {cron_expr} -> {', '.join(modules)}")
                # Проверяем новые модули
                missing_modules = [m for m in all_scheduled_modules if m not in available_modules]
                if missing_modules:
                    logger.warning(f"Следующие модули не найдены: {', '.join(missing_modules)}")
            else:
                logger.warning("Файл расписания пуст или содержит ошибки, используется предыдущее расписание")
            last_cron_refresh = now
        
        # Периодически выводим информацию о запущенных модулях
        status_counter += 1
        if status_counter >= STATUS_INTERVAL:
            status_counter = 0
            running = module_lock_manager.get_running_modules()
            if running:
                logger.info(f"Активные модули: {', '.join(running)}")
        
        # Получаем модули, которые должны быть запущены сейчас
        modules_to_run = get_modules_to_run_now(cron_entries, now)
        
        # Фильтруем модули, которые уже были запущены в эту минуту
        modules_to_run_now = []
        for module_name in modules_to_run:
            if module_name not in available_modules:
                continue
            
            last_run = last_run_times.get(module_name)
            if last_run != current_minute_key:
                modules_to_run_now.append(module_name)
        
        if modules_to_run_now:
            logger.info(f"\n{'=' * 80}")
            logger.info(f"Запуск по расписанию - {now.strftime('%d.%m.%Y %H:%M:%S')}")
            logger.info(f"Модули: {', '.join(modules_to_run_now)}")
            logger.info(f"{'=' * 80}")
            
            try:
                # Запускаем модули асинхронно - каждый в своём потоке
                # Это позволяет нескольким модулям работать одновременно
                run_modules(settings, modules_to_run_now, available_modules, async_mode=True)
                
                # Отмечаем время запуска
                for module_name in modules_to_run_now:
                    last_run_times[module_name] = current_minute_key
                    
            except Exception as e:
                logger.error(f"Ошибка при выполнении модулей: {type(e).__name__}: {e}")
        
        # Спим 1 секунду перед следующей проверкой
        time.sleep(1)


def parse_arguments():
    """Парсит аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description='Скрипт для отслеживания задач и автоматизации управления пользователями Yandex 360.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
    python track_tasks.py --auto
    python track_tasks.py --run license
    python track_tasks.py --run license,delete_users

Модули загружаются динамически из файлов module_*.py в каталоге скрипта.
Каждый модуль должен содержать функцию run(settings).

Режим --auto:
    Расписание запуска модулей задаётся в файле track_cron.txt.
    
    Формат track_cron.txt (cron):
        <мин> <час> <день_месяца> <месяц> <день_недели> <модули>
    
    Примеры:
        0 0/3 ? * MON-FRI delete_users,license   # Каждые 3 часа Пн-Пт
        15 10 * * ? license                       # Ежедневно в 10:15
        0 9 ? * ПН-ПТ report                      # В 9:00 Пн-Пт (русские дни)
    
    Поддерживаемые модификаторы:
        *       Все значения
        ?       Любое значение (для day-of-month/day-of-week)
        -       Диапазон (например: MON-FRI, 1-15)
        ,       Список (например: MON,WED,FRI)
        /       Шаг (например: 0/15 - каждые 15 минут начиная с 0)
        L       Последний день (L - последний день месяца, 6L - последняя суббота)
        W       Ближайший рабочий день (15W - ближайший рабочий к 15-му)
        #       N-й день недели (6#3 - третья суббота месяца)
    
    Дни недели:
        SUN, MON, TUE, WED, THU, FRI, SAT (или 0-6)
        ВС, ПН, ВТ, СР, ЧТ, ПТ, СБ (русские аббревиатуры)

Параметры .env:
    OAUTH_TOKEN              - OAuth токен для доступа к API
    ORG_ID                   - ID организации
    DRY_RUN                  - Режим пробного запуска (True/False)
    LICENSES_COUNT           - Общее количество лицензий
    LICENSES_THRESHOLD       - Пороговое количество свободных лицензий для уведомления
    ALERT_EMAIL              - Email для отправки уведомлений
    DELETE_AFTER_LOCKED_DAYS - Количество дней блокировки для удаления
    WARNING_DAYS             - За сколько дней предупреждать об удалении
    DELETE_USERS             - Разрешить удаление пользователей (True/False)
    SMTP_SERVER              - SMTP сервер
    SMTP_PORT                - SMTP порт
    SMTP_LOGIN               - SMTP логин
    SMTP_PASSWORD            - SMTP пароль
    SMTP_FROM_EMAIL          - Email отправителя
    SMTP_TYPE                - Тип SMTP (ssl/starttls)
    IMAP_SERVER              - IMAP сервер для чтения подтверждений
    IMAP_PORT                - IMAP порт (по умолчанию 993)
    IMAP_LOGIN               - IMAP логин
    IMAP_PASSWORD            - IMAP пароль
    CHECK_IMAP_DAYS          - За сколько дней искать письма подтверждения (по умолчанию 7)
    CONFIRM_MESSAGE_SUBJECT  - Тема письма с подтверждением удаления
    LICENSE_WARNING_MESSAGE_SUBJECT - Тема письма для отчёта о лицензиях
        """
    )
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--auto', action='store_true', 
                       help='Запуск в бесконечном цикле с расписанием из track_cron.txt')
    group.add_argument('--run', type=str, metavar='MODULES',
                       help='Выполнить указанные модули через запятую (например: license,delete_users)')
    
    return parser.parse_args()


def main():
    """Главная функция."""
    load_dotenv()
    
    logger.info("=" * 80)
    logger.info("Запуск скрипта track_tasks.py")
    logger.info(f"Время запуска: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    logger.info("=" * 80)
    
    args = parse_arguments()
    
    # Обнаруживаем доступные модули
    available_modules = discover_available_modules()
    
    if not available_modules:
        logger.error("Не найдено ни одного модуля. Проверьте наличие файлов module_*.py в каталоге скрипта.")
        sys.exit(1)
    
    logger.info(f"Обнаружено модулей: {len(available_modules)}")
    for module_name in available_modules:
        logger.info(f"  - {module_name}")
    
    # Загружаем настройки
    settings = get_settings()
    if not settings:
        logger.error("Не удалось загрузить настройки. Завершение работы.")
        sys.exit(1)
    
    logger.info(f"DRY_RUN: {settings.dry_run}")
    logger.info(f"LICENSES_COUNT: {settings.licenses_count}")
    logger.info(f"LICENSES_THRESHOLD: {settings.licenses_threshold}")
    logger.info(f"DELETE_AFTER_LOCKED_DAYS: {settings.delete_after_locked_days}")
    logger.info(f"WARNING_DAYS: {settings.warning_days}")
    logger.info(f"DELETE_USERS: {settings.delete_users}")
    logger.info(f"ALERT_EMAIL: {settings.alert_email}")
    logger.info(f"IMAP_SERVER: {settings.imap_server if settings.imap_server else 'не задан'}")
    logger.info(f"CHECK_IMAP_DAYS: {settings.check_imap_days}")
    logger.info(f"CONFIRM_MESSAGE_SUBJECT: {settings.confirm_message_subject}")
    logger.info(f"LICENSE_WARNING_MESSAGE_SUBJECT: {settings.license_warning_message_subject}")
    logger.info(f"RUN_MODULES: {', '.join(settings.run_modules) if settings.run_modules else 'не задан'}")
    
    # Проверка модулей, которые есть в каталоге, но не указаны в RUN_MODULES
    if settings.run_modules:
        unused_modules = set(available_modules.keys()) - set(settings.run_modules)
        if unused_modules:
            logger.warning(f"Модули не указаны в RUN_MODULES и не будут запускаться: {', '.join(sorted(unused_modules))}")
    
    # Проверка RUN_MODULES для режима --auto
    if args.auto and not settings.run_modules:
        logger.error("Параметр RUN_MODULES не определён в .env файле.")
        logger.error("Для режима --auto необходимо указать список модулей в переменной RUN_MODULES.")
        logger.error("Пример: RUN_MODULES = license,delete_users")
        sys.exit(1)
    
    if args.auto:
        run_auto_mode(settings, available_modules)
    elif args.run:
        modules = [m.strip() for m in args.run.split(',')]
        logger.info(f"Запуск модулей: {', '.join(modules)}")
        success = run_modules(settings, modules, available_modules)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
