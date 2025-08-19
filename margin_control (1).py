# margin_control.py

import pandas as pd
import re
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, Application
from datetime import time
import logging
from pathlib import Path
from openpyxl import Workbook, load_workbook
from difflib import SequenceMatcher

# Настройка логирования
logger = logging.getLogger(__name__)

# ID владельца для отправки ежедневных отчетов (замените на реальный)
OWNER_ID = 123456789

# Пороговое значение маржи в процентах
MARGIN_THRESHOLD = 40

# Названия файлов
COSTS_FILE = "Себестоимость.xlsx"  # Изменено: убрано (1)
TTK_FILE = "TTK.xlsx"
PRICES_FILE = "Prices.xlsx"

# Словарь синонимов для ингредиентов
INGREDIENT_SYNONYMS = {
    "кабачок": ["цукини", "цуккини"],
    "цукини": ["кабачок", "цуккини"],
    "соус соевый": ["соевый соус"],
    "соевый соус": ["соус соевый"],
    "масло растительное": ["растительное масло", "масло подсолнечное"],
    "растительное масло": ["масло растительное", "масло подсолнечное"],
    "креветка": ["креветки", "креветки тигровые"],
    "креветки": ["креветка", "креветки тигровые"],
    "сыр": ["сыр твердый", "сыр российский"],
    "помидор": ["томат", "помидоры", "томаты"],
    "томат": ["помидор", "помидоры", "томаты"],
    "перец болгарский": ["болгарский перец", "перец сладкий"],
    "болгарский перец": ["перец болгарский", "перец сладкий"],
}


def normalize_text(text: str) -> str:
    """Нормализация текста: убираем лишние пробелы, приводим к нижнему регистру"""
    if pd.isna(text):
        return ""
    return re.sub(r"\s+", " ", str(text).strip()).lower()


def find_ingredient_match(ingredient: str,
                          available_ingredients: list,
                          threshold: float = 0.6) -> tuple:
    """Поиск ингредиента с учетом синонимов и частичного совпадения"""
    ingredient_norm = normalize_text(ingredient)

    # Точное совпадение
    for avail in available_ingredients:
        if ingredient_norm == normalize_text(avail):
            return (avail, 1.0)

    # Проверка синонимов
    for key, synonyms in INGREDIENT_SYNONYMS.items():
        if ingredient_norm == normalize_text(key):
            for avail in available_ingredients:
                if normalize_text(avail) in [
                        normalize_text(s) for s in synonyms
                ]:
                    return (avail, 0.95)
        for synonym in synonyms:
            if ingredient_norm == normalize_text(synonym):
                for avail in available_ingredients:
                    if normalize_text(avail) == normalize_text(key):
                        return (avail, 0.95)

    # Частичное совпадение (содержание)
    best_match = None
    best_ratio = 0

    for avail in available_ingredients:
        avail_norm = normalize_text(avail)

        # Проверка вхождения одного в другое
        if ingredient_norm in avail_norm or avail_norm in ingredient_norm:
            ratio = 0.8
            if ratio > best_ratio:
                best_match = avail
                best_ratio = ratio
        else:
            # Использование SequenceMatcher для похожести
            ratio = SequenceMatcher(None, ingredient_norm, avail_norm).ratio()
            if ratio > best_ratio:
                best_match = avail
                best_ratio = ratio

    if best_ratio >= threshold:
        return (best_match, best_ratio)

    return (None, 0)


def ensure_prices_file():
    """Создание файла Prices.xlsx, если он не существует"""
    p = Path(PRICES_FILE)

    if not p.exists():
        # Создаем новый файл
        wb = Workbook()
        ws = wb.active
        ws.title = "Prices"
        ws.append(["Блюдо", "Цена продажи"])

        # Если есть TTK файл, добавляем все блюда из него
        if Path(TTK_FILE).exists():
            try:
                df_ttk = pd.read_excel(TTK_FILE, sheet_name='TTK')
                df_ttk.columns = df_ttk.columns.str.strip()

                if 'Блюдо' in df_ttk.columns:
                    unique_dishes = df_ttk['Блюдо'].dropna().unique()
                    for dish in unique_dishes:
                        ws.append([str(dish).strip(),
                                   0])  # Цена по умолчанию 0
            except Exception as e:
                logger.warning(f"Не удалось загрузить блюда из TTK: {e}")

        wb.save(p)
        logger.info(f"Создан файл {PRICES_FILE}")


