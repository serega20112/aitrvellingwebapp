import os
import json
import logging
import datetime
import functools
import sqlite3
import google.generativeai as genai
import wikipediaapi
from dotenv import load_dotenv
from duckduckgo_search import DDGS  # Добавляем для поиска через DuckDuckGo
from geopy.exc import GeocoderServiceError, GeocoderTimedOut
from geopy.geocoders import Nominatim
from werkzeug.security import check_password_hash, generate_password_hash
from flask import (
    Flask, Blueprint, flash, g, jsonify, redirect, render_template, request,
    session, url_for, current_app
)
from database import (
    init_app, get_db, close_db, init_db, get_cached_place_info, cache_place_info,
    get_user_by_id, get_user_by_username, add_user, get_all_interests,
    get_user_interest_ids, update_user_interests, get_visited_places,
    add_visited_place, get_user_interests
)

# Загрузка переменных окружения из .env
load_dotenv()


# --- Константы для форматов JSON ---
PLACE_INFO_EXPECTED_JSON_FORMAT = """
{
  "title": "string (Краткий, точный заголовок)",
  "description": "string (Увлекательный параграф, ~100-150 слов, с учетом интересов)",
  "details": ["string", "string", "..."] (Список ключевых фактов/особенностей),
  "ai_confidence": "'High', 'Medium', или 'Low' (Ваша уверенность в сгенерированной информации)",
  "sources": ["url1", "url2", ...] (URL использованных источников, если есть, кроме Википедии/гео)
}
"""

RECOMMENDATIONS_EXPECTED_JSON_FORMAT = """
[
    {
        "name": "string (Название рекомендуемого места)",
        "lat": float (Широта),
        "lng": float (Долгота),
        "reason": "string (Почему это соответствует интересам/профилю пользователя)",
        "tags": ["interest1", "interest2", ...] (Релевантные теги интересов)
    },
    ...
] (Верни 3-5 рекомендаций в виде JSON массива)
"""


# --- Вспомогательные функции ---
def get_location_details(latitude, longitude):
    """Получает адрес и имя по координатам. Вызывает исключения при ошибках."""
    print(f"Геокодирование координат: {latitude}, {longitude}")
    geolocator = Nominatim(
        user_agent=current_app.config['GEOPY_USER_AGENT'],
        timeout=current_app.config['GEOPY_TIMEOUT']
    )
    location = geolocator.reverse(
        (latitude, longitude), exactly_one=True, language='en'
    )

    if location and location.raw.get('address'):
        address_info = location.raw['address']
        components = [
            address_info.get('amenity'), address_info.get('historic'),
            address_info.get('tourism'), address_info.get('shop'),
            address_info.get('building'), address_info.get('road'),
            address_info.get('neighbourhood', address_info.get('suburb')),
            address_info.get('city', address_info.get('town', address_info.get('village'))),
            address_info.get('country')
        ]
        place_name = next((item for item in components if item), None)
        if not place_name:
            place_name = address_info.get(
                'city', address_info.get(
                    'town', address_info.get(
                        'village', address_info.get('country', 'Unknown Location')
                    )
                )
            )
        else:
            area = address_info.get(
                'city', address_info.get(
                    'town', address_info.get('village', address_info.get('country'))
                )
            )
            if area and area != place_name:
                place_name = f"{place_name}, {area}"

        print(f"Geocoded Name: {place_name}")
        return place_name, address_info
    else:
        print("Nominatim did not return a valid location or address.")
        default_place_name = f"Location at {latitude:.5f}, {longitude:.5f}"
        return default_place_name, {}


