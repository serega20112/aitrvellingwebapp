# database.py

import sqlite3
import click
import json
import datetime
import sys
from flask import current_app, g

# --- Управление Соединением с БД ---

def get_db():
    """
    Возвращает соединение с БД для текущего запроса.
    Создает новое соединение, если его еще нет.
    """
    if 'db' not in g:
        try:
            g.db = sqlite3.connect(
                current_app.config['DATABASE'],
                detect_types=sqlite3.PARSE_DECLTYPES # Автоматическое определение типов
            )
            # Возвращать строки как объекты, к которым можно обращаться по имени колонки
            g.db.row_factory = sqlite3.Row
            print("Соединение с БД установлено.")
        except sqlite3.Error as e:
            print(f"ОШИБКА: Не удалось подключиться к базе данных {current_app.config['DATABASE']}: {e}", file=sys.stderr)
            # В реальном приложении здесь может быть более сложная обработка ошибок
            raise # Передаем исключение дальше, чтобы Flask мог его обработать
    return g.db

def close_db(e=None):
    """Закрывает соединение с БД, если оно было открыто."""
    db = g.pop('db', None)
    if db is not None:
        db.close()
        print("Соединение с БД закрыто.")

# --- Инициализация БД ---

def init_db():
    """Удаляет существующие данные и создает новые таблицы на основе schema.sql."""
    try:
        db = get_db()
        # Получаем путь к файлу schema.sql из папки приложения
        with current_app.open_resource('schema.sql') as f:
            # Выполняем все SQL команды из файла
            db.executescript(f.read().decode('utf8'))
        print("База данных успешно инициализирована схемой из schema.sql.")
    except FileNotFoundError:
         print("ОШИБКА: Файл schema.sql не найден в корневой папке приложения.", file=sys.stderr)
    except sqlite3.Error as e:
        print(f"ОШИБКА при выполнении schema.sql: {e}", file=sys.stderr)
        # Проверяем, возможно, таблицы уже существуют
        try:
            cursor = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users';")
            if cursor.fetchone():
                print("Похоже, таблицы базы данных уже существуют.")
            else:
                raise e # Повторно вызываем исходную ошибку, если таблицы не найдены
        except Exception as check_e:
             print(f"Не удалось проверить существующие таблицы: {check_e}", file=sys.stderr)
             raise e # Повторно вызываем исходную ошибку

@click.command('init-db')
def init_db_command():
    """Flask CLI команда для инициализации базы данных."""
    try:
        # Убедимся, что выполняемся в контексте приложения для доступа к config и g
        from flask import current_app
        with current_app.app_context():
             init_db()
        click.echo('База данных успешно инициализирована.')
    except Exception as e:
        click.echo(f'Ошибка при инициализации базы данных: {e}', err=True)


def init_app(app):
    """Регистрирует функции управления БД в Flask приложении."""
    # Говорит Flask вызывать close_db при очистке после возврата ответа
    app.teardown_appcontext(close_db)
    # Добавляет команду 'flask init-db' в CLI
    if click: # Проверяем, установлен ли click
        app.cli.add_command(init_db_command)
    else:
        print("Предупреждение: библиотека 'click' не найдена, команда 'flask init-db' будет недоступна.")


# --- Функции Кэширования ---

def get_cached_place_info(lat, lng, max_age_seconds=3600 * 6): # Кэш на 6 часов по умолчанию
    """Извлекает кэшированную информацию о месте, если она существует и не устарела."""
    db = get_db()
    # Создаем стабильный ключ кэша с фиксированной точностью координат
    cache_key = f"{lat:.6f}_{lng:.6f}"
    try:
        cursor = db.execute(
            'SELECT json_result, timestamp FROM cache WHERE cache_key = ?', (cache_key,)
        )
        row = cursor.fetchone()
        if row:
            cached_time = datetime.datetime.fromisoformat(row['timestamp'])
            # Используем UTC для сравнения времени
            if datetime.datetime.now(datetime.timezone.utc) - cached_time < datetime.timedelta(seconds=max_age_seconds):
                print(f"Кэш HIT для ключа: {cache_key}")
                return json.loads(row['json_result']) # Возвращаем распарсенный JSON
            else:
                print(f"Кэш STALE для ключа: {cache_key}")
                # Можно добавить удаление устаревшей записи здесь:
                # db.execute('DELETE FROM cache WHERE cache_key = ?', (cache_key,))
                # db.commit()
        else:
            print(f"Кэш MISS для ключа: {cache_key}")
    except sqlite3.Error as e:
        print(f"SQLite ошибка при чтении кэша для ключа {cache_key}: {e}", file=sys.stderr)
    except json.JSONDecodeError as e:
        print(f"Ошибка декодирования JSON из кэша для ключа {cache_key}: {e}", file=sys.stderr)
        # Возможно, стоит удалить поврежденную запись?
    except Exception as e:
        print(f"Неожиданная ошибка при чтении кэша для ключа {cache_key}: {e}", file=sys.stderr)
    return None # Возвращаем None в случае ошибки или отсутствия/устаревания кэша