def check_files_exist():
    """Проверка существования необходимых файлов"""
    missing_files = []

    if not Path(COSTS_FILE).exists():
        missing_files.append(COSTS_FILE)
    if not Path(TTK_FILE).exists():
        missing_files.append(TTK_FILE)

    # Автоматически создаем Prices.xlsx если его нет
    ensure_prices_file()

    return missing_files


def calculate_margin_report():
    """Расчет себестоимости и маржи для всех блюд"""
    try:
        # Проверка наличия файлов
        missing_files = check_files_exist()
        if missing_files:
            return {'error': f"Отсутствуют файлы: {', '.join(missing_files)}"}

        # Загрузка данных из Excel файлов
        try:
            df_costs = pd.read_excel(COSTS_FILE, sheet_name='Sheet')
        except Exception as e:
            return {'error': f"Ошибка чтения {COSTS_FILE}: {str(e)}"}

        try:
            df_ttk = pd.read_excel(TTK_FILE, sheet_name='TTK')
        except Exception as e:
            return {'error': f"Ошибка чтения {TTK_FILE}: {str(e)}"}

        try:
            df_prices = pd.read_excel(PRICES_FILE, sheet_name='Prices')
        except Exception as e:
            return {'error': f"Ошибка чтения {PRICES_FILE}: {str(e)}"}

        # Приведение названий колонок к стандартному виду (убираем пробелы)
        df_costs.columns = df_costs.columns.str.strip()
        df_ttk.columns = df_ttk.columns.str.strip()
        df_prices.columns = df_prices.columns.str.strip()

        # Проверка наличия необходимых колонок
        required_costs_cols = ['Ингредиент', 'Цена за 1 кг']
        required_ttk_cols = ['Блюдо', 'Ингредиент', 'Вес (г)']
        required_prices_cols = ['Блюдо', 'Цена продажи']

        if not all(col in df_costs.columns for col in required_costs_cols):
            return {
                'error':
                f"В файле {COSTS_FILE} отсутствуют необходимые колонки"
            }

        if not all(col in df_ttk.columns for col in required_ttk_cols):
            return {
                'error': f"В файле {TTK_FILE} отсутствуют необходимые колонки"
            }

        if not all(col in df_prices.columns for col in required_prices_cols):
            return {
                'error':
                f"В файле {PRICES_FILE} отсутствуют необходимые колонки"
            }

        # Создание списка доступных ингредиентов
        available_ingredients = df_costs[
            df_costs['Ингредиент'].notna()]['Ингредиент'].tolist()

        # Создание словаря цен ингредиентов с нормализацией
        ingredient_prices = {}
        for _, row in df_costs.iterrows():
            if pd.notna(row['Ингредиент']) and pd.notna(row['Цена за 1 кг']):
                ingredient_name = str(row['Ингредиент']).strip()
                try:
                    price_per_kg = float(row['Цена за 1 кг'])
                    ingredient_prices[ingredient_name] = price_per_kg
                except (ValueError, TypeError):
                    continue

        # Создание словаря цен продажи блюд с нормализацией
        selling_prices = {}
        selling_prices_normalized = {}
        for _, row in df_prices.iterrows():
            if pd.notna(row['Блюдо']) and pd.notna(row['Цена продажи']):
                dish_name = str(row['Блюдо']).strip()
                dish_name_norm = normalize_text(dish_name)
                try:
                    selling_price = float(row['Цена продажи'])
                    selling_prices[dish_name] = selling_price
                    selling_prices_normalized[dish_name_norm] = selling_price
                except (ValueError, TypeError):
                    continue

        # Расчет себестоимости для каждого блюда
        dish_costs = {}
        missing_ingredients_by_dish = {}
        ingredient_matches = {}  # Для отладки

        # Группировка TTK по блюдам
        for dish_name in df_ttk['Блюдо'].unique():
            if pd.isna(dish_name):
                continue

            dish_name = str(dish_name).strip()
            dish_ingredients = df_ttk[df_ttk['Блюдо'] == dish_name]

            total_cost = 0
            missing_ingredients = []
            dish_matches = []

            for _, ingredient_row in dish_ingredients.iterrows():
                if pd.notna(ingredient_row['Ингредиент']) and pd.notna(
                        ingredient_row['Вес (г)']):
                    ingredient_name = str(ingredient_row['Ингредиент']).strip()
                    try:
                        weight_grams = float(ingredient_row['Вес (г)'])
                    except (ValueError, TypeError):
                        continue

                    # Поиск ингредиента с учетом синонимов и частичного совпадения
                    matched_ingredient, match_ratio = find_ingredient_match(
                        ingredient_name, available_ingredients)

                    if matched_ingredient and matched_ingredient in ingredient_prices:
                        # Расчет стоимости ингредиента
                        ingredient_cost = (
                            weight_grams *
                            ingredient_prices[matched_ingredient]) / 1000
                        total_cost += ingredient_cost
                        dish_matches.append({
                            'original': ingredient_name,
                            'matched': matched_ingredient,
                            'ratio': match_ratio
                        })
                    else:
                        missing_ingredients.append(ingredient_name)

            dish_costs[dish_name] = total_cost
            if missing_ingredients:
                missing_ingredients_by_dish[dish_name] = missing_ingredients
            if dish_matches:
                ingredient_matches[dish_name] = dish_matches

        # Расчет маржи для каждого блюда
        results = []
        low_margin_dishes = []
        dishes_without_price = []
        dishes_with_missing_ingredients = []

        for dish_name, cost in dish_costs.items():
            # Проверяем наличие отсутствующих ингредиентов
            if dish_name in missing_ingredients_by_dish:
                dishes_with_missing_ingredients.append({
                    'dish':
                    dish_name,
                    'missing':
                    missing_ingredients_by_dish[dish_name]
                })
                continue

            # Поиск цены с нормализацией
            dish_name_norm = normalize_text(dish_name)
            selling_price = None

            if dish_name in selling_prices:
                selling_price = selling_prices[dish_name]
            elif dish_name_norm in selling_prices_normalized:
                selling_price = selling_prices_normalized[dish_name_norm]

            if selling_price is not None:
                if selling_price > 0:
                    margin_percent = (
                        (selling_price - cost) / selling_price) * 100
                else:
                    margin_percent = 0

                result = {
                    'dish': dish_name,
                    'cost': cost,
                    'price': selling_price,
                    'margin': margin_percent
                }
                results.append(result)

                # Проверка на низкую маржу
                if margin_percent < MARGIN_THRESHOLD:
                    low_margin_dishes.append(result)
            else:
                dishes_without_price.append(dish_name)

        # Сортировка результатов по марже (от меньшей к большей)
        results.sort(key=lambda x: x['margin'])
        low_margin_dishes.sort(key=lambda x: x['margin'])

        return {
            'results': results,
            'low_margin': low_margin_dishes,
            'no_price': dishes_without_price,
            'missing_ingredients': dishes_with_missing_ingredients,
            'ingredient_matches': ingredient_matches
        }

    except Exception as e:
        logger.error(f"Непредвиденная ошибка при расчете маржи: {e}")
        return {'error': f"Непредвиденная ошибка: {str(e)}"}