def get_wikipedia_info(place_name, latitude, longitude):
    """Получает сводку из Википедии. Вызывает исключения при ошибках."""
    print(f"Поиск в Википедии для: '{place_name}' или координат {latitude}, {longitude}")
    wiki_wiki = wikipediaapi.Wikipedia(
        current_app.config['WIKI_USER_AGENT'], 'en'
    )
    page = wiki_wiki.page(place_name.split(',')[0])  # Используем первую часть имени

    if page.exists():
        print(f"Найдена страница Википедии по имени: {page.title}")
        summary = page.summary[:700] + ("..." if len(page.summary) > 700 else "")
        return summary, page.fullurl

    print(f"Поиск по имени не удался. Пробуем геопоиск...")
    geo_search_results = wiki_wiki.find_pages_near_coord(latitude, longitude, radius=1000)

    if geo_search_results:
        closest_page_title = next(iter(geo_search_results))
        page = wiki_wiki.page(closest_page_title)
        if page.exists():
            print(f"Найдена близкая страница Википедии через геопоиск: {page.title}")
            summary = page.summary[:700] + ("..." if len(page.summary) > 700 else "")
            return summary, page.fullurl

    print("Релевантная страница Википедии не найдена.")
    return "Соответствующая статья в Википедии не найдена.", None


