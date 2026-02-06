#!/usr/bin/env python3
"""
Общие функции и классы для модулей track_tasks.
"""

import os
import logging
import logging.handlers as handlers
import time
import re
import html
import concurrent.futures
from functools import partial
import requests
import smtplib
import imaplib
import email
import json
from datetime import datetime, timedelta
from dataclasses import dataclass
from http import HTTPStatus
from dotenv import load_dotenv
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header, decode_header

# Константы
DEFAULT_360_API_URL = "https://api360.yandex.net"
USERS_PER_PAGE_FROM_API = 1000
DELEGATE_MAILBOXES_PER_PAGE_FROM_API = 100
MAX_RETRIES = 3
RETRIES_DELAY_SEC = 2
LOG_FILE = "track_tasks.log"
SMTP_TIMEOUT = 10

# Интервал проверок в режиме --auto (в секундах)
AUTO_CHECK_INTERVAL_SEC = 3600  # 1 час

# Интервал обновления кэша пользователей (в минутах)
ALL_USERS_REFRESH_IN_MINUTES = 15

# Интервал обновления кэша делегированных почтовых ящиков (в минутах)
ALL_DELEGATE_MAILBOXES_REFRESH_IN_MINUTES = 15

DEPARTMENTS_PER_PAGE_FROM_API = 1000
ALL_DEPS_REFRESH_IN_MINUTES = 15

# Максимальное число одновременных вызовов get_forward_rules_from_api при обогащении пользователей
THREAD_COUNT = 5

# Необходимые права доступа для работы скрипта
NEEDED_PERMISSIONS = [
    "directory:read_users",
    "directory:write_users",
    "ya360_admin:mail_read_shared_mailbox_inventory",
    "ya360_admin:mail_read_user_settings",
    "directory:read_departments"
]

# Настройка логирования
logger = logging.getLogger("track_tasks")
logger.setLevel(logging.DEBUG)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s.%(msecs)03d %(levelname)s:\t%(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
file_handler = handlers.RotatingFileHandler(LOG_FILE, maxBytes=1024 * 1024 * 10, backupCount=5, encoding='utf-8')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter('%(asctime)s.%(msecs)03d %(levelname)s:\t%(message)s', datefmt='%Y-%m-%d %H:%M:%S'))

# Добавляем обработчики только если их ещё нет
if not logger.handlers:
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)


@dataclass
class SettingParams:
    """Параметры настроек скрипта."""
    oauth_token: str
    org_id: int
    dry_run: bool
    
    # Параметры SMTP
    smtp_server: str
    smtp_port: int
    smtp_login: str
    smtp_password: str
    smtp_from_email: str
    smtp_type: str
    
    # Параметры для отслеживания лицензий
    licenses_count: int
    licenses_threshold: int
    alert_emails: list  # Список email адресов для уведомлений
    
    # Параметры для удаления пользователей
    delete_after_locked_days: int
    warning_days: int
    delete_users: bool
    value_for_empty_date: datetime  # Дата по умолчанию для пользователей без даты блокировки
    
    # Параметры IMAP для подтверждения удаления
    imap_server: str
    imap_port: int
    imap_login: str
    imap_password: str
    check_imap_days: int
    confirmation_imap_folder: str  # Дополнительная папка для поиска писем подтверждения
    confirm_message_subject: str
    license_warning_message_subject: str  # Тема письма для отчёта о лицензиях (также для поиска подтверждений)
    waiting_confirmation_subject: str  # Тема письма ожидания подтверждения удаления
    approved_senders: list  # Список email адресов одобренных отправителей
    
    # Файл исключений для заблокированных пользователей
    blocked_users_exceptions_file: str
    
    # Кэш пользователей
    all_users: list
    all_users_get_timestamp: datetime

    # Кэш делегированных почтовых ящиков
    all_delegate_mailboxes: list
    all_delegate_mailboxes_get_timestamp: datetime

    all_deps: list
    all_deps_get_timestamp: datetime
    
    # Список модулей для запуска в режиме --auto
    run_modules: list = None
    
    # Статус и ошибки последнего запуска модулей
    run_status: dict = None  # {module_name: "Success" | "Error" | "Running"}
    run_error: dict = None   # {module_name: error_message или ""}
    
    def __post_init__(self):
        """Инициализация словарей и списков, если не заданы."""
        if self.run_modules is None:
            self.run_modules = []
        if self.run_status is None:
            self.run_status = {}
        if self.run_error is None:
            self.run_error = {}