def format_margin_report(data):
    """Форматирование отчета о марже для отправки в Telegram"""

    if 'error' in data:
        return f"❌ Ошибка: {data['error']}"

    message = "📊 *ОТЧЕТ О МАРЖЕ И СЕБЕСТОИМОСТИ*\n"
    message += "=" * 30 + "\n\n"

    # Блюда с низкой маржой
    if data['low_margin']:
        message += f"⚠️ *ВНИМАНИЕ! Блюда с маржой ниже {MARGIN_THRESHOLD}%:*\n\n"
        for item in data['low_margin']:
            message += f"❗ *{item['dish']}*\n"
            message += f"   • Себестоимость: {item['cost']:.2f} ₽\n"
            message += f"   • Цена продажи: {item['price']:.2f} ₽\n"
            message += f"   • Маржа: *{item['margin']:.1f}%*\n\n"
        message += "-" * 30 + "\n\n"

    # Общая таблица всех блюд
    if data['results']:
        message += "📋 *ВСЕ БЛЮДА:*\n\n"

        # Группировка блюд по уровню маржи
        high_margin = [r for r in data['results'] if r['margin'] >= 60]
        medium_margin = [
            r for r in data['results'] if MARGIN_THRESHOLD <= r['margin'] < 60
        ]

        if high_margin:
            message += "🟢 *Высокая маржа (≥60%):*\n"
            # Сортируем по убыванию маржи для топ-5
            high_margin.sort(key=lambda x: x['margin'], reverse=True)
            for item in high_margin:  # Показываем топ-5
                message += f"• {item['dish']}: {item['margin']:.1f}%\n"

            message += "\n"

        if medium_margin:
            message += f"🟡 *Средняя маржа ({MARGIN_THRESHOLD}-60%):*\n"
            for item in medium_margin[:5]:  # Показываем топ-5
                message += f"• {item['dish']}: {item['margin']:.1f}%\n"
            if len(medium_margin) > 5:
                message += f"  _(и еще {len(medium_margin)-5} блюд)_\n"
            message += "\n"

    # Статистика
    if data['results']:
        avg_margin = sum(r['margin']
                         for r in data['results']) / len(data['results'])
        message += "\n📈 *СТАТИСТИКА:*\n"
        message += f"• Всего блюд с расчетом: {len(data['results'])}\n"
        message += f"• Средняя маржа: {avg_margin:.1f}%\n"
        message += f"• Блюд с низкой маржой: {len(data['low_margin'])}\n"

        if data['no_price']:
            message += f"• Блюд без цены продажи: {len(data['no_price'])}\n"

        if data['missing_ingredients']:
            message += f"• Блюд с отсутствующими ингредиентами: {len(data['missing_ingredients'])}\n"

    # Блюда без цены продажи
    if data['no_price']:
        message += "\n⚪ *Блюда без установленной цены:*\n"
        for dish in data['no_price'][:10]:
            message += f"• {dish}\n"
        if len(data['no_price']) > 10:
            message += f"  _(и еще {len(data['no_price'])-10} блюд)_\n"

    # Блюда с отсутствующими ингредиентами
    if data['missing_ingredients']:
        message += "\n❌ *Блюда с отсутствующими ингредиентами в базе:*\n"
        for item in data['missing_ingredients'][:5]:
            message += f"• *{item['dish']}:*\n"
            for ing in item['missing'][:3]:
                message += f"  - {ing}\n"
            if len(item['missing']) > 3:
                message += f"  _(и еще {len(item['missing'])-3} ингредиентов)_\n"
        if len(data['missing_ingredients']) > 5:
            message += f"  _(и еще {len(data['missing_ingredients'])-5} блюд)_\n"

    return message


