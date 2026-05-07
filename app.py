import os
import random
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash
from openai import OpenAI

# Всегда загружаем .env из папки проекта, даже если запуск был не из корня.
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=BASE_DIR / ".env")
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key")
DB_PATH = BASE_DIR / "app.db"


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_text TEXT NOT NULL,
                product_url TEXT,
                tone TEXT,
                created_at_utc TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vk_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_text TEXT NOT NULL,
                product_url TEXT,
                tone TEXT,
                vk_post_id INTEGER,
                vk_owner_id INTEGER,
                status TEXT NOT NULL,
                publish_at_utc TEXT,
                created_at_utc TEXT NOT NULL,
                error TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_local_datetime_to_utc_iso(value: str) -> str | None:
    """
    Ожидаем формат input[type=datetime-local]: YYYY-MM-DDTHH:MM
    Считаем время локальным временем машины, переводим в UTC ISO.
    """
    if not value:
        return None
    try:
        local_naive = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None

    # Наивное время трактуем как локальное; переводим в UTC через timestamp.
    # (Это работает корректно для локального часового пояса ОС.)
    ts = local_naive.timestamp()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _vk_config():
    token = os.getenv("VK_ACCESS_TOKEN", "").strip()
    group_id_raw = os.getenv("VK_GROUP_ID", "").strip()
    api_version = os.getenv("VK_API_VERSION", "5.131").strip() or "5.131"

    group_id = None
    if group_id_raw.isdigit():
        group_id = int(group_id_raw)

    return token, group_id, api_version