def get_settings():
    """Загружает настройки из переменных окружения."""
    load_dotenv()
    
    exit_flag = False
    
    oauth_token = os.environ.get("OAUTH_TOKEN", "")
    if not oauth_token:
        logger.error("OAUTH_TOKEN не установлен.")
        exit_flag = True
    
    org_id = os.environ.get("ORG_ID", "")
    if not org_id:
        logger.error("ORG_ID не установлен.")
        exit_flag = True
    
    # Парсинг списка email адресов для уведомлений
    alert_emails_str = os.environ.get("ALERT_EMAILS", "")
    alert_emails = []
    if alert_emails_str:
        # Разделяем по запятой или пробелу
        email_pattern = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
        raw_emails = re.split(r'[,\s]+', alert_emails_str)
        for email_addr in raw_emails:
            email_addr = email_addr.strip()
            if email_addr:
                if email_pattern.match(email_addr):
                    alert_emails.append(email_addr)
                else:
                    logger.warning(f"Некорректный email адрес в ALERT_EMAILS: {email_addr}")
    if not alert_emails:
        logger.warning("ALERT_EMAILS не установлен или не содержит корректных адресов. Уведомления не будут отправляться.")
    
    licenses_count = os.environ.get("LICENSES_COUNT", "0")
    try:
        licenses_count = int(licenses_count)
    except ValueError:
        logger.error(f"LICENSES_COUNT должен быть числом, получено: {licenses_count}")
        licenses_count = 0
    
    licenses_threshold = os.environ.get("LICENSES_THRESHOLD", "5")
    try:
        licenses_threshold = int(licenses_threshold)
    except ValueError:
        logger.error(f"LICENSES_THRESHOLD должен быть числом, получено: {licenses_threshold}")
        licenses_threshold = 5
    
    delete_after_locked_days = os.environ.get("DELETE_AFTER_LOCKED_DAYS", "30")
    try:
        delete_after_locked_days = int(delete_after_locked_days)
    except ValueError:
        logger.error(f"DELETE_AFTER_LOCKED_DAYS должен быть числом, получено: {delete_after_locked_days}")
        delete_after_locked_days = 30
    
    warning_days = os.environ.get("WARNING_DAYS", "7")
    try:
        warning_days = int(warning_days)
    except ValueError:
        logger.error(f"WARNING_DAYS должен быть числом, получено: {warning_days}")
        warning_days = 7
    
    # Дата по умолчанию для пользователей без даты блокировки (формат: YYYY-MM-DD)
    value_for_empty_date_str = os.environ.get("VALUE_FOR_EMPTY_DATE", "2020-01-01")
    try:
        value_for_empty_date = datetime.strptime(value_for_empty_date_str, "%Y-%m-%d")
    except ValueError:
        logger.error(f"VALUE_FOR_EMPTY_DATE должен быть в формате YYYY-MM-DD, получено: {value_for_empty_date_str}")
        value_for_empty_date = datetime(2020, 1, 1)
    
    smtp_port = os.environ.get("SMTP_PORT", "465")
    try:
        smtp_port = int(smtp_port)
    except ValueError:
        logger.error(f"SMTP_PORT должен быть числом, получено: {smtp_port}")
        smtp_port = 465
    
    imap_port = os.environ.get("IMAP_PORT", "993")
    try:
        imap_port = int(imap_port)
    except ValueError:
        logger.error(f"IMAP_PORT должен быть числом, получено: {imap_port}")
        imap_port = 993
    
    check_imap_days = os.environ.get("CHECK_IMAP_DAYS", "7")
    try:
        check_imap_days = int(check_imap_days)
    except ValueError:
        logger.error(f"CHECK_IMAP_DAYS должен быть числом, получено: {check_imap_days}")
        check_imap_days = 7
    
    # Список модулей для запуска в режиме --auto
    run_modules_str = os.environ.get("RUN_MODULES", "")
    run_modules = [m.strip() for m in run_modules_str.split(",") if m.strip()]
    
    settings = SettingParams(
        oauth_token=oauth_token,
        org_id=int(org_id) if org_id else 0,
        dry_run=os.environ.get("DRY_RUN", "false").lower() == "true",
        smtp_server=os.environ.get("SMTP_SERVER", "smtp.yandex.ru"),
        smtp_port=smtp_port,
        smtp_login=os.environ.get("SMTP_LOGIN", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        smtp_from_email=os.environ.get("SMTP_FROM_EMAIL", ""),
        smtp_type=os.environ.get("SMTP_TYPE", "ssl"),
        licenses_count=licenses_count,
        licenses_threshold=licenses_threshold,
        alert_emails=alert_emails,
        delete_after_locked_days=delete_after_locked_days,
        warning_days=warning_days,
        delete_users=os.environ.get("DELETE_USERS", "false").lower() == "true",
        value_for_empty_date=value_for_empty_date,
        imap_server=os.environ.get("IMAP_SERVER", "imap.yandex.ru"),
        imap_port=imap_port,
        imap_login=os.environ.get("IMAP_LOGIN", ""),
        imap_password=os.environ.get("IMAP_PASSWORD", ""),
        check_imap_days=check_imap_days,
        confirmation_imap_folder=os.environ.get("CONFIRMATION_IMAP_FOLDER", "").strip(),
        confirm_message_subject=os.environ.get("CONFIRM_MESSAGE_SUBJECT", "Подтверждение удаления пользователей"),
        license_warning_message_subject=os.environ.get("LICENSE_WARNING_MESSAGE_SUBJECT", "[Yandex 360] Отчёт о лицензиях"),
        waiting_confirmation_subject=os.environ.get("WAITING_CONFIRMATION_SUBJECT", "[Yandex 360] Ожидание подтверждения"),
        approved_senders=[s.strip().lower() for s in os.environ.get("APPROVED_SENDERS", "").split(",") if s.strip()],
        blocked_users_exceptions_file=os.environ.get("BLOCKED_USERS_EXCEPTIONS_FILE", "blocked_users_exceptions.txt"),
        all_users=[],
        all_users_get_timestamp=datetime.now(),
        all_delegate_mailboxes = [],
        all_delegate_mailboxes_get_timestamp = datetime.now(),
        all_deps = [],
        all_deps_get_timestamp = datetime.now(),
        run_modules=run_modules,
    )
    
    if exit_flag:
        return None
    
    # Проверка токена и прав доступа
    hard_error, permissions_ok = check_token_permissions(settings.oauth_token, settings.org_id, NEEDED_PERMISSIONS)
    if hard_error:
        logger.error("OAUTH_TOKEN недействителен или не имеет доступа к организации.")
        return None
    
    if not permissions_ok:
        logger.warning("ВНИМАНИЕ: У токена отсутствуют некоторые права. Функциональность может быть ограничена.")
    
    return settings


def check_oauth_token(oauth_token: str, org_id: int) -> bool:
    """Проверяет, что токен OAuth действителен."""
    url = f'{DEFAULT_360_API_URL}/directory/v1/org/{org_id}/users?perPage=1'
    headers = {'Authorization': f'OAuth {oauth_token}'}
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == HTTPStatus.OK:
            return True
        logger.error(f"Ошибка проверки токена: {response.status_code} - {response.text}")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при проверке токена: {e}")
        return False


def check_token_permissions(token: str, org_id: int, needed_permissions: list) -> tuple:
    """
    Проверяет права доступа для заданного токена.
    
    Args:
        token: OAuth токен для проверки
        org_id: ID организации
        needed_permissions: Список необходимых прав доступа
        
    Returns:
        tuple: (hard_error: bool, success: bool)
            - hard_error: True если токен невалидный, продолжение работы невозможно
            - success: True если все права присутствуют и org_id совпадает
    """
    url = 'https://api360.yandex.net/whoami'
    headers = {
        'Authorization': f'OAuth {token}'
    }
    try:
        response = requests.get(url, headers=headers, timeout=30)
        
        # Проверка валидности токена
        if response.status_code != HTTPStatus.OK:
            logger.error(f"Невалидный токен. Статус код: {response.status_code}")
            if response.status_code == 401:
                logger.error("Токен недействителен или истек срок его действия.")
            else:
                logger.error(f"Ошибка при проверке токена: {response.text}")
            return True, False
        
        data = response.json()
        
        # Извлечение scopes и orgIds из ответа
        token_scopes = data.get('scopes', [])
        token_org_ids = data.get('orgIds', [])
        login = data.get('login', 'unknown')
        
        logger.info(f"Проверка прав доступа для токена пользователя: {login}")
        logger.debug(f"Доступные права: {token_scopes}")
        logger.debug(f"Доступные организации: {token_org_ids}")
        
        # Проверка наличия org_id в списке доступных организаций
        if str(org_id) not in [str(org) for org in token_org_ids]:
            logger.error("=" * 100)
            logger.error(f"ОШИБКА: Токен не имеет доступа к организации с ID {org_id}")
            logger.error(f"Доступные организации для этого токена: {token_org_ids}")
            logger.error("=" * 100)
            return True, False

        # Проверка наличия всех необходимых прав
        missing_permissions = []
        for permission in needed_permissions:
            if permission not in token_scopes:
                missing_permissions.append(permission)
        
        if missing_permissions:
            logger.error("=" * 100)
            logger.error("ОШИБКА: У токена отсутствуют необходимые права доступа!")
            logger.error("Недостающие права:")
            for perm in missing_permissions:
                logger.error(f"  - {perm}")
            logger.error("=" * 100)
            return False, False

        logger.info("✓ Все необходимые права доступа присутствуют")
        logger.info(f"✓ Доступ к организации {org_id} подтвержден")
        return False, True
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при выполнении запроса к API: {e}")
        return True, False
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка при парсинге ответа от API: {e}")
        return True, False
    except Exception as e:
        logger.error(f"Неожиданная ошибка при проверке прав доступа: {type(e).__name__}: {e}")
        return True, False


def get_all_users(settings: SettingParams, force: bool = False) -> list:
    """Получает список всех пользователей организации из API."""
    if not force:
        logger.debug("Получение всех пользователей из кэша...")
    
    # Проверяем, нужно ли обновить кэш
    cache_expired = (datetime.now() - settings.all_users_get_timestamp).total_seconds() > ALL_USERS_REFRESH_IN_MINUTES * 60
    
    if settings.all_users and not force and not cache_expired:
        logger.debug(f"Возврат {len(settings.all_users)} пользователей из кэша.")
        return settings.all_users
    
    logger.info("Получение всех пользователей организации из API...")
    url = f'{DEFAULT_360_API_URL}/directory/v1/org/{settings.org_id}/users'
    headers = {"Authorization": f"OAuth {settings.oauth_token}"}
    
    users = []
    current_page = 1
    last_page = 1
    
    while current_page <= last_page:
        params = {'page': current_page, 'perPage': USERS_PER_PAGE_FROM_API}
        
        retries = 1
        while retries <= MAX_RETRIES:
            try:
                logger.debug(f"GET URL - {url}, page {current_page}")
                response = requests.get(url, headers=headers, params=params, timeout=60)
                logger.debug(f"x-request-id: {response.headers.get('x-request-id', '')}")
                
                if response.status_code == HTTPStatus.OK:
                    data = response.json()
                    for user in data.get('users', []):
                        # Исключаем роботов и технические аккаунты
                        if not user.get('isRobot') and int(user.get("id", 0)) >= 1130000000000000:
                            users.append(user)
                    
                    logger.debug(f"Загружено {len(data.get('users', []))} пользователей. Страница {current_page}/{data.get('pages', 1)}.")
                    last_page = data.get('pages', 1)
                    current_page += 1
                    break
                else:
                    logger.error(f"Ошибка при GET запросе: {response.status_code}. Сообщение: {response.text}")
                    if retries < MAX_RETRIES:
                        logger.info(f"Повторная попытка ({retries + 1}/{MAX_RETRIES})")
                        time.sleep(RETRIES_DELAY_SEC * retries)
                        retries += 1
                    else:
                        logger.error("Достигнуто максимальное количество попыток.")
                        return []
                        
            except requests.exceptions.RequestException as e:
                logger.error(f"Ошибка запроса: {e}")
                if retries < MAX_RETRIES:
                    logger.info(f"Повторная попытка ({retries + 1}/{MAX_RETRIES})")
                    time.sleep(RETRIES_DELAY_SEC * retries)
                    retries += 1
                else:
                    logger.error("Достигнуто максимальное количество попыток.")
                    return []
    
    settings.all_users = users
    settings.all_users_get_timestamp = datetime.now()
    logger.info(f"Всего загружено {len(users)} пользователей.")
    return users

def get_all_delegated_mailboxes(settings: "SettingParams", force = False, thread_id: int = 0):
    thread_prefix = f"[THREAD #{thread_id}] " if thread_id > 0 else ""
    if not force:
        logger.info(f"{thread_prefix}Получение всех делегированных почтовых ящиков из кэша...")
    if not settings.all_delegate_mailboxes or force or (datetime.now() - settings.all_delegate_mailboxes_get_timestamp).total_seconds() > ALL_DELEGATE_MAILBOXES_REFRESH_IN_MINUTES * 60:
        settings.all_delegate_mailboxes = get_all_delegated_mailboxes_from_api(settings, per_page=DELEGATE_MAILBOXES_PER_PAGE_FROM_API, thread_id=thread_id)
        settings.all_delegate_mailboxes_get_timestamp = datetime.now()
    return settings.all_delegate_mailboxes

def get_all_delegated_mailboxes_from_api(settings: "SettingParams", per_page: int = DELEGATE_MAILBOXES_PER_PAGE_FROM_API, thread_id: int = 0):
    """
    Получает полный список всех делегированных почтовых ящиков в организации (все страницы).
    
    Args:
        settings: Объект настроек с oauth_token и organization_id
        per_page: Количество записей на одной странице ответа (по умолчанию 100)
        thread_id: Идентификатор потока для логирования
        
    Returns:
        list: Список объектов с полями resourceId и count
        None: в случае ошибки
    """
    # Формируем префикс для логов
    thread_prefix = f"[THREAD #{thread_id}] " if thread_id > 0 else ""
    
    logger.info(f"{thread_prefix}Получение полного списка всех делегированных ящиков...")
    all_resources = []
    current_page = 1
    
    while True:
        result = get_delegated_mailboxes(settings, page=current_page, per_page=per_page, thread_id=thread_id)
        
        if result is None:
            logger.error(f"{thread_prefix}Ошибка при получении делегированных ящиков. Возвращается пустой список.")
            return []
        
        resources = result.get('resources', [])
        all_resources.extend(resources)
        
        total = result.get('total', 0)
        
        logger.debug(f"{thread_prefix}Загружено {len(resources)} делегированных ящиков. Всего получено: {len(all_resources)} из {total}")
        
        # Проверяем, есть ли еще страницы
        if len(all_resources) >= total or len(resources) == 0:
            break
            
        current_page += 1
    
    logger.info(f"{thread_prefix}Всего получено {len(all_resources)} делегированных ящиков")
    return all_resources

def get_delegated_mailboxes(settings: "SettingParams", page: int = 1, per_page: int = 10, thread_id: int = 0):
    """
    Получает список делегированных почтовых ящиков в организации.
    
    Args:
        settings: Объект настроек с oauth_token и organization_id
        page: Номер страницы ответа (по умолчанию 1)
        per_page: Количество записей на одной странице ответа (по умолчанию 10)
        thread_id: Идентификатор потока для логирования
        
    Returns:
        dict: Словарь с полями:
            - resources: список объектов с resourceId и count
            - page: номер страницы
            - perPage: количество записей на странице
            - total: общее количество записей
        None: в случае ошибки
    """
    # Формируем префикс для логов
    thread_prefix = f"[THREAD #{thread_id}] " if thread_id > 0 else ""
    
    logger.info(f"{thread_prefix}Получение списка делегированных ящиков (страница {page}, записей на странице: {per_page})...")
    url = f"{DEFAULT_360_API_URL}/admin/v1/org/{settings.org_id}/mailboxes/delegated"
    headers = {"Authorization": f"OAuth {settings.oauth_token}"}
    params = {'page': page, 'perPage': per_page}
    
    try:
        retries = 1
        while True:
            logger.debug(f"{thread_prefix}GET URL - {url}")
            response = requests.get(url, headers=headers, params=params)
            logger.debug(f"{thread_prefix}x-request-id: {response.headers.get('x-request-id','')}")
            
            if response.status_code != HTTPStatus.OK.value:
                logger.error(f"{thread_prefix}!!! ОШИБКА !!! при GET запросе url - {url}: {response.status_code}. Сообщение об ошибке: {response.text}")
                if retries < MAX_RETRIES:
                    logger.error(f"{thread_prefix}Повторная попытка ({retries+1}/{MAX_RETRIES})")
                    time.sleep(RETRIES_DELAY_SEC * retries)
                    retries += 1
                else:
                    logger.error(f"{thread_prefix}Превышено максимальное количество попыток. Возвращается None.")
                    return None
            else:
                result = response.json()
                logger.info(f"{thread_prefix}Успешно получено {len(result.get('resources', []))} делегированных ящиков. " 
                           f"Страница {result.get('page', page)} из {result.get('total', 0) // result.get('perPage', per_page) + 1}")
                return result
                
    except requests.exceptions.RequestException as e:
        logger.error(f"{thread_prefix}!!! ERROR !!! {type(e).__name__} at line {e.__traceback__.tb_lineno} of {__file__}: {e}")
        return None

def get_forward_rules_from_api(settings: "SettingParams", user):
    logger.debug(f"Получаем правило пересылки для пользователя {user['id']} ({user['nickname']})...")
    url = f"{DEFAULT_360_API_URL}/admin/v1/org/{settings.org_id}/mail/users/{user['id']}/settings/user_rules"
    headers = {"Authorization": f"OAuth {settings.oauth_token}"}
    data = {}
    try:
        retries = 1
        while True:
            logger.debug(f"GET url - {url}")
            response = requests.get(url, headers=headers)
            logger.debug(f"x-request-id: {response.headers.get('x-request-id','')}")
            if response.status_code != HTTPStatus.OK.value:
                logger.error(f"ОШИБКА при запросе GET для пользователя {user['id']}: {response.status_code}. Сообщение об ошибке: {response.text}")
                if retries < MAX_RETRIES:
                    logger.error(f"Повторная попытка ({retries+1}/{MAX_RETRIES})")
                    time.sleep(RETRIES_DELAY_SEC * retries)
                    retries += 1
                else:
                    logger.error(f"ОШИБКА. Получение правил пересылки для пользователя {user['id']} ({user['nickname']}) не удалось.")
                    break
            else:
                data = response.json()
                break
    except requests.exceptions.RequestException as e:
        logger.error(f"{type(e).__name__} на строке {e.__traceback__.tb_lineno} в файле {__file__}: {e}")
        return []
    return data

def get_all_api360_departments(settings: "SettingParams", force = False, show_messages = False):
    if not force:
        if show_messages:
            logger.info("Получение всех подразделений организации из кэша...")
        else:
            logger.debug("Получение всех подразделений организации из кэша...")
    if not settings.all_deps or force or (datetime.now() - settings.all_deps_get_timestamp).total_seconds() > ALL_DEPS_REFRESH_IN_MINUTES * 60:
        settings.all_deps = get_all_api360_departments_from_api(settings)
        settings.all_deps_get_timestamp = datetime.now()
    return settings.all_deps

def get_all_api360_departments_from_api(settings: "SettingParams"):
    logger.info("Получение всех подразделений организации из API...")
    url = f'{DEFAULT_360_API_URL}/directory/v1/org/{settings.org_id}/departments'
    headers = {"Authorization": f"OAuth {settings.oauth_token}"}

    has_errors = False
    departments = []
    current_page = 1
    last_page = 1
    while current_page <= last_page:
        params = {'page': current_page, 'perPage': DEPARTMENTS_PER_PAGE_FROM_API}
        try:
            retries = 1
            while True:
                logger.debug(f"GET URL - {url}")
                response = requests.get(url, headers=headers, params=params)
                logger.debug(f"x-request-id: {response.headers.get('x-request-id','')}")
                if response.status_code != HTTPStatus.OK.value:
                    logger.error(f"!!! ОШИБКА !!! при GET запросе url - {url}: {response.status_code}. Сообщение об ошибке: {response.text}")
                    if retries < MAX_RETRIES:
                        logger.error(f"Повторная попытка ({retries+1}/{MAX_RETRIES})")
                        time.sleep(RETRIES_DELAY_SEC * retries)
                        retries += 1
                    else:
                        has_errors = True
                        break
                else:
                    for deps in response.json()['departments']:
                        departments.append(deps)
                    logger.debug(f"Загружено {len(response.json()['departments'])} подразделений. Текущая страница - {current_page} (всего {last_page} страниц).")
                    current_page += 1
                    last_page = response.json()['pages']
                    break

        except requests.exceptions.RequestException as e:
            logger.error(f"!!! ERROR !!! {type(e).__name__} at line {e.__traceback__.tb_lineno} of {__file__}: {e}")
            has_errors = True
            break

        if has_errors:
            break

    if has_errors:
        print("Есть ошибки при GET запросах. Возвращается пустой список подразделений.")
        return []
    
    return departments


def get_blocked_users(users: list) -> list:
    """Возвращает список заблокированных пользователей."""
    blocked = []
    for user in users:
        if not user.get('isEnabled', True):
            blocked.append(user)
    return blocked


def enrich_users(settings: "SettingParams", users: list) -> list:
    """
    Обогащает список пользователей дополнительной информацией.
    
    Добавляет поля:
        - isDelegated: True если почтовый ящик пользователя является делегированным
        - hasForwardingRules: True если у пользователя настроены правила пересылки
    
    Args:
        settings: Объект настроек
        users: Список пользователей для обогащения
        
    Returns:
        list: Обогащённый список пользователей
    """
    # Получаем делегированные почтовые ящики
    delegated_mailboxes = get_all_delegated_mailboxes(settings)
    delegated_uids = {str(mb.get('resourceId', '')) for mb in delegated_mailboxes}

    deps = get_all_api360_departments(settings)
    deps_dict = {dep.get('id'): dep.get('name') for dep in deps}
    
    logger.info(f"Обогащение информации для {len(users)} пользователей...")
    logger.debug(f"Делегированных ящиков: {len(delegated_uids)}")

    # Вызов get_forward_rules_from_api в пуле потоков (не более THREAD_COUNT одновременно)
    with concurrent.futures.ThreadPoolExecutor(max_workers=THREAD_COUNT) as executor:
        forward_rules_list = list(executor.map(partial(get_forward_rules_from_api, settings), users))

    for user, forward_rules in zip(users, forward_rules_list):
        user_id = str(user.get('id', ''))
        
        # Проверяем, является ли пользователь делегированным
        user['isDelegated'] = user_id in delegated_uids
        
        # Наличие правил пересылки (результат асинхронного вызова)
        user['hasForwardingRules'] = bool(forward_rules.get('forwards') if forward_rules else False)
        
        if user['isDelegated']:
            logger.debug(f"Пользователь {user.get('nickname', '')} ({user_id}) имеет делегированный ящик")
        if user['hasForwardingRules']:
            logger.debug(f"Пользователь {user.get('nickname', '')} ({user_id}) имеет правила пересылки")

        if user['departmentId'] == 1:
            user['department'] = ""
        else:
            user['department'] = deps_dict[user['departmentId']]
    
    return users


def parse_date(date_str: str) -> datetime:
    """Парсит дату из строки ISO формата."""
    if not date_str:
        return None
    try:
        # Формат: 2024-01-15T10:30:00.000Z или 2024-01-15T10:30:00Z
        date_str = date_str.replace('Z', '+00:00')
        if '.' in date_str:
            return datetime.fromisoformat(date_str.split('.')[0])
        return datetime.fromisoformat(date_str.replace('+00:00', ''))
    except ValueError as e:
        logger.warning(f"Не удалось распарсить дату: {date_str}, ошибка: {e}")
        return None


def get_user_lock_date(user: dict, default_date: datetime) -> tuple:
    """
    Получает дату блокировки пользователя.
    
    Args:
        user: Данные пользователя
        default_date: Дата по умолчанию, если дата блокировки не установлена
        
    Returns:
        tuple: (lock_date: datetime, is_unknown: bool)
            - lock_date: дата блокировки или default_date
            - is_unknown: True если дата блокировки не была установлена
    """
    lock_date = parse_date(user.get('isEnabledUpdatedAt', ''))
    if lock_date:
        return lock_date, False
    
    # Дата блокировки не установлена - используем значение по умолчанию
    return default_date, True


def sort_blocked_users_by_lock_date(blocked_users: list, default_date: datetime) -> list:
    """Сортирует заблокированных пользователей по дате блокировки (новые первыми)."""
    # Устанавливаем флаг _lock_date_unknown для каждого пользователя
    for user in blocked_users:
        _, is_unknown = get_user_lock_date(user, default_date)
        user['_lock_date_unknown'] = is_unknown
    
    def get_lock_date(user):
        date, _ = get_user_lock_date(user, default_date)
        return date
    
    return sorted(blocked_users, key=get_lock_date, reverse=True)


def get_users_near_deletion(blocked_users: list, delete_after_days: int, warning_days: int, default_date: datetime) -> list:
    """
    Возвращает список пользователей, которые скоро будут удалены.
    
    Пользователь включается в список, если:
    дата_блокировки + delete_after_days - warning_days <= текущая_дата
    """
    near_deletion = []
    today = datetime.now()
    warning_threshold = delete_after_days - warning_days
    
    for user in blocked_users:
        lock_date, is_unknown = get_user_lock_date(user, default_date)
        
        days_since_lock = (today - lock_date).days
        if days_since_lock >= warning_threshold:
            deletion_date = lock_date + timedelta(days=delete_after_days)
            user['_calculated_deletion_date'] = deletion_date
            user['_days_until_deletion'] = (deletion_date - today).days
            user['_lock_date_unknown'] = is_unknown
            near_deletion.append(user)
    
    return near_deletion


def get_users_for_deletion(blocked_users: list, delete_after_days: int, default_date: datetime) -> list:
    """
    Возвращает список пользователей для удаления.
    
    Пользователь включается в список, если:
    дата_блокировки + delete_after_days <= текущая_дата
    """
    for_deletion = []
    today = datetime.now()
    
    for user in blocked_users:
        lock_date, is_unknown = get_user_lock_date(user, default_date)
        
        days_since_lock = (today - lock_date).days
        if days_since_lock >= delete_after_days:
            user['_lock_date_unknown'] = is_unknown
            for_deletion.append(user)
    
    return for_deletion


def delete_user_by_api(settings: SettingParams, user_id: str) -> tuple:
    """
    Удаляет пользователя через API Yandex 360.
    
    Returns:
        tuple: (success: bool, response_data: dict)
    """
    url = f'{DEFAULT_360_API_URL}/directory/v1/org/{settings.org_id}/users/{user_id}'
    headers = {"Authorization": f"OAuth {settings.oauth_token}"}
    
    logger.debug(f"DELETE URL: {url}")
    
    if settings.dry_run:
        logger.info(f"DRY RUN: Пользователь {user_id} был бы удален.")
        return True, {"dry_run": True}
    
    retries = 1
    while retries <= MAX_RETRIES:
        try:
            response = requests.delete(url, headers=headers, timeout=60)
            logger.debug(f"x-request-id: {response.headers.get('x-request-id', '')}")
            
            if response.status_code in [HTTPStatus.OK, HTTPStatus.NO_CONTENT]:
                logger.info(f"Успех - пользователь {user_id} удален.")
                return True, response.json() if response.text else {}
            else:
                logger.error(f"Ошибка при удалении пользователя: {response.status_code}. Сообщение: {response.text}")
                if retries < MAX_RETRIES:
                    logger.info(f"Повторная попытка ({retries + 1}/{MAX_RETRIES})")
                    time.sleep(RETRIES_DELAY_SEC * retries)
                    retries += 1
                else:
                    logger.error(f"Ошибка. Удаление пользователя {user_id} не удалось.")
                    return False, {}
                    
        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка запроса при удалении: {e}")
            if retries < MAX_RETRIES:
                logger.info(f"Повторная попытка ({retries + 1}/{MAX_RETRIES})")
                time.sleep(RETRIES_DELAY_SEC * retries)
                retries += 1
            else:
                return False, {}
    
    return False, {}


def send_email(settings: SettingParams, to_email: str, subject: str, html_body: str, include_alert_emails: bool = True) -> bool:
    """
    Отправляет email сообщение по SMTP.
    
    Args:
        settings: Параметры настроек
        to_email: Основной адрес получателя
        subject: Тема письма
        html_body: HTML тело письма
        include_alert_emails: Если True, добавляет все адреса из settings.alert_emails в To:
    
    Returns:
        True если письмо отправлено успешно, False в противном случае
    """
    if not all([settings.smtp_server, settings.smtp_port, settings.smtp_login, settings.smtp_password]):
        logger.error("Не заданы параметры SMTP сервера в файле .env")
        return False
    
    try:
        # Формируем список получателей
        recipients = [to_email] if to_email else []
        if include_alert_emails and settings.alert_emails:
            for alert_addr in settings.alert_emails:
                if alert_addr not in recipients:
                    recipients.append(alert_addr)
        
        if not recipients:
            logger.error("Не указаны получатели для отправки email")
            return False
        
        msg = MIMEMultipart('alternative')
        msg['From'] = settings.smtp_from_email if settings.smtp_from_email else settings.smtp_login
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = Header(subject, 'utf-8')
        
        html_part = MIMEText(html_body, 'html', 'utf-8')
        msg.attach(html_part)
        
        logger.debug(f"Подключение к SMTP серверу {settings.smtp_server}:{settings.smtp_port}")
        
        if settings.smtp_type.lower() == "ssl":
            with smtplib.SMTP_SSL(settings.smtp_server, settings.smtp_port, timeout=SMTP_TIMEOUT) as server:
                logger.debug(f"Аутентификация как {settings.smtp_login}")
                server.login(settings.smtp_login, settings.smtp_password)
                logger.debug(f"Отправка письма на {', '.join(recipients)}")
                server.send_message(msg)
        else:  # starttls
            with smtplib.SMTP(settings.smtp_server, settings.smtp_port, timeout=SMTP_TIMEOUT) as server:
                server.starttls()
                logger.debug(f"Аутентификация как {settings.smtp_login}")
                server.login(settings.smtp_login, settings.smtp_password)
                logger.debug(f"Отправка письма на {', '.join(recipients)}")
                server.send_message(msg)
        
        logger.info(f"Email успешно отправлен на адрес(а): {', '.join(recipients)}")
        return True
        
    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"Ошибка аутентификации SMTP: {e}")
        return False
    except smtplib.SMTPException as e:
        logger.error(f"Ошибка SMTP при отправке email на {to_email}: {e}")
        return False
    except Exception as e:
        logger.error(f"Неожиданная ошибка при отправке email на {to_email}: {type(e).__name__}: {e}")
        return False


