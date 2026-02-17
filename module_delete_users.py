#!/usr/bin/env python3
"""
Модуль удаления пользователей Yandex 360.

Удаляет пользователей, заблокированных дольше указанного срока,
с подтверждением через IMAP почту.
"""

import copy
from datetime import datetime

from common import (
    logger,
    SettingParams,
    get_settings,
    get_all_users,
    get_blocked_users,
    enrich_users,
    get_users_for_deletion,
    parse_date,
    format_user_info,
    send_email,
    read_imap_confirmation_emails,
    is_user_confirmed,
    delete_user_by_api,
    load_blocked_users_exceptions,
    filter_exception_users,
)


def generate_deletion_report_html(
    settings: SettingParams, 
    deleted_users: list, 
    not_confirmed_users: list,
    is_dry_run: bool,
    exception_users: list = None,
    delegated_or_forwarding_users: list = None
) -> str:
    """Генерирует HTML-содержимое для письма об удалении пользователей с учётом подтверждения."""
    
    if exception_users is None:
        exception_users = []
    if delegated_or_forwarding_users is None:
        delegated_or_forwarding_users = []
    
    action_word = "могли быть удалены" if is_dry_run else "были удалены"
    title = "Предупреждение: пользователи могут быть удалены" if is_dry_run else "Отчёт об удалении пользователей"
    
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        h1 {{ color: {'#f0ad4e' if is_dry_run else '#d9534f'}; }}
        h2 {{ color: #555; margin-top: 30px; }}
        .warning {{ color: #f0ad4e; font-weight: bold; }}
        .danger {{ color: #d9534f; font-weight: bold; }}
        .info {{ color: #5bc0de; font-weight: bold; }}
        table {{ border-collapse: collapse; width: 100%; margin-top: 10px; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #f5f5f5; }}
        tr:nth-child(even) {{ background-color: #fafafa; }}
        .not-confirmed {{ background-color: #fcf8e3; }}
    </style>
</head>
<body>
    <h1>{title}</h1>
    
    <p><strong>Дата:</strong> {datetime.now().strftime('%d.%m.%Y %H:%M')}</p>
    <p><strong>Режим DRY_RUN:</strong> {'Да (реальное удаление не выполнялось)' if is_dry_run else 'Нет'}</p>
    <p><strong>Срок удаления после блокировки:</strong> {settings.delete_after_locked_days} дней</p>
"""
    
    if deleted_users:
        html += f"""
    <h2>Пользователи, которые {action_word} (подтверждение получено)</h2>
    <table>
        <tr>
            <th>№</th>
            <th>ФИО</th>
            <th>Логин</th>
            <th>Email</th>
            <th>Должность / Отдел</th>
            <th>Дата блокировки</th>
            <th>ID</th>
            <th>Делегирован</th>
            <th>Правила пересылки</th>
        </tr>
"""
        
        for idx, user in enumerate(deleted_users, 1):
            name = user.get('name', {})
            full_name = f"{name.get('last', '')} {name.get('first', '')} {name.get('middle', '')}".strip()
            nickname = user.get('nickname', '')
            user_email = user.get('email', '')
            user_id = user.get('id', '')
            position = user.get('position', '')
            department = user.get('department', {}).get('name', '') if isinstance(user.get('department'), dict) else user.get('department', '')
            position_dept = f"{position}, {department}".strip(', ') if position or department else ''
            lock_date_unknown = user.get('_lock_date_unknown', False)
            lock_date = user.get('isEnabledUpdatedAt', '')
            parsed_date = parse_date(lock_date)
            if parsed_date:
                lock_date_formatted = parsed_date.strftime('%d.%m.%Y %H:%M')
            elif lock_date_unknown:
                lock_date_formatted = f'<span class="warning" title="Используется значение по умолчанию: {settings.value_for_empty_date.strftime("%d.%m.%Y")}">не установлена</span>'
            else:
                lock_date_formatted = 'неизвестно'
            
            is_delegated = "✅ Да" if user.get('isDelegated', False) else "❌ Нет"
            has_forwarding = "✅ Да" if user.get('hasForwardingRules', False) else "❌ Нет"
            
            html += f"""
        <tr>
            <td>{idx}</td>
            <td>{full_name}</td>
            <td>{nickname}</td>
            <td>{user_email}</td>
            <td>{position_dept}</td>
            <td>{lock_date_formatted}</td>
            <td>{user_id}</td>
            <td>{is_delegated}</td>
            <td>{has_forwarding}</td>
        </tr>
"""
        
        html += "    </table>\n"
        
        if is_dry_run:
            html += """
    <p class="warning">⚠️ Это предупреждение. Режим DRY_RUN включён, реальное удаление не выполнялось.</p>
    <p>Чтобы выполнить удаление, установите DRY_RUN=False в файле .env</p>
"""
        else:
            html += """
    <p class="danger">❌ Указанные пользователи были удалены из системы.</p>
"""
    
    if not_confirmed_users:
        html += f"""
    <h2 class="info">ℹ️ Пользователи без подтверждения удаления</h2>
    <p>Следующие пользователи подлежат удалению, но подтверждение по email не получено.</p>
    <p>Для удаления отправьте письмо на почту с темой "{settings.confirm_message_subject}".</p>
    <p>Формат тела письма:</p>
    <pre>удалить
    user@example.com
    nickname
    user_id</pre>
    <p>или</p>
    <pre>delete
    user@example.com
    nickname
    user_id</pre>
    <table>
        <tr>
            <th>№</th>
            <th>ФИО</th>
            <th>Логин</th>
            <th>Email</th>
            <th>Должность / Отдел</th>
            <th>Дата блокировки</th>
            <th>ID</th>
            <th>Делегирован</th>
            <th>Правила пересылки</th>
        </tr>
"""
        
        for idx, user in enumerate(not_confirmed_users, 1):
            name = user.get('name', {})
            full_name = f"{name.get('last', '')} {name.get('first', '')} {name.get('middle', '')}".strip()
            nickname = user.get('nickname', '')
            user_email = user.get('email', '')
            user_id = user.get('id', '')
            position = user.get('position', '')
            department = user.get('department', {}).get('name', '') if isinstance(user.get('department'), dict) else user.get('department', '')
            position_dept = f"{position}, {department}".strip(', ') if position or department else ''
            lock_date_unknown = user.get('_lock_date_unknown', False)
            lock_date = user.get('isEnabledUpdatedAt', '')
            parsed_date = parse_date(lock_date)
            if parsed_date:
                lock_date_formatted = parsed_date.strftime('%d.%m.%Y %H:%M')
            elif lock_date_unknown:
                lock_date_formatted = f'<span class="warning" title="Используется значение по умолчанию: {settings.value_for_empty_date.strftime("%d.%m.%Y")}">не установлена</span>'
            else:
                lock_date_formatted = 'неизвестно'
            
            is_delegated = "✅ Да" if user.get('isDelegated', False) else "❌ Нет"
            has_forwarding = "✅ Да" if user.get('hasForwardingRules', False) else "❌ Нет"
            
            html += f"""
        <tr class="not-confirmed">
            <td>{idx}</td>
            <td>{full_name}</td>
            <td>{nickname}</td>
            <td>{user_email}</td>
            <td>{position_dept}</td>
            <td>{lock_date_formatted}</td>
            <td>{user_id}</td>
            <td>{is_delegated}</td>
            <td>{has_forwarding}</td>
        </tr>
"""
        
        html += "    </table>\n"
    
    if exception_users:
        html += f"""
    <h2>ℹ️ Учётные записи без учёта блокировок (исключения)</h2>
    <p>Следующие заблокированные пользователи не подлежат автоматическому удалению и не требуют подтверждения.</p>
    <table>
        <tr>
            <th>№</th>
            <th>ФИО</th>
            <th>Логин</th>
            <th>Email</th>
            <th>Должность / Отдел</th>
            <th>Дата блокировки</th>
            <th>ID</th>
            <th>Делегирован</th>
            <th>Правила пересылки</th>
        </tr>
"""
        for idx, user in enumerate(exception_users, 1):
            name = user.get('name', {})
            full_name = f"{name.get('last', '')} {name.get('first', '')} {name.get('middle', '')}".strip()
            nickname = user.get('nickname', '')
            user_email = user.get('email', '')
            user_id = user.get('id', '')
            position = user.get('position', '')
            department = user.get('department', {}).get('name', '') if isinstance(user.get('department'), dict) else user.get('department', '')
            position_dept = f"{position}, {department}".strip(', ') if position or department else ''
            lock_date = user.get('isEnabledUpdatedAt', '')
            parsed_date = parse_date(lock_date)
            if parsed_date:
                lock_date_formatted = parsed_date.strftime('%d.%m.%Y %H:%M')
            else:
                lock_date_formatted = '<span class="info">не установлена</span>'
            
            is_delegated = "✅ Да" if user.get('isDelegated', False) else "❌ Нет"
            has_forwarding = "✅ Да" if user.get('hasForwardingRules', False) else "❌ Нет"
            
            html += f"""
        <tr>
            <td>{idx}</td>
            <td>{full_name}</td>
            <td>{nickname}</td>
            <td>{user_email}</td>
            <td>{position_dept}</td>
            <td>{lock_date_formatted}</td>
            <td>{user_id}</td>
            <td>{is_delegated}</td>
            <td>{has_forwarding}</td>
        </tr>
"""
        html += "    </table>\n"
    
    if delegated_or_forwarding_users:
        html += f"""
    <h2>⚠️ Пользователи, исключённые из удаления (делегирование/пересылка)</h2>
    <p>Следующие пользователи имеют делегированные почтовые ящики или настроенные правила пересылки и не могут быть автоматически удалены.</p>
    <table>
        <tr>
            <th>№</th>
            <th>ФИО</th>
            <th>Логин</th>
            <th>Email</th>
            <th>Должность / Отдел</th>
            <th>Дата блокировки</th>
            <th>ID</th>
            <th>Делегирован</th>
            <th>Правила пересылки</th>
        </tr>
"""
        for idx, user in enumerate(delegated_or_forwarding_users, 1):
            name = user.get('name', {})
            full_name = f"{name.get('last', '')} {name.get('first', '')} {name.get('middle', '')}".strip()
            nickname = user.get('nickname', '')
            user_email = user.get('email', '')
            user_id = user.get('id', '')
            position = user.get('position', '')
            department = user.get('department', {}).get('name', '') if isinstance(user.get('department'), dict) else user.get('department', '')
            position_dept = f"{position}, {department}".strip(', ') if position or department else ''
            lock_date = user.get('isEnabledUpdatedAt', '')
            parsed_date = parse_date(lock_date)
            if parsed_date:
                lock_date_formatted = parsed_date.strftime('%d.%m.%Y %H:%M')
            else:
                lock_date_formatted = '<span class="info">не установлена</span>'
            
            is_delegated = "✅ Да" if user.get('isDelegated', False) else "❌ Нет"
            has_forwarding = "✅ Да" if user.get('hasForwardingRules', False) else "❌ Нет"
            
            html += f"""
        <tr>
            <td>{idx}</td>
            <td>{full_name}</td>
            <td>{nickname}</td>
            <td>{user_email}</td>
            <td>{position_dept}</td>
            <td>{lock_date_formatted}</td>
            <td>{user_id}</td>
            <td>{is_delegated}</td>
            <td>{has_forwarding}</td>
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
    Проверка и удаление пользователей, заблокированных дольше DELETE_AFTER_LOCKED_DAYS.
    
    Перед удалением проверяет подтверждение через IMAP почту.
    Удаляются только пользователи, для которых получено подтверждение.
    
    Args:
        settings: Настройки скрипта
        
    Returns:
        bool: True если проверка прошла успешно, False при ошибке
    """
    logger.info("=" * 80)
    logger.info("Запуск проверки: delete_users")
    logger.info("=" * 80)
    
    if not settings.delete_users:
        logger.info("DELETE_USERS=False. Удаление пользователей отключено. Пропуск.")
        return True
    
    # Получаем всех пользователей (deep copy для безопасной работы в асинхронном режиме)
    users_original = get_all_users(settings, force=True)
    if not users_original:
        logger.error("Не удалось получить список пользователей.")
        return False
    users = copy.deepcopy(users_original)
    
    # Находим заблокированных пользователей
    blocked_users = get_blocked_users(users)
    logger.info(f"Заблокированных пользователей: {len(blocked_users)}")
    
    # Обогащаем информацию о пользователях (isDelegated, hasForwardingRules)
    enrich_users(settings, blocked_users)
    
    # Находим пользователей для удаления
    all_users_for_deletion = get_users_for_deletion(blocked_users, settings.delete_after_locked_days, settings.value_for_empty_date)
    
    # Загружаем исключения и фильтруем
    exceptions_set = load_blocked_users_exceptions(settings)
    users_for_deletion, exception_users = filter_exception_users(all_users_for_deletion, exceptions_set)
    
    # Исключаем пользователей с делегированными ящиками или правилами пересылки
    delegated_or_forwarding_users = []
    filtered_users_for_deletion = []
    for user in users_for_deletion:
        if user.get('isDelegated', False) or user.get('hasForwardingRules', False):
            delegated_or_forwarding_users.append(user)
            reason = []
            if user.get('isDelegated', False):
                reason.append("делегированный ящик")
            if user.get('hasForwardingRules', False):
                reason.append("правила пересылки")
            logger.info(f"  [исключён из удаления - {', '.join(reason)}] {format_user_info(user)}")
        else:
            filtered_users_for_deletion.append(user)
    users_for_deletion = filtered_users_for_deletion
    
    if delegated_or_forwarding_users:
        logger.info(f"Исключено из удаления (делегирование/пересылка): {len(delegated_or_forwarding_users)}")
    
    if exception_users:
        logger.info(f"Пользователей-исключений (не подлежат удалению): {len(exception_users)}")
        for user in exception_users:
            logger.info(f"  [исключение] {format_user_info(user)}")
    
    if not users_for_deletion:
        logger.info(f"Нет пользователей, заблокированных дольше {settings.delete_after_locked_days} дней (после исключения исключений).")
        return True
    
    logger.info(f"Пользователей для удаления: {len(users_for_deletion)}")
    
    for user in users_for_deletion:
        logger.info(f"  - {format_user_info(user)}")
    
    # Читаем IMAP почту для получения подтверждений
    logger.info("Проверка подтверждений удаления через IMAP...")
    confirmed_users_set = read_imap_confirmation_emails(settings)
    
    # Разделяем пользователей на подтверждённых и неподтверждённых
    confirmed_for_deletion = []
    not_confirmed_users = []
    
    for user in users_for_deletion:
        if is_user_confirmed(user, confirmed_users_set):
            confirmed_for_deletion.append(user)
            logger.info(f"  ✓ Подтверждён: {format_user_info(user)}")
        else:
            not_confirmed_users.append(user)
            logger.warning(f"  ✗ Не подтверждён: {format_user_info(user)}")
    
    logger.info(f"Подтверждённых для удаления: {len(confirmed_for_deletion)}")
    logger.info(f"Без подтверждения: {len(not_confirmed_users)}")
    
    # Выполняем удаление только подтверждённых пользователей
    deleted_users = []
    failed_users = []
    
    if confirmed_for_deletion:
        if settings.dry_run:
            logger.warning("Режим DRY_RUN включён. Реальное удаление не будет выполнено.")
            deleted_users = confirmed_for_deletion
        else:
            for user in confirmed_for_deletion:
                user_id = user.get('id')
                success, _ = delete_user_by_api(settings, user_id)
                if success:
                    deleted_users.append(user)
                else:
                    failed_users.append(user)
    
    # Отправляем отчёт об удалении
    if (deleted_users or not_confirmed_users or delegated_or_forwarding_users) and settings.alert_emails:
        if deleted_users:
            subject = f"[Yandex 360] {'⚠️ Предупреждение' if settings.dry_run else '❌ Удаление'}: {len(deleted_users)} пользователей"
        else:
            subject = f"{settings.waiting_confirmation_subject}: {len(not_confirmed_users)} пользователей"
        
        html_body = generate_deletion_report_html(settings, deleted_users, not_confirmed_users, settings.dry_run, exception_users, delegated_or_forwarding_users)
        
        if send_email(settings, "", subject, html_body):
            logger.info(f"Отчёт об удалении отправлен на {', '.join(settings.alert_emails)}")
        else:
            logger.error("Не удалось отправить отчёт по email.")
    
    if failed_users:
        logger.error(f"Не удалось удалить {len(failed_users)} пользователей:")
        for user in failed_users:
            logger.error(f"  - {format_user_info(user)}")
    
    return True


def main():
    """Главная функция для запуска модуля напрямую."""
    logger.info("=" * 80)
    logger.info("Запуск модуля module_delete_users.py напрямую")
    logger.info(f"Время запуска: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    logger.info("=" * 80)
    
    settings = get_settings()
    if not settings:
        logger.error("Не удалось загрузить настройки. Завершение работы.")
        return
    
    run(settings)


if __name__ == "__main__":
    main()