def vk_wall_post(message: str, publish_at_utc_iso: str | None = None):
    token, group_id, api_version = _vk_config()
    if not token or not group_id:
        raise ValueError("Не настроен VK: добавьте VK_ACCESS_TOKEN и VK_GROUP_ID в .env")

    owner_id = -group_id  # Постинг от имени сообщества
    payload = {
        "access_token": token,
        "v": api_version,
        "owner_id": owner_id,
        "from_group": 1,
        "message": message,
    }

    if publish_at_utc_iso:
        try:
            publish_dt = datetime.fromisoformat(publish_at_utc_iso.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Некорректная дата/время публикации")

        publish_ts = int(publish_dt.replace(tzinfo=timezone.utc).timestamp())
        now_ts = int(datetime.now(timezone.utc).timestamp())
        if publish_ts <= now_ts:
            raise ValueError("Время отложенной публикации должно быть в будущем")

        # VK требует минимум ~10 минут для publish_date (может меняться), предупредим аккуратно.
        if publish_ts - now_ts < 600:
            raise ValueError("Для отложенного поста выберите время минимум через 10 минут")

        payload["publish_date"] = publish_ts

    resp = requests.post("https://api.vk.com/method/wall.post", data=payload, timeout=15)
    data = resp.json()
    if "error" in data:
        err = data["error"]
        msg = err.get("error_msg", "VK error")
        raise ValueError(f"VK: {msg}")

    response = data.get("response") or {}
    return {
        "vk_post_id": response.get("post_id"),
        "vk_owner_id": response.get("owner_id", owner_id),
    }


def normalize_post_for_vk(text: str) -> str:
    """
    Убираем markdown-разметку и префиксы заголовка,
    чтобы пост в VK начинался с нормального текста.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return cleaned

    # Удаляем markdown-заголовки в начале строк: ###, ##, #
    cleaned = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", cleaned)

    # Удаляем маркеры вроде "Заголовок:" в начале первой строки.
    cleaned = re.sub(r"(?im)^\s*\*{0,2}\s*заголовок\s*:\s*\*{0,2}\s*", "", cleaned, count=1)

    # Схлопываем слишком много пустых строк.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def get_voice_settings():
    """
    Настройки стиля генератора.
    Здесь можно менять:
    - тон
    - длину поста
    - количество эмодзи
    - стиль заголовка
    - стиль CTA и хэштегов
    """
    return {
        "tone": "дружелюбный, интересный и продающий",
        "length": "средняя (700-1000 символов)",
        "emoji_count": "умеренно, но заметно (4-7)",
        "headline_style": "цепляющий и конкретный",
        "cta_style": "мягкий, но мотивирующий",
        "hashtags_style": "4-6 релевантных хэштегов",
    }


def extract_text_from_url(product_url: str) -> str:
    """
    Пытаемся извлечь краткий контекст со страницы товара:
    - title
    - meta description
    - первые абзацы
    Если сайт недоступен/закрыт, модель все равно попробует сгенерировать пост по ссылке.
    """
    try:
        response = requests.get(
            product_url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (PostGeneratorBot/1.0)"},
        )
        response.raise_for_status()
    except requests.RequestException:
        return ""

    soup = BeautifulSoup(response.text, "html.parser")
    title = (soup.title.string or "").strip() if soup.title else ""

    meta_description_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = ""
    if meta_description_tag:
        meta_description = (meta_description_tag.get("content") or "").strip()

    paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    short_paragraphs = [text for text in paragraphs if len(text) > 40][:5]

    parts = []
    if title:
        parts.append(f"Title: {title}")
    if meta_description:
        parts.append(f"Meta description: {meta_description}")
    if short_paragraphs:
        parts.append("Text:\n" + "\n".join(short_paragraphs))

    return "\n\n".join(parts).strip()


def generate_post_with_ai(product_url: str, page_context: str, selected_tone: str) -> str:
    """
    Генерация поста через OpenAI.
    Ключ берется из .env: OPENAI_API_KEY
    Модель: gpt-4o-mini
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("Не найден OPENAI_API_KEY. Добавьте ключ в файл .env")

    voice = get_voice_settings()
    client = OpenAI(api_key=api_key)

    # Каждый запуск выбирает новую эмоцию, чтобы посты отличались по настроению.
    emotion = random.choice(["вдохновляющее", "энергичное", "дружелюбное", "экспертное", "теплое"])

    system_prompt = f"""
Ты копирайтер для e-commerce. Пиши на русском языке.
Сгенерируй оригинальный, интересный пост с разной эмоцией.

Требования к структуре:
1) Заголовок
2) Основной текст
3) Польза товара для покупателя
4) Хэштеги
5) Призыв к действию

Требования к стилю:
- Тон (обязательно соблюдай): {selected_tone}
- Базовый стиль бренда: {voice["tone"]}
- Длина: {voice["length"]}
- Эмодзи: {voice["emoji_count"]}
- Стиль заголовка: {voice["headline_style"]}
- Стиль CTA: {voice["cta_style"]}
- Хэштеги: {voice["hashtags_style"]}
- Эмоция текущего поста: {emotion}
- Не выдумывай технические характеристики, если их нет в источнике.
- Если данных мало, делай аккуратные формулировки без конкретных неподтвержденных цифр.
- Не используй markdown-разметку: никакие #, ##, ###, **, списки вида 1) 2) 3).
- Не пиши служебные префиксы вроде "Заголовок:", "Основной текст:", "CTA:", "Хэштеги:".
- Выводи как обычный живой пост: первая строка — заголовок обычным текстом.
"""

    user_prompt = f"""
Ссылка на товар:
{product_url}

Извлеченный контекст со страницы (если есть):
{page_context if page_context else "Контекст не извлечен. Используй доступные данные по ссылке и нейтральные формулировки."}
"""

    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=1.0,  # Чуть выше, чтобы тексты были более оригинальные.
        messages=[
            {"role": "system", "content": system_prompt.strip()},
            {"role": "user", "content": user_prompt.strip()},
        ],
    )
    return completion.choices[0].message.content.strip()