def decode_email_header(header_value: str) -> str:
    """Декодирует заголовок email."""
    if not header_value:
        return ""
    decoded_parts = decode_header(header_value)
    result = []
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(encoding or 'utf-8', errors='replace'))
        else:
            result.append(part)
    return ''.join(result)


def encode_imap_folder(folder_name: str) -> str:
    """
    Кодирует название папки в IMAP modified UTF-7.
    
    IMAP использует модифицированную версию UTF-7 для не-ASCII символов:
    - & заменяется на &-
    - Non-ASCII символы кодируются в modified base64 и оборачиваются в & и -
    """
    import base64
    
    if folder_name.isascii():
        # Только заменяем & на &- для ASCII папок
        return folder_name.replace('&', '&-')
    
    result = []
    non_ascii_buffer = []
    
    for char in folder_name:
        if ord(char) < 128:
            # ASCII символ
            if non_ascii_buffer:
                # Сначала закодируем накопленные не-ASCII символы
                utf16_bytes = ''.join(non_ascii_buffer).encode('utf-16-be')
                b64_encoded = base64.b64encode(utf16_bytes).decode('ascii')
                # Заменяем / на , для IMAP modified base64
                b64_encoded = b64_encoded.replace('/', ',').rstrip('=')
                result.append('&' + b64_encoded + '-')
                non_ascii_buffer = []
            
            if char == '&':
                result.append('&-')
            else:
                result.append(char)
        else:
            # Non-ASCII символ
            non_ascii_buffer.append(char)
    
    # Обрабатываем оставшиеся не-ASCII символы
    if non_ascii_buffer:
        utf16_bytes = ''.join(non_ascii_buffer).encode('utf-16-be')
        b64_encoded = base64.b64encode(utf16_bytes).decode('ascii')
        b64_encoded = b64_encoded.replace('/', ',').rstrip('=')
        result.append('&' + b64_encoded + '-')
    
    return ''.join(result)


