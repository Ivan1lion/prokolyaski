"""
Мини-лендинг для оплаты.

Маршруты:
  GET  /checkout/{token}  — показывает страницу с суммой и кнопкой «Оплатить»
  POST /checkout/{token}  — создаёт платёж в ЮKassa и редиректит на шлюз банка
"""

import os
import logging
from aiohttp import web
from sqlalchemy import select

from app.core.db.config import session_maker
from app.core.db.models import PaymentSession
from app.core.services.payment_service import create_yookassa_payment
from app.core.services.pay_config import PAYMENTS

logger = logging.getLogger(__name__)

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://bot.prokolyaski.ru")


async def checkout_page(request: web.Request) -> web.Response:
    """GET /checkout/{token} — отображает страницу оплаты."""
    token = request.match_info["token"]

    async with session_maker() as session:
        result = await session.execute(
            select(PaymentSession).where(
                PaymentSession.token == token,
                PaymentSession.status == "pending",
            )
        )
        ps = result.scalar_one_or_none()

    if not ps:
        return web.Response(
            text=_error_page("Ссылка на оплату недействительна или уже использована."),
            content_type="text/html",
        )

    cfg = PAYMENTS.get(ps.payment_type, {})
    description = cfg.get("description", "Оплата")

    html = _checkout_page_html(
        amount=ps.amount,
        description=description,
        token=token,
    )
    return web.Response(text=html, content_type="text/html")


async def checkout_process(request: web.Request) -> web.Response:
    """POST /checkout/{token} — создаёт платёж и редиректит на ЮKassa."""
    token = request.match_info["token"]

    async with session_maker() as session:
        result = await session.execute(
            select(PaymentSession).where(
                PaymentSession.token == token,
                PaymentSession.status == "pending",
            )
        )
        ps = result.scalar_one_or_none()

        if not ps:
            return web.Response(
                text=_error_page("Ссылка на оплату недействительна или уже использована."),
                content_type="text/html",
            )

        # Формируем return_url — после оплаты юзер вернётся сюда
        return_url = f"{WEBHOOK_HOST}/checkout/{token}/success"

        # Создаём платёж через единое ядро
        payment_result = await create_yookassa_payment(
            session=session,
            telegram_id=ps.telegram_id,
            vk_id=ps.vk_id,
            payment_type=ps.payment_type,
            platform=ps.platform,
            return_url=return_url,
        )

        if not payment_result.success:
            return web.Response(
                text=_error_page(f"Ошибка: {payment_result.error}"),
                content_type="text/html",
            )

        # Обновляем сессию
        ps.status = "redirected"
        ps.yookassa_payment_id = payment_result.payment_id
        await session.commit()

    # Редирект на платёжный шлюз
    raise web.HTTPFound(location=payment_result.confirmation_url)


async def checkout_success(request: web.Request) -> web.Response:
    """GET /checkout/{token}/success — страница после оплаты."""
    html = _success_page_html()
    return web.Response(text=html, content_type="text/html")


# ============================================================
# HTML-шаблоны (встроенные, без Jinja — для простоты)
# ============================================================

def _checkout_page_html(amount, description: str, token: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Оплата — ПРОколяски</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 20px;
        }}
        .card {{
            background: #fff;
            border-radius: 16px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.1);
            padding: 40px 32px;
            max-width: 400px;
            width: 100%;
            text-align: center;
        }}
        .logo {{ font-size: 48px; margin-bottom: 16px; }}
        h1 {{ font-size: 20px; color: #333; margin-bottom: 8px; }}
        .amount {{
            font-size: 36px;
            font-weight: 700;
            color: #1a1a1a;
            margin: 24px 0;
        }}
        .description {{ color: #666; margin-bottom: 32px; font-size: 14px; }}
        .btn {{
            display: inline-block;
            width: 100%;
            padding: 16px;
            background: #4CAF50;
            color: #fff;
            border: none;
            border-radius: 12px;
            font-size: 18px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s;
        }}
        .btn:hover {{ background: #43A047; }}
        .secure {{ color: #999; font-size: 12px; margin-top: 16px; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="logo">👶🔧</div>
        <h1>ПРОколяски</h1>
        <div class="description">{description}</div>
        <div class="amount">{amount} ₽</div>
        <form method="POST" action="/checkout/{token}">
            <button type="submit" class="btn">Перейти к оплате</button>
        </form>
        <p class="secure">🔒 Оплата через ЮKassa — сервис безопасных платежей ПАО «Сбербанк»</p>
    </div>
</body>
</html>"""


def _success_page_html() -> str:
    return """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Оплата успешна</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 20px;
        }
        .card {
            background: #fff;
            border-radius: 16px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.1);
            padding: 40px 32px;
            max-width: 400px;
            width: 100%;
            text-align: center;
        }
        .icon { font-size: 64px; margin-bottom: 16px; }
        h1 { font-size: 22px; color: #333; margin-bottom: 12px; }
        p { color: #666; line-height: 1.6; }
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">✅</div>
        <h1>Спасибо за оплату!</h1>
        <p>Вернитесь в бот — баланс уже обновлён.<br>Можете закрыть эту страницу.</p>
    </div>
</body>
</html>"""


def _error_page(message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Ошибка</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 20px;
        }}
        .card {{
            background: #fff;
            border-radius: 16px;
            box-shadow: 0 4px 24px rgba(0,0,0,0.1);
            padding: 40px 32px;
            max-width: 400px;
            width: 100%;
            text-align: center;
        }}
        .icon {{ font-size: 64px; margin-bottom: 16px; }}
        h1 {{ font-size: 20px; color: #333; margin-bottom: 12px; }}
        p {{ color: #666; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="icon">⚠️</div>
        <h1>Что-то пошло не так</h1>
        <p>{message}</p>
    </div>
</body>
</html>"""