def format_debug_report(data):
    """Форматирование отладочного отчета для /margin_debug"""

    if 'error' in data:
        return f"❌ Ошибка: {data['error']}"

    message = "🔍 *ОТЛАДОЧНЫЙ ОТЧЕТ*\n"
    message += "=" * 30 + "\n\n"

    # Блюда без цены продажи
    if data['no_price']:
        message += f"📌 *Блюда без цены в {PRICES_FILE}:*\n"
        for i, dish in enumerate(data['no_price'], 1):
            message += f"{i}. {dish}\n"
        message += f"\n_Всего: {len(data['no_price'])} блюд_\n\n"
    else:
        message += "✅ У всех блюд есть цены продажи\n\n"

    # Блюда с отсутствующими ингредиентами
    if data['missing_ingredients']:
        message += f"⚠️ *Блюда с отсутствующими ингредиентами:*\n\n"

        # Загружаем список доступных ингредиентов для подсказок
        try:
            df_costs = pd.read_excel(COSTS_FILE, sheet_name='Sheet')
            df_costs.columns = df_costs.columns.str.strip()
            available_ingredients = df_costs[
                df_costs['Ингредиент'].notna()]['Ингредиент'].tolist()
        except:
            available_ingredients = []

        for item in data['missing_ingredients']:
            message += f"*{item['dish']}:*\n"
            for ing in item['missing']:
                message += f"  ❌ {ing}"

                # Ищем похожие ингредиенты
                if available_ingredients:
                    matched, ratio = find_ingredient_match(
                        ing, available_ingredients, threshold=0.5)
                    if matched and ratio >= 0.5:
                        message += f" _(возможно: {matched}, схожесть {ratio*100:.0f}%)_"

                message += "\n"
            message += "\n"
    else:
        message += "✅ У всех блюд найдены все ингредиенты\n\n"

    # Информация о сопоставлении ингредиентов
    if 'ingredient_matches' in data and data['ingredient_matches']:
        message += "🔄 *Автоматически сопоставленные ингредиенты:*\n"
        count = 0
        for dish, matches in data['ingredient_matches'].items():
            if count >= 3:  # Показываем только первые 3 блюда
                break
            partial_matches = [m for m in matches if m['ratio'] < 1.0]
            if partial_matches:
                message += f"\n_{dish}:_\n"
                for match in partial_matches[:3]:
                    message += f"  • {match['original']} → {match['matched']} ({match['ratio']*100:.0f}%)\n"
                count += 1

        if count == 0:
            message += "_Все ингредиенты найдены точно_\n"

    return message