def cache_place_info(lat, lng, json_result):
    """Сохраняет информацию о месте в кэш."""
    if not isinstance(json_result, (dict, list)): # Проверяем базовую валидность данных
        print(f"Попытка кэшировать невалидные данные (не dict/list) для {lat},{lng}. Пропуск.", file=sys.stderr)
        return

    db = get_db()
    cache_key = f"{lat:.6f}_{lng:.6f}"
    try:
        # Используем UTC время для временной метки
        timestamp_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        db.execute(
            # INSERT OR REPLACE атомарно заменит запись, если ключ уже существует
            'INSERT OR REPLACE INTO cache (cache_key, json_result, timestamp) VALUES (?, ?, ?)',
            (cache_key, json.dumps(json_result), timestamp_iso)
        )
        db.commit() # Подтверждаем транзакцию
        print(f"Результат для ключа {cache_key} успешно закэширован.")
    except sqlite3.Error as e:
        print(f"SQLite ошибка при записи в кэш для ключа {cache_key}: {e}", file=sys.stderr)
    except Exception as e:
        print(f"Неожиданная ошибка при записи в кэш для ключа {cache_key}: {e}", file=sys.stderr)


# --- Функции для Пользователей ---

def get_user_by_id(user_id):
    """Получает данные пользователя по его ID."""
    db = get_db()
    try:
        user = db.execute(
            'SELECT * FROM users WHERE id = ?', (user_id,)
        ).fetchone()
        return user # Возвращает sqlite3.Row или None
    except sqlite3.Error as e:
        print(f"Ошибка SQLite при поиске пользователя по ID {user_id}: {e}", file=sys.stderr)
        return None

def get_user_by_username(username):
    """Получает данные пользователя по его имени."""
    db = get_db()
    try:
        user = db.execute(
            'SELECT * FROM users WHERE username = ?', (username,)
        ).fetchone()
        return user # Возвращает sqlite3.Row или None
    except sqlite3.Error as e:
        print(f"Ошибка SQLite при поиске пользователя по имени '{username}': {e}", file=sys.stderr)
        return None

def add_user(username, password_hash):
    """Добавляет нового пользователя в БД. Пароль должен быть уже хеширован."""
    db = get_db()
    try:
        cursor = db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, password_hash),
        )
        db.commit()
        return cursor.lastrowid # Возвращаем ID нового пользователя
    except sqlite3.IntegrityError:
        # Возникает, если username уже занят (из-за UNIQUE constraint)
        print(f"Ошибка добавления: Пользователь '{username}' уже существует.")
        return None # Явно возвращаем None при дубликате
    except sqlite3.Error as e:
        print(f"Ошибка SQLite при добавлении пользователя '{username}': {e}", file=sys.stderr)
        return None

# --- Функции для Интересов ---

def get_all_interests():
    """Возвращает список всех доступных интересов."""
    db = get_db()
    try:
        interests = db.execute(
            "SELECT id, name FROM interests ORDER BY name COLLATE NOCASE" # Сортировка без учета регистра
        ).fetchall()
        return interests # Возвращает список sqlite3.Row
    except sqlite3.Error as e:
        print(f"Ошибка SQLite при получении списка всех интересов: {e}", file=sys.stderr)
        return [] # Возвращаем пустой список при ошибке

def get_user_interest_ids(user_id):
    """Возвращает множество (set) ID интересов для указанного пользователя."""
    db = get_db()
    interest_ids = set()
    try:
        cursor = db.execute(
            "SELECT interest_id FROM user_interests WHERE user_id = ?",
            (user_id,)
        )
        for row in cursor:
            interest_ids.add(row['interest_id'])
        return interest_ids
    except sqlite3.Error as e:
        print(f"Ошибка SQLite при получении ID интересов для пользователя {user_id}: {e}", file=sys.stderr)
        return set() # Возвращаем пустое множество при ошибке

