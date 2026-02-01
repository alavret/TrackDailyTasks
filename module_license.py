#!/usr/bin/env python3
"""
Модуль проверки лицензий Yandex 360.

Проверяет количество свободных лицензий и отправляет уведомления
при достижении порогового значения.
"""

import copy
from datetime import datetime

from common import (
    logger,
    SettingParams,
    get_settings,
    get_all_users,
    get_blocked_users,
    sort_blocked_users_by_lock_date,
    get_users_near_deletion,
    parse_date,
    format_user_info,
    send_email,
    load_blocked_users_exceptions,
    filter_exception_users,
)


def generate_license_alert_html(
    settings: SettingParams,
    active_users_count: int,
    free_licenses: int,
    blocked_users: list,
    near_deletion_users: list,
    exception_users: list = None
) -> str:
    """Генерирует HTML-содержимое для письма о лицензиях."""
    
    if exception_users is None:
        exception_users = []
    
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        h1 {{ color: #333; }}
        h2 {{ color: #555; margin-top: 30px; }}
        .warning {{ color: #d9534f; font-weight: bold; }}
        .info {{ color: #5bc0de; }}
        table {{ border-collapse: collapse; width: 100%; margin-top: 10px; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #f5f5f5; }}
        tr:nth-child(even) {{ background-color: #fafafa; }}
        .near-deletion {{ background-color: #fcf8e3; }}
    </style>
</head>
<body>
    <h1>Отчёт о лицензиях Yandex 360</h1>
    
    <p><strong>Дата отчёта:</strong> {datetime.now().strftime('%d.%m.%Y %H:%M')}</p>
    
    <h2>Статистика лицензий</h2>
    <table>
        <tr><th>Показатель</th><th>Значение</th></tr>
        <tr><td>Всего лицензий</td><td>{settings.licenses_count}</td></tr>
        <tr><td>Активных пользователей</td><td>{active_users_count}</td></tr>
        <tr><td>Свободных лицензий</td><td class="{'warning' if free_licenses < settings.licenses_threshold else ''}">{free_licenses}</td></tr>
        <tr><td>Пороговое значение</td><td>{settings.licenses_threshold}</td></tr>
        <tr><td>Заблокированных пользователей</td><td>{len(blocked_users)}</td></tr>
    </table>
    
    {"<p class='warning'>⚠️ ВНИМАНИЕ: Количество свободных лицензий ниже порогового значения!</p>" if free_licenses < settings.licenses_threshold else ""}
"""
    
    if blocked_users:
        html += """
    <h2>Заблокированные пользователи</h2>
    <p>Отсортированы по дате блокировки (новые первыми):</p>
    <table>
        <tr>
            <th>№</th>
            <th>ФИО</th>
            <th>Логин</th>
            <th>Email</th>
            <th>Дата блокировки</th>
        </tr>
"""
        for idx, user in enumerate(blocked_users, 1):
            name = user.get('name', {})
            full_name = f"{name.get('last', '')} {name.get('first', '')} {name.get('middle', '')}".strip()
            nickname = user.get('nickname', '')
            email = user.get('email', '')
            lock_date = user.get('isEnabledUpdatedAt', '')
            parsed_date = parse_date(lock_date)
            if parsed_date:
                lock_date_formatted = parsed_date.strftime('%d.%m.%Y %H:%M')
            else:
                lock_date_formatted = '<span class="info" title="Используется значение по умолчанию">не установлена</span>'
            
            html += f"""
        <tr>
            <td>{idx}</td>
            <td>{full_name}</td>
            <td>{nickname}</td>
            <td>{email}</td>
            <td>{lock_date_formatted}</td>
        </tr>
"""
        html += "    </table>\n"
    
    if near_deletion_users:
        html += f"""
    <h2 class="warning">⚠️ Пользователи, которые скоро будут удалены</h2>
    <p>Срок удаления: {settings.delete_after_locked_days} дней после блокировки. Предупреждение за {settings.warning_days} дней.</p>
    <table>
        <tr>
            <th>№</th>
            <th>ФИО</th>
            <th>Логин</th>
            <th>Email</th>
            <th>Дата блокировки</th>
            <th>Дата удаления</th>
            <th>Осталось дней</th>
        </tr>
"""
        for idx, user in enumerate(near_deletion_users, 1):
            name = user.get('name', {})
            full_name = f"{name.get('last', '')} {name.get('first', '')} {name.get('middle', '')}".strip()
            nickname = user.get('nickname', '')
            email = user.get('email', '')
            lock_date_unknown = user.get('_lock_date_unknown', False)
            lock_date = user.get('isEnabledUpdatedAt', '')
            parsed_date = parse_date(lock_date)
            if parsed_date:
                lock_date_formatted = parsed_date.strftime('%d.%m.%Y')
            elif lock_date_unknown:
                lock_date_formatted = f'<span class="info" title="Используется значение по умолчанию: {settings.value_for_empty_date.strftime("%d.%m.%Y")}">не установлена</span>'
            else:
                lock_date_formatted = 'неизвестно'
            deletion_date = user.get('_calculated_deletion_date')
            deletion_date_formatted = deletion_date.strftime('%d.%m.%Y') if deletion_date else 'неизвестно'
            days_left = user.get('_days_until_deletion', '?')
            
            html += f"""
        <tr class="near-deletion">
            <td>{idx}</td>
            <td>{full_name}</td>
            <td>{nickname}</td>
            <td>{email}</td>
            <td>{lock_date_formatted}</td>
            <td>{deletion_date_formatted}</td>
            <td>{days_left}</td>
        </tr>
"""
        html += "    </table>\n"
    
    if exception_users:
        html += f"""
    <h2>ℹ️ Учётные записи без учёта блокировок (исключения)</h2>
    <p>Для следующих заблокированных пользователей не ведётся учёт блокировок и не отправляются уведомления об удалении.</p>
    <table>
        <tr>
            <th>№</th>
            <th>ФИО</th>
            <th>Логин</th>
            <th>Email</th>
            <th>Дата блокировки</th>
        </tr>
"""
        for idx, user in enumerate(exception_users, 1):
            name = user.get('name', {})
            full_name = f"{name.get('last', '')} {name.get('first', '')} {name.get('middle', '')}".strip()
            nickname = user.get('nickname', '')
            email = user.get('email', '')
            lock_date = user.get('isEnabledUpdatedAt', '')
            parsed_date = parse_date(lock_date)
            if parsed_date:
                lock_date_formatted = parsed_date.strftime('%d.%m.%Y %H:%M')
            else:
                lock_date_formatted = '<span class="info" title="Используется значение по умолчанию">не установлена</span>'
            
            html += f"""
        <tr>
            <td>{idx}</td>
            <td>{full_name}</td>
            <td>{nickname}</td>
            <td>{email}</td>
            <td>{lock_date_formatted}</td>
        </tr>
"""
        html += "    </table>\n"
    
    html += """
</body>
</html>
"""
    return html


def run(settings: SettingParams) -> bool:
    """
    Проверка количества свободных лицензий.
    
    Args:
        settings: Настройки скрипта
        
    Returns:
        bool: True если проверка прошла успешно, False при ошибке
    """
    logger.info("=" * 80)
    logger.info("Запуск проверки: license")
    logger.info("=" * 80)
    
    if settings.licenses_count <= 0:
        logger.warning("LICENSES_COUNT не установлен или равен 0. Пропуск проверки.")
        return True
    
    # Получаем всех пользователей (deep copy для безопасной работы в асинхронном режиме)
    users_original = get_all_users(settings, force=True)
    if not users_original:
        logger.error("Не удалось получить список пользователей.")
        return False
    users = copy.deepcopy(users_original)
    
    # Подсчитываем активных и заблокированных
    blocked_users = get_blocked_users(users)
    active_users_count = len(users) - len(blocked_users)
    free_licenses = settings.licenses_count - len(users)
    
    logger.info(f"Всего пользователей: {len(users)}")
    logger.info(f"Активных пользователей: {active_users_count}")
    logger.info(f"Заблокированных пользователей: {len(blocked_users)}")
    logger.info(f"Всего лицензий: {settings.licenses_count}")
    logger.info(f"Свободных лицензий: {free_licenses}")
    logger.info(f"Пороговое значение: {settings.licenses_threshold}")
    
    # Сортируем заблокированных по дате блокировки
    sorted_blocked = sort_blocked_users_by_lock_date(blocked_users, settings.value_for_empty_date)
    
    # Загружаем исключения
    exceptions_set = load_blocked_users_exceptions()
    
    # Находим пользователей, которые скоро будут удалены
    all_near_deletion = get_users_near_deletion(
        blocked_users,
        settings.delete_after_locked_days,
        settings.warning_days,
        settings.value_for_empty_date
    )
    
    # Фильтруем исключения из near_deletion
    near_deletion, exception_users = filter_exception_users(all_near_deletion, exceptions_set)
    
    if exception_users:
        logger.info(f"Пользователей-исключений (без учёта блокировок): {len(exception_users)}")
    
    if near_deletion:
        logger.warning(f"Пользователей, которые скоро будут удалены: {len(near_deletion)}")
        for user in near_deletion:
            name = user.get('name', {})
            full_name = f"{name.get('last', '')} {name.get('first', '')} {name.get('middle', '')}".strip()
            nickname = user.get('nickname', '')
            email = user.get('email', '')
            lock_date_raw = user.get('isEnabledUpdatedAt', '')
            parsed_lock_date = parse_date(lock_date_raw)
            lock_date = parsed_lock_date.strftime('%d.%m.%y') if parsed_lock_date else 'нет даты'
            days_until_deletion = user.get('_days_until_deletion', '?')
            to_be_deleted = "Да" if days_until_deletion <= 0 else "Нет"
            days_until_deletion_str = "дата в прошлом" if isinstance(days_until_deletion, int) and days_until_deletion < 0 else f"{days_until_deletion} дней"
            logger.warning(
                f"  - {full_name} ({nickname}, {email}) | Дата блокировки: {lock_date} | "
                f"Удаление через: {days_until_deletion_str} | "
                f"Подпадает под удаление: {to_be_deleted}"
            )
    
    # Проверяем, нужно ли отправлять уведомление
    should_send_alert = free_licenses < settings.licenses_threshold
    
    if should_send_alert or near_deletion:
        if not settings.alert_email:
            logger.warning("ALERT_EMAIL не установлен, уведомление не будет отправлено.")
            return True
        
        if should_send_alert:
            logger.warning(f"Свободных лицензий ({free_licenses}) меньше порогового значения ({settings.licenses_threshold})!")
        
        # Генерируем и отправляем письмо
        subject = f"{settings.license_warning_message_subject} - свободно: {free_licenses}"
        if should_send_alert:
            subject = f"{settings.license_warning_message_subject} ⚠️ ВНИМАНИЕ: свободных лицензий: {free_licenses} (порог: {settings.licenses_threshold})"
        
        html_body = generate_license_alert_html(
            settings,
            active_users_count,
            free_licenses,
            sorted_blocked,
            near_deletion,
            exception_users
        )
        
        if send_email(settings, settings.alert_email, subject, html_body):
            logger.info(f"Уведомление отправлено на {settings.alert_email}")
        else:
            logger.error("Не удалось отправить уведомление по email.")
    else:
        logger.info("Лицензий достаточно, уведомление не требуется.")
    
    return True


def main():
    """Главная функция для запуска модуля напрямую."""
    logger.info("=" * 80)
    logger.info("Запуск модуля module_license.py напрямую")
    logger.info(f"Время запуска: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    logger.info("=" * 80)
    
    settings = get_settings()
    if not settings:
        logger.error("Не удалось загрузить настройки. Завершение работы.")
        return
    
    run(settings)


if __name__ == "__main__":
    main()