async def handle_margin_check(update: Update,
                              context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /margin_check"""

    await update.message.reply_text(
        "⏳ Выполняется анализ маржи и себестоимости...")

    # Расчет маржи
    report_data = calculate_margin_report()

    # Форматирование и отправка отчета
    report_message = format_margin_report(report_data)

    # Разбиваем длинное сообщение на части, если необходимо
    max_length = 4000
    if len(report_message) <= max_length:
        await update.message.reply_text(report_message, parse_mode='Markdown')
    else:
        # Разбиваем по частям
        parts = []
        current_part = ""
        for line in report_message.split('\n'):
            if len(current_part) + len(line) + 1 <= max_length:
                current_part += line + '\n'
            else:
                if current_part:
                    parts.append(current_part)
                current_part = line + '\n'
        if current_part:
            parts.append(current_part)

        # Отправляем части
        for i, part in enumerate(parts):
            if i == 0:
                await update.message.reply_text(part, parse_mode='Markdown')
            else:
                await update.message.reply_text(
                    f"_(продолжение {i+1}/{len(parts)})_\n\n" + part,
                    parse_mode='Markdown')


async def handle_margin_debug(update: Update,
                              context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /margin_debug"""

    await update.message.reply_text("🔍 Выполняется отладочный анализ...")

    # Расчет маржи
    report_data = calculate_margin_report()

    # Форматирование и отправка отладочного отчета
    debug_message = format_debug_report(report_data)

    # Разбиваем длинное сообщение на части, если необходимо
    max_length = 4000
    if len(debug_message) <= max_length:
        await update.message.reply_text(debug_message, parse_mode='Markdown')
    else:
        # Разбиваем по частям
        parts = []
        current_part = ""
        for line in debug_message.split('\n'):
            if len(current_part) + len(line) + 1 <= max_length:
                current_part += line + '\n'
            else:
                if current_part:
                    parts.append(current_part)
                current_part = line + '\n'
        if current_part:
            parts.append(current_part)

        # Отправляем части
        for i, part in enumerate(parts):
            if i == 0:
                await update.message.reply_text(part, parse_mode='Markdown')
            else:
                await update.message.reply_text(
                    f"_(продолжение {i+1}/{len(parts)})_\n\n" + part,
                    parse_mode='Markdown')


async def daily_margin_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневная автоматическая проверка маржи"""

    try:
        # Расчет маржи
        report_data = calculate_margin_report()

        # Форматирование отчета
        report_message = "🤖 *ЕЖЕДНЕВНЫЙ АВТОМАТИЧЕСКИЙ ОТЧЕТ*\n\n" + format_margin_report(
            report_data)

        # Отправка владельцу
        max_length = 4000
        if len(report_message) <= max_length:
            await context.bot.send_message(chat_id=OWNER_ID,
                                           text=report_message,
                                           parse_mode='Markdown')
        else:
            # Разбиваем длинное сообщение
            parts = []
            current_part = ""
            for line in report_message.split('\n'):
                if len(current_part) + len(line) + 1 <= max_length:
                    current_part += line + '\n'
                else:
                    if current_part:
                        parts.append(current_part)
                    current_part = line + '\n'
            if current_part:
                parts.append(current_part)

            # Отправляем части
            for i, part in enumerate(parts):
                await context.bot.send_message(
                    chat_id=OWNER_ID,
                    text=part
                    if i == 0 else f"_(часть {i+1}/{len(parts)})_\n\n" + part,
                    parse_mode='Markdown')

        logger.info("Ежедневный отчет о марже успешно отправлен")

    except Exception as e:
        logger.error(f"Ошибка при отправке ежедневного отчета: {e}")
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=
                f"❌ Ошибка при формировании ежедневного отчета о марже: {str(e)}"
            )
        except:
            pass


def setup_margin_control(application: Application) -> None:
    """Настройка функций контроля маржи в боте"""

    try:
        # Добавление обработчиков команд
        application.add_handler(
            CommandHandler("margin_check", handle_margin_check))
        application.add_handler(
            CommandHandler("margin_debug", handle_margin_debug))

        # Настройка ежедневной проверки в 10:00
        if application.job_queue is not None:
            application.job_queue.run_daily(daily_margin_check,
                                            time=time(hour=10,
                                                      minute=0,
                                                      second=0),
                                            name='daily_margin_check')
            logger.info(
                "Контроль маржи настроен: команды /margin_check, /margin_debug и ежедневная проверка в 10:00"
            )
        else:
            logger.warning(
                "JobQueue не инициализирован. Ежедневная проверка не будет работать."
            )
            logger.info(
                "Контроль маржи настроен: команды /margin_check и /margin_debug"
            )

    except Exception as e:
        logger.error(f"Ошибка при настройке контроля маржи: {e}")
        raise