def html_to_plain_text(html_content: str) -> str:
    """
    Преобразует HTML в простой текст, извлекая только значимую часть ответа.
    
    Извлекает текст из HTML и возвращает только первый блок до пустой строки
    или разделителя (цитаты из оригинального письма отбрасываются).
    
    Args:
        html_content: Строка с HTML содержимым
        
    Returns:
        Строка с извлечённым текстом (только значимая часть ответа)
    """
    if not html_content:
        return ""
    
    # Удаляем blockquote (цитаты из оригинального письма)
    text = re.sub(r'<blockquote[^>]*>.*?</blockquote>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    
    # Заменяем <br>, <br/>, </div>, </p> на переносы строк
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    
    # Удаляем все оставшиеся HTML теги
    text = re.sub(r'<[^>]+>', '', text)
    
    # Декодируем HTML сущности (&nbsp;, &amp;, и т.д.)
    text = html.unescape(text)
    
    # Извлекаем только первый блок значимого текста
    # (до пустой строки или разделителя типа "---", "___", "***")
    result_lines = []
    for line in text.split('\n'):
        stripped = line.strip()
        
        # Пустая строка или разделитель означают конец значимой части
        if not stripped:
            if result_lines:  # Если уже есть строки, заканчиваем
                break
            continue  # Пропускаем пустые строки в начале
        
        # Проверяем на разделитель (3+ одинаковых символа: -, _, *, =)
        if re.match(r'^[-_*=]{3,}$', stripped):
            break
        
        result_lines.append(stripped)
    
    return '\n'.join(result_lines)


def get_email_body(msg) -> str:
    """Извлекает текстовое тело письма (предпочитает text/plain, иначе text/html)."""
    body = ""
    html_body = ""
    
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            
            if "attachment" in content_disposition:
                continue
                
            charset = part.get_content_charset() or 'utf-8'
            
            if content_type == "text/plain":
                try:
                    body = part.get_payload(decode=True).decode(charset, errors='replace')
                    break  # Нашли text/plain - используем его
                except Exception as e:
                    logger.debug(f"Ошибка декодирования text/plain части письма: {e}")
                    continue
            elif content_type == "text/html" and not html_body:
                try:
                    html_body = part.get_payload(decode=True).decode(charset, errors='replace')
                except Exception as e:
                    logger.debug(f"Ошибка декодирования text/html части письма: {e}")
                    continue
    else:
        charset = msg.get_content_charset() or 'utf-8'
        try:
            body = msg.get_payload(decode=True).decode(charset, errors='replace')
        except Exception as e:
            logger.debug(f"Ошибка декодирования тела письма: {e}")
    
    # Если text/plain не найден, используем text/html
    if not body and html_body:
        body = html_body
        logger.debug("Используется text/html вместо text/plain")
    
    return body


# === IMAP Rate Limiting и Retry ===
IMAP_RPS_LIMIT = 5  # Максимум запросов в секунду к IMAP
IMAP_RETRY_ATTEMPTS = 3  # Количество попыток при ошибке
_last_imap_call = 0.0


def rate_limit_imap():
    """Ограничение частоты IMAP запросов."""
    global _last_imap_call
    now = time.time()
    delta = now - _last_imap_call
    if delta < 1.0 / IMAP_RPS_LIMIT:
        time.sleep((1.0 / IMAP_RPS_LIMIT) - delta)
    _last_imap_call = time.time()


def connect_imap_with_retry(server: str, port: int, login: str, password: str) -> imaplib.IMAP4_SSL:
    """
    Подключение к IMAP с повторными попытками.
    
    Returns:
        imaplib.IMAP4_SSL: Подключенный IMAP клиент
    
    Raises:
        Exception: Если не удалось подключиться после всех попыток
    """
    last_exc = None
    
    for attempt in range(1, IMAP_RETRY_ATTEMPTS + 1):
        try:
            rate_limit_imap()
            mail = imaplib.IMAP4_SSL(server, port)
            mail.login(login, password)
            return mail
        except Exception as e:
            last_exc = e
            logger.warning(f"IMAP LOGIN попытка {attempt}/{IMAP_RETRY_ATTEMPTS} завершилась ошибкой: {type(e).__name__}: {e}")
            time.sleep(0.5 * attempt)
    
    if last_exc:
        raise last_exc
    raise RuntimeError("Не удалось подключиться к IMAP после всех попыток")


def reconnect_imap_session(server: str, port: int, login: str, password: str, folder: str) -> imaplib.IMAP4_SSL:
    """
    Переподключение к IMAP с повторным выбором папки.
    
    Returns:
        imaplib.IMAP4_SSL: Новый подключенный IMAP клиент с выбранной папкой
    """
    logger.warning(f"↻ Переподключение к IMAP, папка {folder}...")
    mail = connect_imap_with_retry(server, port, login, password)
    folder_encoded = encode_imap_folder(folder)
    rate_limit_imap()
    mail.select(folder_encoded)
    logger.info("✓ Переподключение выполнено")
    return mail


def select_folder_with_retry(
    mail: imaplib.IMAP4_SSL, 
    folder: str,
    server: str, 
    port: int, 
    login: str, 
    password: str
) -> tuple:
    """
    SELECT папки с повторными попытками и переподключением.
    
    Если папка не найдена (status != 'OK'), сразу возвращает результат без retry.
    Retry выполняется только при ошибках соединения.
    
    Returns:
        tuple: (status, data, mail) - результат SELECT и актуальный mail объект
    """
    folder_encoded = encode_imap_folder(folder)
    
    for attempt in range(1, IMAP_RETRY_ATTEMPTS + 1):
        try:
            rate_limit_imap()
            status, data = mail.select(folder_encoded)
            if status == 'OK':
                return status, data, mail
            # Папка не найдена - сразу возвращаем без retry
            logger.debug(f"Папка '{folder}' не найдена (status={status})")
            return status, data, mail
        except Exception as e:
            logger.warning(f"SELECT попытка {attempt}/{IMAP_RETRY_ATTEMPTS} завершилась ошибкой: {type(e).__name__}: {e}")
        
        try:
            mail = reconnect_imap_session(server, port, login, password, folder)
            return 'OK', None, mail
        except Exception as reconnect_error:
            logger.error(f"Не удалось переподключиться при SELECT: {type(reconnect_error).__name__}: {reconnect_error}")
            time.sleep(0.5 * attempt)
    
    return 'NO', None, mail


def search_with_retry(
    mail: imaplib.IMAP4_SSL,
    search_criteria: str,
    folder: str,
    server: str,
    port: int,
    login: str,
    password: str
) -> tuple:
    """
    SEARCH с повторными попытками и переподключением.
    
    Returns:
        tuple: (status, message_ids, mail) - результат SEARCH и актуальный mail объект
    """
    for attempt in range(1, IMAP_RETRY_ATTEMPTS + 1):
        try:
            rate_limit_imap()
            status, message_ids = mail.search(None, search_criteria)
            if status == 'OK':
                return status, message_ids, mail
            logger.warning(f"SEARCH попытка {attempt}/{IMAP_RETRY_ATTEMPTS} вернула {status}")
        except Exception as e:
            logger.warning(f"SEARCH попытка {attempt}/{IMAP_RETRY_ATTEMPTS} завершилась ошибкой: {type(e).__name__}: {e}")
        
        try:
            mail = reconnect_imap_session(server, port, login, password, folder)
        except Exception as reconnect_error:
            logger.error(f"Не удалось переподключиться при SEARCH: {type(reconnect_error).__name__}: {reconnect_error}")
            time.sleep(0.5 * attempt)
            continue
    
    return 'NO', [b''], mail


def fetch_with_retry(
    mail: imaplib.IMAP4_SSL,
    msg_id: bytes,
    fetch_cmd: str,
    folder: str,
    server: str,
    port: int,
    login: str,
    password: str
) -> tuple:
    """
    FETCH с повторными попытками и переподключением.
    
    Returns:
        tuple: (status, msg_data, mail) - результат FETCH и актуальный mail объект
    """
    for attempt in range(1, IMAP_RETRY_ATTEMPTS + 1):
        try:
            rate_limit_imap()
            status, msg_data = mail.fetch(msg_id, fetch_cmd)
            if status == 'OK':
                return status, msg_data, mail
            logger.warning(f"FETCH попытка {attempt}/{IMAP_RETRY_ATTEMPTS} вернула {status}")
        except Exception as e:
            logger.warning(f"FETCH попытка {attempt}/{IMAP_RETRY_ATTEMPTS} завершилась ошибкой: {type(e).__name__}: {e}")
        
        try:
            mail = reconnect_imap_session(server, port, login, password, folder)
        except Exception as reconnect_error:
            logger.error(f"Не удалось переподключиться при FETCH: {type(reconnect_error).__name__}: {reconnect_error}")
            time.sleep(0.5 * attempt)
            continue
    
    return 'NO', None, mail


def read_imap_confirmation_emails(settings: SettingParams) -> set:
    """
    Читает IMAP почтовый ящик и извлекает список пользователей для подтверждения удаления.
    
    Ищет сообщения не старше CHECK_IMAP_DAYS дней:
    - с темой CONFIRM_MESSAGE_SUBJECT (точное совпадение)
    - или с темой, содержащей LICENSE_WARNING_MESSAGE_SUBJECT (для ответов на отчёт о лицензиях)
    - или с темой, содержащей WAITING_CONFIRMATION_SUBJECT (для ответов на запрос подтверждения)
    
    Формат тела сообщения:
        удалить
        user1@example.com
        user2_nickname
        1130000000000123
        ...
    
    Returns:
        set: Множество идентификаторов (email, nickname, uid) пользователей для удаления
    """
    if not settings.approved_senders:
        logger.warning("APPROVED_SENDERS не задан. Чтение подтверждений по IMAP и удаление пользователей отключены.")
        return set()
    
    if not all([settings.imap_server, settings.imap_login, settings.imap_password]):
        logger.warning("Параметры IMAP не заданы полностью. Подтверждение по email недоступно.")
        return set()
    
    confirmed_users = set()
    
    # Параметры для retry
    server = settings.imap_server
    port = settings.imap_port
    login = settings.imap_login
    password = settings.imap_password
    
    try:
        logger.info(f"Подключение к IMAP серверу {server}:{port}")
        
        # Подключаемся к IMAP серверу с retry
        mail = connect_imap_with_retry(server, port, login, password)
        
        logger.debug("Успешная аутентификация на IMAP сервере")
        
        # Вычисляем дату для поиска (не старше CHECK_IMAP_DAYS дней)
        since_date = (datetime.now() - timedelta(days=settings.check_imap_days)).strftime("%d-%b-%Y")
        search_criteria = f'SINCE "{since_date}"'
        
        # Формируем список папок для поиска
        folders_to_search = ["INBOX", "Входящие"]
        if settings.confirmation_imap_folder:
            folders_to_search.append(settings.confirmation_imap_folder)
        
        # Убираем дубликаты, сохраняя порядок
        seen_folders = set()
        unique_folders = []
        for folder in folders_to_search:
            if folder not in seen_folders:
                seen_folders.add(folder)
                unique_folders.append(folder)
        
        logger.debug(f"Папки для поиска: {unique_folders}")
        
        # Поиск писем во всех папках
        for folder in unique_folders:
            try:
                # SELECT папки с retry
                status, _, mail = select_folder_with_retry(mail, folder, server, port, login, password)
                if status != 'OK':
                    logger.debug(f"Папка '{folder}' не найдена или недоступна. Пропуск.")
                    continue
                
                logger.info(f"Поиск в папке: {folder}")
                
                # SEARCH с retry
                logger.debug(f"IMAP поиск: {search_criteria}")
                status, message_ids, mail = search_with_retry(mail, search_criteria, folder, server, port, login, password)
                
                if status != 'OK' or not message_ids[0]:
                    logger.debug(f"Писем в папке '{folder}' за указанный период не найдено.")
                    continue
                
                message_id_list = message_ids[0].split()
                logger.info(f"Найдено писем в '{folder}' (SINCE {since_date}): {len(message_id_list)}")
                
                for msg_id in message_id_list:
                    try:
                        # FETCH с retry
                        status, msg_data, mail = fetch_with_retry(mail, msg_id, '(RFC822)', folder, server, port, login, password)
                        if status != 'OK' or not msg_data:
                            logger.warning(f"Не удалось загрузить письмо {msg_id}")
                            continue
                        
                        raw_email = msg_data[0][1]
                        msg = email.message_from_bytes(raw_email)
                        
                        # Проверяем отправителя письма
                        from_header = msg.get('From', '')
                        # Извлекаем email адрес из заголовка From (может быть в формате "Name <email@domain.com>")
                        from_match = re.search(r'<([^>]+)>', from_header)
                        sender_email = from_match.group(1).lower() if from_match else from_header.strip().lower()
                        
                        if settings.approved_senders and sender_email not in settings.approved_senders:
                            logger.debug(f"Письмо {msg_id}: отправитель '{sender_email}' не в списке одобренных. Пропуск.")
                            continue
                        
                        # Проверяем тему письма
                        subject = decode_email_header(msg.get('Subject', ''))
                        logger.debug(f"Обработка письма: {subject}")
                        
                        # Письмо подходит, если:
                        # 1. Тема точно совпадает с confirm_message_subject
                        # 2. ИЛИ тема содержит license_warning_message_subject (ответ на отчёт о лицензиях)
                        # 3. ИЛИ тема содержит waiting_confirmation_subject (ответ на запрос подтверждения)
                        is_confirm_subject = subject.strip() == settings.confirm_message_subject.strip()
                        is_license_reply = settings.license_warning_message_subject in subject
                        is_waiting_reply = settings.waiting_confirmation_subject in subject
                        
                        if not is_confirm_subject and not is_license_reply and not is_waiting_reply:
                            logger.debug(f"Письмо {msg_id}: тема '{subject}' не соответствует критериям поиска. Пропуск.")
                            continue
                        
                        # Извлекаем тело письма
                        body = get_email_body(msg)
                        if not body:
                            logger.warning(f"Пустое тело письма {msg_id}")
                            continue
                        
                        # Преобразуем HTML в текст (если тело содержит HTML)
                        if '<' in body and '>' in body:
                            body = html_to_plain_text(body)
                            logger.debug(f"Письмо {msg_id}: тело преобразовано из HTML в текст")
                        
                        # Парсим тело письма
                        lines = body.strip().split('\n')
                        if not lines:
                            continue
                        
                        # Первая строка должна быть "удалить" или "delete"
                        first_line = lines[0].strip().lower()
                        if first_line not in ("удалить", "delete"):
                            logger.debug(f"Письмо {msg_id}: первая строка не 'удалить/delete', а '{first_line}'. Пропуск.")
                            continue
                        
                        # Остальные строки - идентификаторы пользователей
                        for line in lines[1:]:
                            user_id = line.strip()
                            if user_id:
                                confirmed_users.add(user_id.lower())
                                logger.debug(f"Добавлен подтверждённый пользователь: {user_id}")
                        
                    except Exception as e:
                        logger.error(f"Ошибка при обработке письма {msg_id}: {type(e).__name__}: {e}")
                        continue
                        
            except imaplib.IMAP4.error as e:
                logger.debug(f"Ошибка при работе с папкой '{folder}': {e}. Пропуск.")
                continue
        
        mail.logout()
        logger.info(f"Всего подтверждённых пользователей из IMAP: {len(confirmed_users)}")
        
    except imaplib.IMAP4.error as e:
        logger.error(f"Ошибка IMAP: {e}")
    except Exception as e:
        logger.error(f"Неожиданная ошибка при чтении IMAP: {type(e).__name__}: {e}")
    
    return confirmed_users


def is_user_confirmed(user: dict, confirmed_set: set) -> bool:
    """
    Проверяет, есть ли пользователь в списке подтверждённых.
    
    Сравнивает email, nickname и id пользователя со списком подтверждённых.
    
    Args:
        user: Данные пользователя
        confirmed_set: Множество подтверждённых идентификаторов (в нижнем регистре)
    
    Returns:
        bool: True если пользователь подтверждён
    """
    if not confirmed_set:
        return False
    
    # Проверяем email
    user_email = user.get('email', '').lower()
    if user_email and user_email in confirmed_set:
        return True
    
    # Проверяем nickname
    user_nickname = user.get('nickname', '').lower()
    if user_nickname and user_nickname in confirmed_set:
        return True

    aliases = user.get('aliases', [])
    for alias in aliases:
        if alias and alias.lower() in confirmed_set:
            return True
    
    # Проверяем id (uid)
    user_id = str(user.get('id', ''))
    if user_id and user_id in confirmed_set:
        return True
    
    return False


def load_blocked_users_exceptions(settings: SettingParams) -> set:
    """
    Загружает список исключений для заблокированных пользователей из файла.
    
    Для пользователей из этого списка не будут отправляться уведомления
    о будущем удалении, и они не будут учитываться в отчётах о приближающемся удалении.
    
    Формат файла:
        - Каждая строка содержит один идентификатор (email, nickname или uid)
        - Строки, начинающиеся с # - комментарии (игнорируются)
        - Пустые строки игнорируются
    
    Args:
        settings: Параметры настроек скрипта
    
    Returns:
        set: Множество идентификаторов исключений (в нижнем регистре)
    """
    exceptions_set = set()
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    exceptions_file = os.path.join(script_dir, settings.blocked_users_exceptions_file)
    
    if not os.path.exists(exceptions_file):
        logger.debug(f"Файл исключений {settings.blocked_users_exceptions_file} не найден.")
        return exceptions_set
    
    try:
        with open(exceptions_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                # Пропускаем пустые строки и комментарии
                if not line or line.startswith('#'):
                    continue
                exceptions_set.add(line.lower())
        
        if exceptions_set:
            logger.info(f"Загружено {len(exceptions_set)} исключений из {settings.blocked_users_exceptions_file}")
            logger.debug(f"Исключения: {exceptions_set}")
        
    except Exception as e:
        logger.error(f"Ошибка при чтении файла исключений {exceptions_file}: {e}")
    
    return exceptions_set


def is_user_exception(user: dict, exceptions_set: set) -> bool:
    """
    Проверяет, является ли пользователь исключением (не требует уведомления об удалении).
    
    Сравнивает email, nickname, aliases и id пользователя со списком исключений.
    
    Args:
        user: Данные пользователя
        exceptions_set: Множество идентификаторов исключений (в нижнем регистре)
    
    Returns:
        bool: True если пользователь является исключением
    """
    if not exceptions_set:
        return False
    
    # Проверяем email
    user_email = user.get('email', '').lower()
    if user_email and user_email in exceptions_set:
        return True
    
    # Проверяем nickname
    user_nickname = user.get('nickname', '').lower()
    if user_nickname and user_nickname in exceptions_set:
        return True
    
    # Проверяем aliases
    aliases = user.get('aliases', [])
    for alias in aliases:
        if alias and alias.lower() in exceptions_set:
            return True
    
    # Проверяем id (uid)
    user_id = str(user.get('id', ''))
    if user_id and user_id in exceptions_set:
        return True
    
    return False


def filter_exception_users(users: list, exceptions_set: set) -> tuple:
    """
    Разделяет список пользователей на обычных и исключения.
    
    Args:
        users: Список пользователей
        exceptions_set: Множество идентификаторов исключений
        
    Returns:
        tuple: (regular_users: list, exception_users: list)
    """
    regular_users = []
    exception_users = []
    
    for user in users:
        if is_user_exception(user, exceptions_set):
            exception_users.append(user)
        else:
            regular_users.append(user)
    
    return regular_users, exception_users


def format_user_info(user: dict) -> str:
    """Форматирует информацию о пользователе для отображения."""
    name = user.get('name', {})
    full_name = f"{name.get('last', '')} {name.get('first', '')} {name.get('middle', '')}".strip()
    nickname = user.get('nickname', '')
    email_addr = user.get('email', '')
    lock_date_unknown = user.get('_lock_date_unknown', False)
    lock_date = user.get('isEnabledUpdatedAt', '')
    
    if lock_date:
        parsed_date = parse_date(lock_date)
        if parsed_date:
            lock_date_str = parsed_date.strftime('%d.%m.%Y %H:%M')
        else:
            lock_date_str = 'неизвестно'
    elif lock_date_unknown:
        lock_date_str = 'не установлена (используется значение по умолчанию)'
    else:
        lock_date_str = 'неизвестно'
    
    return f"{full_name} ({nickname}, {email_addr}) - заблокирован: {lock_date_str}"


# Константы для статуса запуска модулей
RUN_STATUS_FILE = "run_status.csv"
RUN_STATUS_RUNNING = "Running"
RUN_STATUS_SUCCESS = "Success"
RUN_STATUS_ERROR = "Error"


def reset_module_run_status(settings: SettingParams, module_name: str):
    """
    Сбрасывает статус и ошибку для модуля перед его запуском.
    
    Args:
        settings: Настройки скрипта
        module_name: Имя модуля
    """
    settings.run_status[module_name] = RUN_STATUS_RUNNING
    settings.run_error[module_name] = ""


def set_module_run_status(settings: SettingParams, module_name: str, success: bool, error: str = ""):
    """
    Устанавливает статус и ошибку для модуля после его выполнения.
    
    Args:
        settings: Настройки скрипта
        module_name: Имя модуля
        success: True если модуль выполнен успешно
        error: Сообщение об ошибке (если есть)
    """
    settings.run_status[module_name] = RUN_STATUS_SUCCESS if success else RUN_STATUS_ERROR
    settings.run_error[module_name] = error


def save_run_status_to_csv(settings: SettingParams, module_name: str):
    """
    Сохраняет статус последнего запуска модуля в CSV файл.
    
    Файл имеет формат: module;time_last_run;status;error
    Каждая строка содержит информацию о последнем запуске конкретного модуля.
    При обновлении статуса модуля его строка перезаписывается, остальные сохраняются.
    
    Args:
        settings: Настройки скрипта
        module_name: Имя модуля для сохранения
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.join(script_dir, RUN_STATUS_FILE)
    
    # Читаем существующие записи
    existing_records = {}
    if os.path.exists(csv_path):
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                # Пропускаем заголовок
                for line in lines[1:]:
                    line = line.strip()
                    if line:
                        parts = line.split(';')
                        if len(parts) >= 4:
                            existing_records[parts[0]] = line
        except Exception as e:
            logger.warning(f"Ошибка чтения файла статуса {csv_path}: {e}")
    
    # Обновляем или добавляем запись для текущего модуля
    status = settings.run_status.get(module_name, "")
    error = settings.run_error.get(module_name, "")
    # Экранируем точку с запятой в сообщении об ошибке
    error_escaped = error.replace(';', ',').replace('\n', ' ').replace('\r', '')
    time_now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    new_line = f"{module_name};{time_now};{status};{error_escaped}"
    existing_records[module_name] = new_line
    
    # Записываем файл
    try:
        with open(csv_path, 'w', encoding='utf-8') as f:
            f.write("module;time_last_run;status;error\n")
            for record_line in existing_records.values():
                f.write(record_line + "\n")
        logger.debug(f"Статус модуля {module_name} сохранён в {RUN_STATUS_FILE}")
    except Exception as e:
        logger.error(f"Ошибка записи файла статуса {csv_path}: {e}")