def search_web(query):
    """Поиск в интернете через DuckDuckGo."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        return [
            {'title': result['title'], 'link': result['href'], 'snippet': result['body']}
            for result in results
        ]
    except Exception as e:
        logging.error(f"Ошибка поиска через DuckDuckGo: {e}")
        return []


def call_ai_model(prompt, expected_json_format_description):
    """Вызывает Google Gemini AI. Вызывает исключения при ошибках."""
    print("\n--- Вызов Google Gemini AI ---")
    model_to_use = current_app.ai_model
    generation_config = current_app.generation_config

    if not model_to_use:
        raise RuntimeError("Модель Google Gemini не инициализирована.")

    print(f"Отправка запроса к модели: {model_to_use.model_name}")

    response = model_to_use.generate_content(
        prompt,
        generation_config=generation_config,
    )

    raw_response_content = ""
    if hasattr(response, 'text'):
        raw_response_content = response.text
    elif hasattr(response, 'parts') and response.parts:
        raw_response_content = "".join(part.text for part in response.parts if hasattr(part, 'text'))
    else:
        logging.error(f"Неожиданная структура ответа от Gemini: {response}")
        raise ValueError("Неожиданная структура ответа от Gemini")

    print(f"Сырой ответ от Gemini (ожидается JSON): {raw_response_content[:500]}...")

    try:
        ai_result_json = json.loads(raw_response_content)
        print("--- Ответ Gemini успешно распарсен как JSON ---")
        return ai_result_json
    except json.JSONDecodeError as e:
        logging.error(f"Ошибка парсинга JSON от Gemini: {e}")
        raise


def login_required(view):
    """Декоратор для требования аутентификации."""
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            flash('Для доступа к этой странице необходимо войти.', 'warning')
            return redirect(url_for('auth.login_route'))
        return view(**kwargs)
    return wrapped_view


def sanitize_mermaid_text(text):
    """Экранирует текст для Mermaid."""
    if not isinstance(text, str):
        text = str(text)
    return (
        text.replace(')', r'\)').replace('(', r'\(')
        .replace('"', '&quot;').replace('#', '')
        .replace('{', r'\{').replace('}', r'\}')
        .replace('[', r'\[').replace(']', r'\]')
        .strip()
    )


# --- Фабрика приложения Flask ---
def create_app(test_config=None):
    """Создает и конфигурирует экземпляр приложения Flask."""
    app = Flask(__name__, instance_relative_config=True)

    # --- Конфигурация по умолчанию ---
    app.config.from_mapping(
        SECRET_KEY=os.environ.get('SECRET_KEY', 'dev_secret_key_change_me_in_prod'),
        DATABASE=os.path.join(app.instance_path, 'main.db'),
        GOOGLE_API_KEY=os.environ.get("GOOGLE_API_KEY"),
        GOOGLE_GEMINI_MODEL=os.environ.get("GOOGLE_GEMINI_MODEL", "gemini-1.5-flash"),
        WIKI_USER_AGENT='InteractiveMapApp/1.3 (MyMapApp; contact@example.com)',
        GEOPY_USER_AGENT='InteractiveMapApp/1.3 (MyMapApp; contact@example.com)',
        GEOPY_TIMEOUT=10,
    )

    # Загрузка из instance/config.py
    app.config.from_pyfile('config.py', silent=True)

    # Применение тестовой конфигурации
    if test_config:
        app.config.update(test_config)

    # --- Настройка логирования ---
    logging.basicConfig(
        filename='app.log',
        level=logging.INFO,
        format='%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
    )

    # --- Критические проверки конфигурации ---
    if not app.config.get('GOOGLE_API_KEY'):
        logging.error("GOOGLE_API_KEY не установлен.")
        raise RuntimeError("GOOGLE_API_KEY не установлен.")

    # Убедимся, что папка instance существует
    os.makedirs(app.instance_path, exist_ok=True)

    # --- Инициализация клиента Google Gemini ---
    try:
        genai.configure(api_key=app.config['GOOGLE_API_KEY'])
        app.ai_model = genai.GenerativeModel(app.config['GOOGLE_GEMINI_MODEL'])
        app.generation_config = genai.GenerationConfig(response_mime_type="application/json")
        logging.info(f"Клиент Google Gemini сконфигурирован для модели: {app.config['GOOGLE_GEMINI_MODEL']}")
    except Exception as e:
        logging.error(f"Ошибка при конфигурации Google Gemini: {e}")
        raise

    # --- Инициализация базы данных ---
    init_app(app)

    with app.app_context():
        db_path = current_app.config['DATABASE']
        if not os.path.exists(db_path):
            logging.info(f"Файл базы данных не найден в {db_path}. Инициализация схемы...")
            init_db()

    # --- Blueprint для аутентификации ---
    auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

    @app.before_request
    def load_logged_in_user():
        """Загружает данные пользователя из сессии перед каждым запросом."""
        user_id = session.get('user_id')
        if user_id is None:
            g.user = None
        else:
            try:
                db = get_db()
                g.user = db.execute(
                    'SELECT id, username FROM users WHERE id = ?', (user_id,)
                ).fetchone()
                if g.user is None:
                    session.clear()
                    logging.warning(f"ID пользователя {user_id} из сессии не найден в БД.")
            except Exception as e:
                logging.error(f"Ошибка при загрузке пользователя {user_id} из БД: {e}")
                g.user = None

    # --- Маршруты аутентификации ---
    @auth_bp.route('/register', methods=('GET', 'POST'))
    def register_route():
        """Обрабатывает регистрацию пользователя."""
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            error = None
            db = get_db()

            if not username:
                error = 'Требуется имя пользователя.'
            elif not password:
                error = 'Требуется пароль.'
            else:
                existing_user = db.execute(
                    'SELECT id FROM users WHERE username = ?', (username,)
                ).fetchone()
                if existing_user:
                    error = f"Имя пользователя '{username}' уже занято."

            if error is None:
                try:
                    db.execute(
                        "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                        (username, generate_password_hash(password)),
                    )
                    db.commit()
                    logging.info(f"Пользователь '{username}' успешно зарегистрирован.")
                    flash('Регистрация прошла успешно! Пожалуйста, войдите.', 'success')
                    return redirect(url_for("auth.login_route"))
                except sqlite3.Error as e:
                    error = f"Ошибка базы данных при регистрации: {e}"
                    logging.error(error)

            if error:
                flash(error, 'danger')

        return render_template('register.html')

    @auth_bp.route('/login', methods=('GET', 'POST'))
    def login_route():
        """Обрабатывает вход пользователя."""
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            error = None
            user = None

            if not username:
                error = 'Требуется имя пользователя.'
            elif not password:
                error = 'Требуется пароль.'

            if error is None:
                try:
                    db = get_db()
                    user = db.execute(
                        'SELECT * FROM users WHERE username = ?', (username,)
                    ).fetchone()
                    if user is None or not check_password_hash(user['password_hash'], password):
                        error = 'Неверное имя пользователя или пароль.'
                except sqlite3.Error as e:
                    error = f"Ошибка базы данных при входе: {e}"
                    logging.error(error)

            if error is None and user:
                session.clear()
                session['user_id'] = user['id']
                session['username'] = user['username']
                g.user = user
                logging.info(f"Пользователь '{user['username']}' (ID: {user['id']}) вошел.")
                flash(f'Добро пожаловать, {user["username"]}!', 'success')
                return redirect(url_for('index'))
            else:
                flash(error or 'Неизвестная ошибка входа.', 'danger')

        return render_template('login.html', user=g.user)

    @auth_bp.route('/logout')
    def logout_route():
        """Обрабатывает выход пользователя."""
        username = session.get('username', 'Пользователь')
        session.clear()
        g.user = None
        flash(f'{username}, вы успешно вышли.', 'info')
        logging.info(f"Пользователь '{username}' вышел.")
        return redirect(url_for('index'))

    @auth_bp.route('/profile', methods=('GET', 'POST'))
    @login_required
    def profile_route():
        """Отображает профиль и обрабатывает обновление интересов."""
        user_id = g.user['id']
        db = get_db()

        if request.method == 'POST':
            selected_interest_ids = request.form.getlist('interest_ids', type=int)
            try:
                if update_user_interests(user_id, selected_interest_ids):
                    logging.info(f"Интересы пользователя ID {user_id} обновлены: {selected_interest_ids}")
                    flash('Интересы успешно обновлены!', 'success')
                else:
                    flash('Произошла ошибка при обновлении интересов.', 'danger')
                return redirect(url_for('auth.profile_route'))
            except Exception as e:
                logging.error(f"Неожиданная ошибка при вызове update_user_interests: {e}")
                flash('Произошла серьезная ошибка при обновлении интересов.', 'danger')

        try:
            all_interests = get_all_interests()
            user_interest_ids = get_user_interest_ids(user_id)
        except Exception as e:
            logging.error(f"Ошибка при получении данных для профиля: {e}")
            flash(f"Не удалось загрузить данные профиля: {e}", 'danger')
            all_interests = []
            user_interest_ids = set()

        return render_template(
            'profile.html',
            user=g.user,
            all_interests=all_interests,
            user_interest_ids=user_interest_ids
        )

    app.register_blueprint(auth_bp)

    # --- Основные маршруты приложения ---
    @app.route('/')
    def index():
        """Рендерит главную страницу карты."""
        return render_template('index.html', user=g.user)

    @app.route('/get-place-info', methods=['POST'])
    def get_place_info_route():
        """Обрабатывает запрос информации о месте по координатам."""
        if not request.json or 'lat' not in request.json or 'lng' not in request.json:
            return jsonify({"error": "Отсутствуют координаты"}), 400

        lat = request.json['lat']
        lng = request.json['lng']
        user_id = g.user['id'] if g.user else None
        interests = get_user_interests(user_id) if user_id else []

        logging.info(f"Запрос инфо: Lat={lat}, Lng={lng}, UserID={user_id}, Interests={interests}")

        cached_data = get_cached_place_info(lat, lng)
        if cached_data:
            logging.info("Возврат из кэша.")
            cached_data['requested_lat'] = lat
            cached_data['requested_lng'] = lng
            return jsonify(cached_data)

        try:
            logging.info("Получение деталей местоположения...")
            place_name, address_info = get_location_details(lat, lng)

            logging.info("Получение информации из Википедии...")
            wiki_summary, wiki_url = get_wikipedia_info(place_name, lat, lng)

            logging.info("Поиск в интернете...")
            web_results = search_web(place_name)

            logging.info("Подготовка промпта и вызов AI...")
            prompt = f"""
            Проанализируй местоположение: Lat={lat}, Lng={lng}.
            Вероятное название/район: {place_name}
            Детали адреса: {json.dumps(address_info) if address_info else 'Недоступно'}
            Сводка из Википедии: {wiki_summary}
            Результаты поиска в интернете: {json.dumps(web_results)}
            Интересы пользователя: {', '.join(interests) if interests else 'Нет'}.

            Задача: Сгенерируй краткое, увлекательное описание, фокусируясь на аспектах, релевантных интересам пользователя. Если точные координаты неинтересны, опиши ближайшую релевантную точку интереса или общий характер местности, упоминая интересы пользователя.

            Вывод ДОЛЖЕН быть ЕДИНСТВЕННЫМ, валидным JSON объектом, соответствующим этому описанию структуры:
            {PLACE_INFO_EXPECTED_JSON_FORMAT}
            Адаптируй поля 'description' и 'details' под указанные Интересы пользователя. Включи '{wiki_url}' в 'sources', если он доступен и релевантен. Не добавляй никаких вводных фраз типа "Вот JSON:" или markdown разметку ```json ... ```.
            """
            ai_result = call_ai_model(prompt, PLACE_INFO_EXPECTED_JSON_FORMAT)

            if isinstance(ai_result, dict):
                ai_result['requested_lat'] = lat
                ai_result['requested_lng'] = lng
                ai_result['identified_place_name'] = place_name
                ai_result['wikipedia_summary'] = wiki_summary
                ai_result['web_results'] = web_results
                if wiki_url and 'sources' in ai_result and isinstance(ai_result['sources'], list) and wiki_url not in ai_result['sources']:
                    ai_result['sources'].insert(0, wiki_url)
                elif wiki_url and ('sources' not in ai_result or not isinstance(ai_result.get('sources'), list)):
                    ai_result['sources'] = [wiki_url]

                logging.info("Кэширование успешного результата AI.")
                cache_place_info(lat, lng, ai_result)

                return jsonify(ai_result)
            else:
                logging.error("Результат AI не является словарем после успешного вызова.")
                raise ValueError("Неожиданный тип результата от AI")

        except Exception as e:
            logging.error(f"Ошибка при обработке /get-place-info: {type(e).__name__}: {e}", exc_info=True)
            return jsonify({"error": "Внутренняя ошибка сервера", "details": str(e)}), 500

    @app.route('/get-recommendations', methods=['GET'])
    def get_recommendations_route():
        """Генерирует рекомендации с помощью AI."""
        user_id = g.user['id'] if g.user else None
        if not user_id:
            return jsonify({"error": "Требуется вход для получения рекомендаций"}), 401

        try:
            interests = get_user_interests(user_id)
            visited = get_visited_places(user_id)

            logging.info(f"Генерация рекомендаций для UserID={user_id}, Interests={interests}, VisitedCount={len(visited)}")

            visited_summary = [
                f"'{v.get('place_name', 'Unknown')}' ({v['lat']:.3f},{v['lng']:.3f})"
                for v in visited[:10]
            ]

            rec_prompt = f"""
            Основываясь на профиле пользователя, сгенерируй 3-5 рекомендаций для путешествий в виде JSON массива.

            Профиль пользователя:
            - Интересы: {', '.join(interests) if interests else 'Не указаны'}
            - Недавно посещенные места (часть): {'; '.join(visited_summary) if visited_summary else 'Нет записей'}

            Сгенерируй JSON массив объектов, соответствующий этой структуре:
            {RECOMMENDATIONS_EXPECTED_JSON_FORMAT}

            Требования:
            - Рекомендуй места, которые пользователь недавно не посещал (если возможно).
            - Рекомендации должны соответствовать интересам пользователя.
            - Укажи краткую причину (`reason`), объясняющую релевантность.
            - Включи географические координаты (`lat`, `lng`).
            - Включи релевантные теги (`tags`) из списка интересов пользователя.
            - Фокусируйся на разнообразных и интересных локациях.

            Верни ТОЛЬКО валидный JSON массив, без вводного текста или markdown.
            """

            recommendations_result = call_ai_model(rec_prompt, RECOMMENDATIONS_EXPECTED_JSON_FORMAT)

            if isinstance(recommendations_result, list):
                return jsonify(recommendations_result)
            else:
                logging.error(f"Результат рекомендаций AI не является списком: {type(recommendations_result)}")
                raise ValueError("Неожиданный тип результата рекомендаций от AI")

        except Exception as e:
            logging.error(f"Ошибка при обработке /get-recommendations: {type(e).__name__}: {e}", exc_info=True)
            return jsonify({"error": "Не удалось сгенерировать рекомендации", "details": str(e)}), 500

    @app.route('/generate-scheme', methods=['POST'])
    def generate_scheme_route():
        """Генерирует данные для Mermaid mind map."""
        if not request.json or 'place_data' not in request.json:
            return jsonify({"error": "Отсутствуют данные о месте"}), 400

        place_data = request.json['place_data']

        if not isinstance(place_data, dict) or place_data.get("error"):
            return jsonify({"error": "Невозможно сгенерировать схему из неверных данных"}), 400

        user_id = g.user['id'] if g.user else None
        interests = get_user_interests(user_id) if user_id else []

        title = place_data.get('title', 'Местоположение')
        description = place_data.get('description', '')
        details = place_data.get('details', [])
        sources = place_data.get('sources', [])
        confidence = place_data.get('ai_confidence', 'Неизвестно')
        wiki_summary = place_data.get('wikipedia_summary', '')
        web_results = place_data.get('web_results', [])

        logging.info(f"Генерация схемы для: '{title}', Уверенность: {confidence}")

        mermaid_string = f"""mindmap
  root(({sanitize_mermaid_text(title)}))
    (AI Confidence: {sanitize_mermaid_text(confidence)})