@app.route("/", methods=["GET", "POST"])
def index():
    generated_post = ""
    error_message = ""

    form_data = {
        "product_url": "",
        "tone": "дружелюбный",
    }

    if request.method == "POST":
        form_data["product_url"] = request.form.get("product_url", "").strip()
        form_data["tone"] = request.form.get("tone", "дружелюбный").strip()

        allowed_tones = {"дружелюбный", "с юмором", "экспертный"}
        if form_data["tone"] not in allowed_tones:
            form_data["tone"] = "дружелюбный"

        if not form_data["product_url"]:
            error_message = "Добавьте ссылку на товар"
        elif not form_data["product_url"].startswith(("http://", "https://")):
            error_message = "Ссылка должна начинаться с http:// или https://"
        else:
            try:
                context = extract_text_from_url(form_data["product_url"])
                generated_post = generate_post_with_ai(
                    form_data["product_url"],
                    context,
                    form_data["tone"],
                )
            except ValueError as error:
                error_message = str(error)
            except Exception as error:
                # Показываем тип ошибки, чтобы проще диагностировать проблему с ключом/лимитами/сетью.
                error_message = f"Не удалось сгенерировать пост ({error.__class__.__name__}). Проверьте ключ OpenAI, баланс и сеть."

    # Данные для интерфейса: избранное и последние публикации/отложенные
    conn = _db()
    try:
        favorites = conn.execute(
            "SELECT id, post_text, product_url, tone, created_at_utc FROM favorites ORDER BY id DESC LIMIT 20"
        ).fetchall()
        vk_posts = conn.execute(
            """
            SELECT id, post_text, vk_post_id, vk_owner_id, status, publish_at_utc, created_at_utc, error
            FROM vk_posts
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()
    finally:
        conn.close()

    return render_template(
        "index.html",
        generated_post=generated_post,
        error_message=error_message,
        form_data=form_data,
        favorites=favorites,
        vk_posts=vk_posts,
    )


@app.route("/favorites/add", methods=["POST"])
def favorites_add():
    post_text = (request.form.get("post_text") or "").strip()
    product_url = (request.form.get("product_url") or "").strip()
    tone = (request.form.get("tone") or "").strip()

    if not post_text:
        flash("Нет текста поста — сначала сгенерируйте пост", "error")
        return redirect(url_for("index"))

    conn = _db()
    try:
        conn.execute(
            "INSERT INTO favorites (post_text, product_url, tone, created_at_utc) VALUES (?, ?, ?, ?)",
            (post_text, product_url, tone, _utc_now_iso()),
        )
        conn.commit()
    finally:
        conn.close()

    flash("Сохранено в избранное", "success")
    return redirect(url_for("index"))


@app.route("/favorites/delete", methods=["POST"])
def favorites_delete():
    fav_id = (request.form.get("favorite_id") or "").strip()
    if not fav_id.isdigit():
        return redirect(url_for("index"))

    conn = _db()
    try:
        conn.execute("DELETE FROM favorites WHERE id = ?", (int(fav_id),))
        conn.commit()
    finally:
        conn.close()

    flash("Удалено из избранного", "success")
    return redirect(url_for("index"))


@app.route("/vk/publish", methods=["POST"])
def vk_publish():
    post_text = (request.form.get("post_text") or "").strip()
    product_url = (request.form.get("product_url") or "").strip()
    tone = (request.form.get("tone") or "").strip()
    publish_at_local = (request.form.get("publish_at") or "").strip()

    if not post_text:
        flash("Нет текста поста — сначала сгенерируйте пост", "error")
        return redirect(url_for("index"))

    post_text_for_vk = normalize_post_for_vk(post_text)
    publish_at_utc_iso = _parse_local_datetime_to_utc_iso(publish_at_local)
    status = "published" if not publish_at_utc_iso else "scheduled"

    conn = _db()
    try:
        result = vk_wall_post(post_text_for_vk, publish_at_utc_iso=publish_at_utc_iso)
        conn.execute(
            """
            INSERT INTO vk_posts (
                post_text, product_url, tone, vk_post_id, vk_owner_id,
                status, publish_at_utc, created_at_utc, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_text_for_vk,
                product_url,
                tone,
                result.get("vk_post_id"),
                result.get("vk_owner_id"),
                status,
                publish_at_utc_iso,
                _utc_now_iso(),
                None,
            ),
        )
        conn.commit()
    except Exception as e:
        conn.execute(
            """
            INSERT INTO vk_posts (
                post_text, product_url, tone, vk_post_id, vk_owner_id,
                status, publish_at_utc, created_at_utc, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_text_for_vk,
                product_url,
                tone,
                None,
                None,
                "error",
                publish_at_utc_iso,
                _utc_now_iso(),
                str(e),
            ),
        )
        conn.commit()
        flash(str(e), "error")
        return redirect(url_for("index"))
    finally:
        conn.close()

    if status == "scheduled":
        flash("Пост отправлен в VK как отложенный", "success")
    else:
        flash("Пост опубликован в VK", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