def update_user_interests(user_id, interest_ids):
    """Обновляет интересы пользователя. Удаляет старые, добавляет новые."""
    db = get_db()
    # Используем транзакцию для атомарности операции
    try:
        with db:
            # 1. Удаляем все текущие интересы пользователя
            db.execute("DELETE FROM user_interests WHERE user_id = ?", (user_id,))
            print(f"Старые интересы для user_id {user_id} удалены.")

            # 2. Вставляем новые интересы, если они есть
            if interest_ids: # Вставляем только если список ID не пуст
                # Готовим данные для множественной вставки
                # Убедимся, что все ID - целые числа
                values_to_insert = [(user_id, int(interest_id)) for interest_id in interest_ids]
                # Выполняем множественную вставку
                db.executemany(
                    "INSERT INTO user_interests (user_id, interest_id) VALUES (?, ?)",
                    values_to_insert
                )
                print(f"Новые интересы {interest_ids} для user_id {user_id} добавлены.")
            else:
                 print(f"Нет новых интересов для добавления для user_id {user_id}.")
        return True # Успешно
    except sqlite3.Error as e:
        print(f"Ошибка SQLite при обновлении интересов для пользователя {user_id}: {e}", file=sys.stderr)
        # db.rollback() # Не нужно с 'with db:', откат произойдет автоматически при исключении
        return False # Неудача
    except ValueError as e:
         print(f"Ошибка преобразования ID интереса в число для пользователя {user_id}: {e}", file=sys.stderr)
         return False
    except Exception as e:
        print(f"Неожиданная ошибка при обновлении интересов для пользователя {user_id}: {e}", file=sys.stderr)
        return False

# --- Функции для Посещенных Мест ---

def get_visited_places(user_id):
    """Возвращает список посещенных мест для пользователя."""
    db = get_db()
    try:
        places = db.execute(
            "SELECT place_name, latitude, longitude, visited_at FROM visited_places WHERE user_id = ? ORDER BY visited_at DESC",
            (user_id,)
        ).fetchall()
        # Возвращаем список sqlite3.Row, чтобы можно было обращаться по именам колонок
        return places
    except sqlite3.Error as e:
        print(f"Ошибка SQLite при получении посещенных мест для пользователя {user_id}: {e}", file=sys.stderr)
        return [] # Возвращаем пустой список при ошибке

def add_visited_place(user_id, place_name, lat, lng):
    """Добавляет запись о посещенном месте."""
    db = get_db()
    try:
        # Используем UTC время для метки посещения
        visited_time_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        db.execute(
            "INSERT INTO visited_places (user_id, place_name, latitude, longitude, visited_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, place_name, lat, lng, visited_time_iso)
        )
        db.commit()
        print(f"Посещенное место '{place_name}' добавлено для пользователя {user_id}.")
        return True
    except sqlite3.Error as e:
        print(f"Ошибка SQLite при добавлении посещенного места '{place_name}' для пользователя {user_id}: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Неожиданная ошибка при добавлении посещенного места для пользователя {user_id}: {e}", file=sys.stderr)
        return False
# --- Add this function to your database.py ---

def get_user_interests(user_id):
    """
    Возвращает список имен интересов для указанного пользователя.
    """
    db = get_db()
    interest_names = []
    if not user_id: # Handle cases where user_id might be None (e.g., anonymous user)
        return interest_names

    try:
        # Используем JOIN для получения имен интересов напрямую по user_id
        cursor = db.execute(
            """
            SELECT i.name
            FROM interests i
            JOIN user_interests ui ON i.id = ui.interest_id
            WHERE ui.user_id = ?
            ORDER BY i.name COLLATE NOCASE
            """,
            (user_id,)
        )
        # Извлекаем только имена из результатов
        interest_names = [row['name'] for row in cursor.fetchall()]
        return interest_names
    except sqlite3.Error as e:
        print(f"Ошибка SQLite при получении имен интересов для пользователя {user_id}: {e}", file=sys.stderr)
        return [] # Возвращаем пустой список при ошибке
    except Exception as e:
        print(f"Неожиданная ошибка при получении имен интересов для пользователя {user_id}: {e}", file=sys.stderr)
        return []