"""
        if description:
            mermaid_string += f"    (Desc: {sanitize_mermaid_text(description[:80])}...)\n"

        if details:
            mermaid_string += f"    ::icon(fa fa-list-ul) Key Details\n"
            for detail in details:
                if isinstance(detail, str) and detail.strip():
                    mermaid_string += f"      - {sanitize_mermaid_text(detail[:100])}\n"

        if wiki_summary and wiki_summary != "Соответствующая статья в Википедии не найдена.":
            mermaid_string += f"    ::icon(fa fa-wikipedia-w) Wikipedia Summary\n"
            mermaid_string += f"      ... {sanitize_mermaid_text(wiki_summary[:150])} ...\n"

        if web_results:
            mermaid_string += f"    ::icon(fa fa-globe) Web Results\n"
            for result in web_results[:3]:
                mermaid_string += f"      > {sanitize_mermaid_text(result.get('title', 'No title')[:60])}...\n"

        related_interests = []
        combined_text_lower = (description + " " + " ".join(map(str, details)) + " " + wiki_summary).lower()
        for interest in interests:
            if interest.lower() in combined_text_lower:
                related_interests.append(interest)

        if related_interests:
            mermaid_string += f"    ::icon(fa fa-star) Related User Interests\n"
            for interest in related_interests:
                mermaid_string += f"      * {sanitize_mermaid_text(interest.capitalize())}\n"

        valid_sources = [s for s in sources if isinstance(s, str) and s.strip() and s != 'N/A']
        if valid_sources:
            mermaid_string += f"    ::icon(fa fa-link) Sources\n"
            for source in valid_sources:
                if source.startswith(('http://', 'https://')):
                    try:
                        domain_parts = source.split('/')
                        domain = domain_parts[2] if len(domain_parts) > 2 else source
                        safe_source_url = source.replace('"', '&quot;')
                        mermaid_string += f'      > <a href="{safe_source_url}" target="_blank">{sanitize_mermaid_text(domain)}</a>\n'
                    except IndexError:
                        mermaid_string += f"      > {sanitize_mermaid_text(source[:60])}...\n"
                else:
                    mermaid_string += f"      > {sanitize_mermaid_text(source[:60])}...\n"

        scheme_data = {
            "type": "mermaid_mindmap",
            "data": mermaid_string.strip()
        }
        return jsonify(scheme_data)

    @app.context_processor
    def inject_now():
        """Делает переменную 'now' доступной во всех шаблонах."""
        return {'now': datetime.datetime.utcnow()}

    return app


# --- Точка входа для запуска ---
if __name__ == '__main__':
    flask_app = create_app()
    host = os.environ.get('FLASK_HOST', '127.0.0.1')
    port = int(os.environ.get('FLASK_PORT', 5000))
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() in ['true', '1', 'yes']

    logging.info("--- Starting Flask Application ---")
    logging.info(f"Configuration: Debug Mode: {'ON' if debug_mode else 'OFF'}, Host: {host}, Port: {port}, AI Model: {flask_app.config.get('GOOGLE_GEMINI_MODEL')}, Database: {flask_app.config.get('DATABASE')}")
    if debug_mode:
        logging.warning("DEBUG MODE IS ON. DO NOT USE IN PRODUCTION.")
    logging.info(f"Access the app at: http://{host}:{port}")

    flask_app.run(host=host, port=port, debug=debug_mode)