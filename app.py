from flask import Flask, render_template, request, jsonify
import sqlite3
from datetime import datetime
from urllib.parse import unquote
import csv
import io

app = Flask(__name__)
DATABASE = 'meal_planner.db'

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def parse_number(value):
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        cleaned = str(value).strip().replace(',', '.')
        cleaned = ''.join(c for c in cleaned if c.isdigit() or c in '.-')
        return float(cleaned)
    except (ValueError, TypeError):
        return 0.0

def init_db():
    with sqlite3.connect(DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # ----- Удаляем ограничение UNIQUE с поля calories, если оно есть (для совместимости со старой БД) -----
        cursor.execute("PRAGMA table_info(programs)")
        columns = cursor.fetchall()
        for col in columns:
            if col['name'] == 'calories' and col['pk'] == 0 and 'UNIQUE' in str(col):
                cursor.execute("ALTER TABLE programs RENAME TO programs_old")
                cursor.execute('''
                    CREATE TABLE programs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        calories INTEGER
                    )
                ''')
                cursor.execute("INSERT INTO programs (id, name, calories) SELECT id, name, calories FROM programs_old")
                cursor.execute("DROP TABLE programs_old")
                break

        # ----- СОЗДАНИЕ НОВЫХ ТАБЛИЦ (ЕСЛИ ИХ НЕТ) -----
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS meal_type (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type_name TEXT DEFAULT ''
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_program (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT DEFAULT '',
                program_id INTEGER,
                day_num TEXT,
                FOREIGN KEY (program_id) REFERENCES programs(id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_meal_replacements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                day_num INTEGER NOT NULL,
                meal_type_id INTEGER NOT NULL,
                original_meal_name TEXT NOT NULL,
                new_meal_name TEXT NOT NULL,
                new_weight TEXT NOT NULL,
                new_kkal INTEGER NOT NULL,
                new_protein REAL DEFAULT 0,
                new_fat REAL DEFAULT 0,
                new_carbs REAL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES user_program(id) ON DELETE CASCADE,
                FOREIGN KEY (meal_type_id) REFERENCES meal_type(id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS food_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS food_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                calories_per_100 REAL NOT NULL,
                protein REAL DEFAULT 0,
                fat REAL DEFAULT 0,
                carbs REAL DEFAULT 0,
                note TEXT,
                category_id INTEGER,
                FOREIGN KEY (category_id) REFERENCES food_categories(id) ON DELETE SET NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dish_ingredients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dish_name TEXT NOT NULL,
                ingredient_id INTEGER NOT NULL,
                weight_grams REAL NOT NULL,
                FOREIGN KEY (ingredient_id) REFERENCES food_items(id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dish_nutrition (
                dish_name TEXT PRIMARY KEY,
                calories REAL,
                protein REAL,
                fat REAL,
                carbs REAL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS programs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                calories INTEGER
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS program_dishes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                program_id INTEGER NOT NULL,
                day_num INTEGER NOT NULL,
                meal_type_id INTEGER NOT NULL,
                dish_name TEXT NOT NULL,
                weight TEXT NOT NULL,
                kkal INTEGER NOT NULL,
                FOREIGN KEY (program_id) REFERENCES programs(id) ON DELETE CASCADE,
                FOREIGN KEY (meal_type_id) REFERENCES meal_type(id)
            )
        ''')

        # ----- ИНИЦИАЛИЗАЦИЯ СПРАВОЧНИКОВ -----
        cursor.execute("SELECT COUNT(*) FROM meal_type")
        if cursor.fetchone()[0] == 0:
            meal_types = ["Завтрак", "Ланч", "Обед", "Полдник", "Ужин"]
            cursor.executemany("INSERT INTO meal_type (type_name) VALUES (?)", [(mt,) for mt in meal_types])

        cursor.execute("SELECT COUNT(*) FROM food_categories")
        if cursor.fetchone()[0] == 0:
            categories = [
                "Курица", "Говядина", "Баранина", "Кролик", "Индейка", "Свинина", "Утка",
                "Рыба", "Масла", "Крупа", "Мука", "Специи", "Зелень", "Овощи", "Фрукт",
                "Кисло-молочная продукция", "Сыр", "Орехи", "Сухофрукты", "Ягоды", "Грибы",
                "Морепродукты", "Виды молока", "Виды яица", "Виды хлеба"
            ]
            for cat in categories:
                cursor.execute("INSERT INTO food_categories (name) VALUES (?)", (cat,))

        # ----- МИГРАЦИЯ СТАРЫХ ДАННЫХ (если есть таблицы typical_programm_*) -----
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='typical_programm_900'")
        old_table_exists = cursor.fetchone()
        if old_table_exists:
            print("Обнаружены старые таблицы, запускаем миграцию...")
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'typical_programm_%'")
            old_tables = cursor.fetchall()
            program_id_map = {}
            for row in old_tables:
                old_table = row['name']
                cal_str = old_table.replace('typical_programm_', '')
                try:
                    cal = int(cal_str)
                except:
                    continue
                cursor.execute("INSERT OR IGNORE INTO programs (name, calories) VALUES (?, ?)", (f"{cal} ккал", cal))
                program_id = cursor.execute("SELECT id FROM programs WHERE calories = ? AND name = ?", (cal, f"{cal} ккал")).fetchone()
                if program_id:
                    program_id = program_id['id']
                else:
                    program_id = cursor.execute("SELECT id FROM programs WHERE calories = ?", (cal,)).fetchone()['id']
                program_id_map[cal] = program_id
                rows = conn.execute(f"SELECT meel_name, weight, kkal, meel_type, day_num FROM {old_table}").fetchall()
                for r in rows:
                    existing_dish = cursor.execute('''
                        SELECT id FROM program_dishes 
                        WHERE program_id = ? AND day_num = ? AND meal_type_id = ? AND dish_name = ?
                    ''', (program_id, r['day_num'], r['meel_type'], r['meel_name'])).fetchone()
                    if not existing_dish:
                        conn.execute('''
                            INSERT INTO program_dishes (program_id, day_num, meal_type_id, dish_name, weight, kkal)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (program_id, r['day_num'], r['meel_type'], r['meel_name'], r['weight'], r['kkal']))
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_programm'")
            if cursor.fetchone():
                cursor.execute("ALTER TABLE user_programm RENAME TO user_programm_old")
                old_users = conn.execute("SELECT id, name, programm_type, day_num FROM user_programm_old").fetchall()
                for u in old_users:
                    prog_id = program_id_map.get(u['programm_type'])
                    if not prog_id:
                        prog_id = program_id_map.get(900)
                    if prog_id:
                        conn.execute('''
                            INSERT OR IGNORE INTO user_program (id, name, program_id, day_num)
                            VALUES (?, ?, ?, ?)
                        ''', (u['id'], u['name'], prog_id, u['day_num']))
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_meal_replacements'")
                if cursor.fetchone():
                    cursor.execute("PRAGMA foreign_keys=OFF")
                    old_repl = conn.execute("SELECT * FROM user_meal_replacements").fetchall()
                    for r in old_repl:
                        r_dict = dict(r)
                        user_exists = conn.execute("SELECT id FROM user_program WHERE id = ?", (r_dict['user_id'],)).fetchone()
                        if user_exists:
                            conn.execute('''
                                INSERT OR REPLACE INTO user_meal_replacements 
                                (id, user_id, day_num, meal_type_id, original_meal_name, new_meal_name, new_weight, new_kkal, new_protein, new_fat, new_carbs)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ''', (
                                r_dict['id'], r_dict['user_id'], r_dict['day_num'], r_dict['meal_type_id'],
                                r_dict['original_meal_name'], r_dict['new_meal_name'], r_dict['new_weight'],
                                r_dict['new_kkal'],
                                r_dict.get('new_protein', 0), r_dict.get('new_fat', 0), r_dict.get('new_carbs', 0)
                            ))
                    cursor.execute("PRAGMA foreign_keys=ON")
                conn.execute("DROP TABLE user_programm_old")
            for row in old_tables:
                conn.execute(f"DROP TABLE {row['name']}")
            print("Миграция завершена успешно.")
        else:
            cursor.execute("SELECT COUNT(*) FROM programs")
            if cursor.fetchone()[0] == 0:
                cursor.execute("INSERT INTO programs (name, calories) VALUES (?, ?)", ("900 ккал", 900))
                program_id = cursor.lastrowid
                meals = {
                    1: [("Овсяная каша", "150г", 180, 1),
                        ("Яблоко", "100г", 52, 2),
                        ("Суп куриный", "250г", 210, 3),
                        ("Печенье", "30г", 120, 4),
                        ("Рыба с овощами", "200г", 250, 5)],
                    2: [("Творог с ягодами", "150г", 190, 1),
                        ("Грейпфрут", "150г", 70, 2),
                        ("Борщ", "300г", 240, 3),
                        ("Йогурт", "125г", 90, 4),
                        ("Курица с гречкой", "200г", 320, 5)],
                    3: [("Омлет", "150г", 210, 1),
                        ("Апельсин", "100г", 47, 2),
                        ("Грибной суп", "250г", 150, 3),
                        ("Орехи", "20г", 120, 4),
                        ("Говядина с рисом", "200г", 340, 5)],
                }
                for day in range(1, 22):
                    source_day = ((day - 1) % 3) + 1
                    for meal in meals[source_day]:
                        cursor.execute('''
                            INSERT INTO program_dishes (program_id, day_num, meal_type_id, dish_name, weight, kkal)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (program_id, day, meal[3], meal[0], meal[1], meal[2]))
                base_cal = 900
                for target_cal in [1200, 1500, 1600, 1800, 2000, 2200, 2600]:
                    factor = target_cal / base_cal
                    cursor.execute("INSERT OR IGNORE INTO programs (name, calories) VALUES (?, ?)", (f"{target_cal} ккал", target_cal))
                    new_prog_id = cursor.lastrowid
                    if not new_prog_id:
                        new_prog_id = cursor.execute("SELECT id FROM programs WHERE calories = ?", (target_cal,)).fetchone()['id']
                    rows = conn.execute("SELECT day_num, meal_type_id, dish_name, weight, kkal FROM program_dishes WHERE program_id = ?", (program_id,)).fetchall()
                    for r in rows:
                        new_kkal = int(r['kkal'] * factor)
                        try:
                            old_weight_val = float(r['weight'].replace('г', '').replace('мл', '').strip())
                        except:
                            old_weight_val = 100
                        new_weight_val = int(old_weight_val * factor)
                        new_weight = f"{new_weight_val}г"
                        conn.execute('''
                            INSERT INTO program_dishes (program_id, day_num, meal_type_id, dish_name, weight, kkal)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (new_prog_id, r['day_num'], r['meal_type_id'], r['dish_name'], new_weight, new_kkal))
                print("Базовая программа 900 ккал и производные созданы.")
        conn.commit()

# ---------------------- API ДЛЯ КАТЕГОРИЙ ----------------------
@app.route('/api/categories', methods=['GET'])
def get_categories():
    with get_db() as conn:
        rows = conn.execute('SELECT id, name FROM food_categories ORDER BY name').fetchall()
        return jsonify([dict(row) for row in rows])

@app.route('/api/categories', methods=['POST'])
def create_category():
    data = request.json
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Название категории обязательно'}), 400
    with get_db() as conn:
        try:
            cursor = conn.execute('INSERT INTO food_categories (name) VALUES (?)', (name,))
            conn.commit()
            return jsonify({'id': cursor.lastrowid, 'name': name}), 201
        except sqlite3.IntegrityError:
            return jsonify({'error': 'Категория уже существует'}), 400

@app.route('/api/categories/<int:cat_id>', methods=['PUT'])
def update_category(cat_id):
    data = request.json
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Название категории обязательно'}), 400
    with get_db() as conn:
        conn.execute('UPDATE food_categories SET name = ? WHERE id = ?', (name, cat_id))
        conn.commit()
        return jsonify({'success': True})

@app.route('/api/categories/<int:cat_id>', methods=['DELETE'])
def delete_category(cat_id):
    with get_db() as conn:
        conn.execute('UPDATE food_items SET category_id = NULL WHERE category_id = ?', (cat_id,))
        conn.execute('DELETE FROM food_categories WHERE id = ?', (cat_id,))
        conn.commit()
        return jsonify({'success': True})

# ---------------------- API ДЛЯ ПРОДУКТОВ ----------------------
@app.route('/api/food_items', methods=['GET'])
def get_food_items():
    category_id = request.args.get('category_id', type=int)
    query = '''
        SELECT fi.*, fc.name as category_name
        FROM food_items fi
        LEFT JOIN food_categories fc ON fi.category_id = fc.id
    '''
    params = []
    if category_id:
        query += ' WHERE fi.category_id = ?'
        params.append(category_id)
    query += ' ORDER BY fi.name'
    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            for col in ['calories_per_100', 'protein', 'fat', 'carbs']:
                d[col] = parse_number(d.get(col, 0))
            result.append(d)
        return jsonify(result)

@app.route('/api/food_items', methods=['POST'])
def create_food_item():
    data = request.json
    if not data.get('name') or data.get('calories_per_100') is None:
        return jsonify({'error': 'Не хватает обязательных полей (name, calories_per_100)'}), 400
    with get_db() as conn:
        cursor = conn.execute('''
            INSERT INTO food_items (name, calories_per_100, protein, fat, carbs, note, category_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            data['name'], parse_number(data['calories_per_100']),
            parse_number(data.get('protein', 0)), parse_number(data.get('fat', 0)), parse_number(data.get('carbs', 0)),
            data.get('note', ''), data.get('category_id')
        ))
        conn.commit()
        return jsonify({'id': cursor.lastrowid}), 201

@app.route('/api/food_items/<int:item_id>', methods=['PUT'])
def update_food_item(item_id):
    data = request.json
    with get_db() as conn:
        conn.execute('''
            UPDATE food_items
            SET name = ?, calories_per_100 = ?, protein = ?, fat = ?, carbs = ?, note = ?, category_id = ?
            WHERE id = ?
        ''', (
            data.get('name'), parse_number(data.get('calories_per_100')),
            parse_number(data.get('protein', 0)), parse_number(data.get('fat', 0)), parse_number(data.get('carbs', 0)),
            data.get('note', ''), data.get('category_id'), item_id
        ))
        conn.commit()
        return jsonify({'success': True})

@app.route('/api/food_items/<int:item_id>', methods=['DELETE'])
def delete_food_item(item_id):
    with get_db() as conn:
        conn.execute('DELETE FROM food_items WHERE id = ?', (item_id,))
        conn.commit()
        return jsonify({'success': True})

# ---------------------- API ДЛЯ ДЕРЕВА ИНГРЕДИЕНТОВ ----------------------
@app.route('/api/ingredients_tree')
def ingredients_tree():
    with get_db() as conn:
        categories = conn.execute('SELECT id, name FROM food_categories ORDER BY name').fetchall()
        tree = []
        for cat in categories:
            items = conn.execute('''
                SELECT id, name, calories_per_100, protein, fat, carbs
                FROM food_items WHERE category_id = ? ORDER BY name
            ''', (cat['id'],)).fetchall()
            children = []
            for it in items:
                children.append({
                    'id': it['id'],
                    'name': it['name'],
                    'calories_per_100': parse_number(it['calories_per_100']),
                    'protein': parse_number(it['protein']),
                    'fat': parse_number(it['fat']),
                    'carbs': parse_number(it['carbs'])
                })
            tree.append({
                'id': cat['id'],
                'name': cat['name'],
                'type': 'category',
                'children': children
            })
        uncat_items = conn.execute('''
            SELECT id, name, calories_per_100, protein, fat, carbs
            FROM food_items WHERE category_id IS NULL ORDER BY name
        ''').fetchall()
        if uncat_items:
            uncat_children = []
            for it in uncat_items:
                uncat_children.append({
                    'id': it['id'],
                    'name': it['name'],
                    'calories_per_100': parse_number(it['calories_per_100']),
                    'protein': parse_number(it['protein']),
                    'fat': parse_number(it['fat']),
                    'carbs': parse_number(it['carbs'])
                })
            tree.append({
                'id': -1,
                'name': 'Без категории',
                'type': 'category',
                'children': uncat_children
            })
        return jsonify(tree)

# ---------------------- API ДЛЯ СОСТАВА БЛЮД ----------------------
@app.route('/api/dish_composition/<path:dish_name>', methods=['GET'])
def get_dish_composition(dish_name):
    dish_name = unquote(dish_name)
    with get_db() as conn:
        try:
            rows = conn.execute('''
                SELECT fi.id, fi.name, di.weight_grams, fi.calories_per_100, fi.protein, fi.fat, fi.carbs
                FROM dish_ingredients di
                JOIN food_items fi ON di.ingredient_id = fi.id
                WHERE di.dish_name = ?
            ''', (dish_name,)).fetchall()
            ingredients = []
            total_cal = 0.0
            total_protein = 0.0
            total_fat = 0.0
            total_carbs = 0.0
            for r in rows:
                weight = parse_number(r['weight_grams'])
                if weight == 0:
                    continue
                cal_per_100 = parse_number(r['calories_per_100'])
                prot_per_100 = parse_number(r['protein'])
                fat_per_100 = parse_number(r['fat'])
                carbs_per_100 = parse_number(r['carbs'])
                cal = cal_per_100 * weight / 100
                prot = prot_per_100 * weight / 100
                fat = fat_per_100 * weight / 100
                carbs = carbs_per_100 * weight / 100
                ingredients.append({
                    'id': r['id'],
                    'name': r['name'],
                    'weight': weight,
                    'calories': round(cal, 1),
                    'protein': round(prot, 1),
                    'fat': round(fat, 1),
                    'carbs': round(carbs, 1)
                })
                total_cal += cal
                total_protein += prot
                total_fat += fat
                total_carbs += carbs
            return jsonify({
                'ingredients': ingredients,
                'totals': {
                    'calories': round(total_cal, 1),
                    'protein': round(total_protein, 1),
                    'fat': round(total_fat, 1),
                    'carbs': round(total_carbs, 1)
                }
            })
        except Exception as e:
            print(f"Ошибка в get_dish_composition: {e}")
            return jsonify({'ingredients': [], 'totals': {'calories':0,'protein':0,'fat':0,'carbs':0}}), 200

@app.route('/api/dish_composition/<path:dish_name>', methods=['POST'])
def save_dish_composition(dish_name):
    dish_name = unquote(dish_name)
    data = request.json
    ingredients = data.get('ingredients', [])
    with get_db() as conn:
        conn.execute('DELETE FROM dish_ingredients WHERE dish_name = ?', (dish_name,))
        for ing in ingredients:
            weight = parse_number(ing.get('weight_grams', 0))
            if weight <= 0:
                continue
            conn.execute('''
                INSERT INTO dish_ingredients (dish_name, ingredient_id, weight_grams)
                VALUES (?, ?, ?)
            ''', (dish_name, ing['ingredient_id'], weight))
        rows = conn.execute('''
            SELECT fi.calories_per_100, fi.protein, fi.fat, fi.carbs, di.weight_grams
            FROM dish_ingredients di
            JOIN food_items fi ON di.ingredient_id = fi.id
            WHERE di.dish_name = ?
        ''', (dish_name,)).fetchall()
        total_cal = total_protein = total_fat = total_carbs = 0.0
        for r in rows:
            w = parse_number(r['weight_grams'])
            if w <= 0:
                continue
            cal_per_100 = parse_number(r['calories_per_100'])
            prot_per_100 = parse_number(r['protein'])
            fat_per_100 = parse_number(r['fat'])
            carbs_per_100 = parse_number(r['carbs'])
            total_cal += cal_per_100 * w / 100
            total_protein += prot_per_100 * w / 100
            total_fat += fat_per_100 * w / 100
            total_carbs += carbs_per_100 * w / 100
        conn.execute('''
            INSERT OR REPLACE INTO dish_nutrition (dish_name, calories, protein, fat, carbs)
            VALUES (?, ?, ?, ?, ?)
        ''', (dish_name, round(total_cal,1), round(total_protein,1), round(total_fat,1), round(total_carbs,1)))
        conn.commit()
    return jsonify({'success': True})

@app.route('/api/dish_composition/<path:dish_name>', methods=['DELETE'])
def delete_dish_composition(dish_name):
    dish_name = unquote(dish_name)
    with get_db() as conn:
        conn.execute('DELETE FROM dish_ingredients WHERE dish_name = ?', (dish_name,))
        conn.execute('DELETE FROM dish_nutrition WHERE dish_name = ?', (dish_name,))
        conn.commit()
    return jsonify({'success': True})

# ---------------------- API ДЛЯ БЛЮД ДЛЯ ЗАМЕН (правильный порядок маршрутов) ----------------------
def _get_available_dishes(program_id, meal_type_id, user_id=None, current_day=None):
    with get_db() as conn:
        rows = conn.execute('''
            SELECT DISTINCT dish_name, weight, kkal
            FROM program_dishes
            WHERE program_id = ? AND meal_type_id = ?
            ORDER BY dish_name
        ''', (program_id, meal_type_id)).fetchall()
        print(f"[DEBUG] program_id={program_id}, meal_type_id={meal_type_id}, найдено блюд: {len(rows)}")
        dishes = []
        for r in rows:
            nut = conn.execute('SELECT protein, fat, carbs FROM dish_nutrition WHERE dish_name = ?', (r['dish_name'],)).fetchone()
            days_ago = None
            if user_id is not None and current_day is not None:
                user = conn.execute('SELECT program_id FROM user_program WHERE id = ?', (user_id,)).fetchone()
                if user:
                    last_used = conn.execute('''
                        SELECT MAX(day_num) as last_day
                        FROM program_dishes
                        WHERE program_id = ? AND dish_name = ? AND day_num < ?
                    ''', (user['program_id'], r['dish_name'], current_day)).fetchone()
                    if last_used and last_used['last_day']:
                        days_ago = current_day - last_used['last_day']
            dishes.append({
                'name': r['dish_name'],
                'weight': r['weight'],
                'kkal': r['kkal'],
                'protein': parse_number(nut['protein']) if nut else 0,
                'fat': parse_number(nut['fat']) if nut else 0,
                'carbs': parse_number(nut['carbs']) if nut else 0,
                'days_ago': days_ago
            })
        return dishes

@app.route('/api/available_dishes/<int:calories>/<int:meal_type_id>')
def get_available_dishes_by_calories(calories, meal_type_id):
    user_id = request.args.get('user_id', type=int)
    current_day = request.args.get('day', type=int)
    print(f"[DEBUG] Запрос по калориям: calories={calories}, meal_type_id={meal_type_id}, user_id={user_id}, day={current_day}")
    with get_db() as conn:
        prog = conn.execute('SELECT id FROM programs WHERE calories = ? LIMIT 1', (calories,)).fetchone()
        if not prog:
            return jsonify({'error': f'Программа с калорийностью {calories} не найдена'}), 404
        program_id = prog['id']
    dishes = _get_available_dishes(program_id, meal_type_id, user_id, current_day)
    return jsonify(dishes)

@app.route('/api/available_dishes/<int:program_id>/<int:meal_type_id>')
def get_available_dishes_by_program_id(program_id, meal_type_id):
    user_id = request.args.get('user_id', type=int)
    current_day = request.args.get('day', type=int)
    print(f"[DEBUG] Запрос по program_id: program_id={program_id}, meal_type_id={meal_type_id}, user_id={user_id}, day={current_day}")
    dishes = _get_available_dishes(program_id, meal_type_id, user_id, current_day)
    return jsonify(dishes)

# ---------------------- API ДЛЯ ЗАМЕН БЛЮД ПОЛЬЗОВАТЕЛЯ ----------------------
@app.route('/api/user_replacements', methods=['POST'])
def save_user_replacement():
    data = request.json
    user_id = data.get('user_id')
    day = data.get('day')
    meal_type_id = data.get('meal_type_id')
    original_meal_name = data.get('original_meal_name')
    items = data.get('items', [])
    if not all([user_id, day, meal_type_id, original_meal_name]):
        return jsonify({'error': 'Не все поля заполнены'}), 400
    with get_db() as conn:
        conn.execute('''
            DELETE FROM user_meal_replacements
            WHERE user_id = ? AND day_num = ? AND meal_type_id = ? AND original_meal_name = ?
        ''', (user_id, day, meal_type_id, original_meal_name))
        for it in items:
            conn.execute('''
                INSERT INTO user_meal_replacements
                (user_id, day_num, meal_type_id, original_meal_name, new_meal_name, new_weight, new_kkal,
                 new_protein, new_fat, new_carbs)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                user_id, day, meal_type_id, original_meal_name,
                it['name'], it['weight'], parse_number(it['kkal']),
                parse_number(it.get('protein', 0)), parse_number(it.get('fat', 0)), parse_number(it.get('carbs', 0))
            ))
        conn.commit()
    return jsonify({'success': True})

@app.route('/api/user_replacements/<int:user_id>/<int:day>')
def get_user_replacements(user_id, day):
    with get_db() as conn:
        rows = conn.execute('''
            SELECT meal_type_id, original_meal_name, new_meal_name, new_weight, new_kkal,
                   new_protein, new_fat, new_carbs
            FROM user_meal_replacements
            WHERE user_id = ? AND day_num = ?
        ''', (user_id, day)).fetchall()
        replacements = {}
        for r in rows:
            key = r['meal_type_id']
            if key not in replacements:
                replacements[key] = []
            replacements[key].append({
                'original_meal_name': r['original_meal_name'],
                'new_meal_name': r['new_meal_name'],
                'new_weight': r['new_weight'],
                'new_kkal': parse_number(r['new_kkal']),
                'new_protein': parse_number(r['new_protein']),
                'new_fat': parse_number(r['new_fat']),
                'new_carbs': parse_number(r['new_carbs'])
            })
        return jsonify(replacements)

# ---------------------- API ДЛЯ МЕНЮ ПОЛЬЗОВАТЕЛЯ С ИСТОРИЕЙ ----------------------
def get_last_occurrence(program_id, current_day, dish_name):
    with get_db() as conn:
        row = conn.execute('''
            SELECT MAX(day_num) as last_day
            FROM program_dishes
            WHERE program_id = ? AND dish_name = ? AND day_num < ?
        ''', (program_id, dish_name, current_day)).fetchone()
        return row['last_day'] if row and row['last_day'] else None

@app.route('/api/menu_with_history/<int:user_id>')
@app.route('/api/menu_with_history/<int:user_id>/<int:day>')
def get_menu_with_history(user_id, day=None):
    with get_db() as conn:
        user = conn.execute('SELECT program_id, day_num FROM user_program WHERE id = ?', (user_id,)).fetchone()
        if not user:
            return jsonify({'error': 'Пользователь не найден'}), 404
        program_id = user['program_id']
        start_date_str = user['day_num']
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            today = datetime.today().date()
            delta = (today - start_date).days
            if day is None:
                if delta < 0:
                    return jsonify({'error': 'Программа ещё не началась', 'day': None}), 200
                day_num = delta + 1
                if day_num > 21:
                    return jsonify({'error': 'Программа завершена', 'day': None}), 200
            else:
                day_num = day
                if day_num < 1 or day_num > 21:
                    return jsonify({'error': 'День должен быть от 1 до 21'}), 400
        except ValueError:
            return jsonify({'error': 'Неверная дата начала'}), 500

        rows = conn.execute('''
            SELECT pd.dish_name, pd.weight, pd.kkal, mt.id as meal_type_id, mt.type_name
            FROM program_dishes pd
            JOIN meal_type mt ON pd.meal_type_id = mt.id
            WHERE pd.program_id = ? AND pd.day_num = ?
            ORDER BY pd.meal_type_id
        ''', (program_id, day_num)).fetchall()

        repl_rows = conn.execute('''
            SELECT meal_type_id, original_meal_name, new_meal_name, new_weight, new_kkal,
                   new_protein, new_fat, new_carbs
            FROM user_meal_replacements
            WHERE user_id = ? AND day_num = ?
        ''', (user_id, day_num)).fetchall()
        replacements = {}
        for r in repl_rows:
            key = (r['meal_type_id'], r['original_meal_name'])
            if key not in replacements:
                replacements[key] = []
            replacements[key].append({
                'name': r['new_meal_name'],
                'weight': r['new_weight'],
                'kkal': parse_number(r['new_kkal']),
                'protein': parse_number(r['new_protein']),
                'fat': parse_number(r['new_fat']),
                'carbs': parse_number(r['new_carbs'])
            })

        menu = {}
        total_kkal = 0
        total_protein = 0.0
        total_fat = 0.0
        total_carbs = 0.0

        for row in rows:
            type_name = row['type_name']
            if type_name not in menu:
                menu[type_name] = []
            meal_type_id = row['meal_type_id']
            original_name = row['dish_name']
            key = (meal_type_id, original_name)

            if key in replacements:
                for repl in replacements[key]:
                    menu[type_name].append({
                        'name': repl['name'],
                        'weight': repl['weight'],
                        'kkal': repl['kkal'],
                        'days_ago': 'замена',
                        'meal_type_id': meal_type_id,
                        'original': original_name,
                        'protein': repl['protein'],
                        'fat': repl['fat'],
                        'carbs': repl['carbs']
                    })
                    total_kkal += repl['kkal']
                    total_protein += repl['protein']
                    total_fat += repl['fat']
                    total_carbs += repl['carbs']
            else:
                nut = conn.execute('SELECT protein, fat, carbs FROM dish_nutrition WHERE dish_name = ?', (original_name,)).fetchone()
                protein_val = parse_number(nut['protein']) if nut else 0
                fat_val = parse_number(nut['fat']) if nut else 0
                carbs_val = parse_number(nut['carbs']) if nut else 0
                last_day = get_last_occurrence(program_id, day_num, original_name)
                repeat_note = f"{day_num - last_day} дн. назад" if last_day else "впервые"
                menu[type_name].append({
                    'name': original_name,
                    'weight': row['weight'],
                    'kkal': row['kkal'],
                    'days_ago': repeat_note,
                    'meal_type_id': meal_type_id,
                    'original': None,
                    'protein': protein_val,
                    'fat': fat_val,
                    'carbs': carbs_val
                })
                total_kkal += row['kkal']
                total_protein += protein_val
                total_fat += fat_val
                total_carbs += carbs_val

        return jsonify({
            'day': day_num,
            'menu': menu,
            'total_kkal': total_kkal,
            'total_protein': round(total_protein, 1),
            'total_fat': round(total_fat, 1),
            'total_carbs': round(total_carbs, 1)
        })

# ---------------------- API ДЛЯ ПОЛНОГО МЕНЮ ПОЛЬЗОВАТЕЛЯ НА 21 ДЕНЬ ----------------------
@app.route('/api/user_full_menu/<int:user_id>')
def get_user_full_menu(user_id):
    with get_db() as conn:
        user = conn.execute('SELECT program_id, day_num FROM user_program WHERE id = ?', (user_id,)).fetchone()
        if not user:
            return jsonify({'error': 'Пользователь не найден'}), 404
        program_id = user['program_id']

        repl_rows = conn.execute('''
            SELECT day_num, meal_type_id, original_meal_name, new_meal_name, new_weight, new_kkal,
                   new_protein, new_fat, new_carbs
            FROM user_meal_replacements
            WHERE user_id = ?
        ''', (user_id,)).fetchall()
        replacements = {}
        for r in repl_rows:
            day = r['day_num']
            mt = r['meal_type_id']
            orig = r['original_meal_name']
            replacements.setdefault(day, {}).setdefault(mt, {}).setdefault(orig, []).append({
                'name': r['new_meal_name'],
                'weight': r['new_weight'],
                'kkal': r['new_kkal'],
                'protein': r['new_protein'],
                'fat': r['new_fat'],
                'carbs': r['new_carbs']
            })

        days_result = []
        for day_num in range(1, 22):
            rows = conn.execute('''
                SELECT pd.dish_name, pd.weight, pd.kkal, mt.id as meal_type_id, mt.type_name
                FROM program_dishes pd
                JOIN meal_type mt ON pd.meal_type_id = mt.id
                WHERE pd.program_id = ? AND pd.day_num = ?
                ORDER BY pd.meal_type_id
            ''', (program_id, day_num)).fetchall()

            meals = []
            total_kkal = 0
            total_protein = 0.0
            total_fat = 0.0
            total_carbs = 0.0

            for row in rows:
                mt_id = row['meal_type_id']
                orig_name = row['dish_name']
                if (day_num in replacements and mt_id in replacements[day_num] and orig_name in replacements[day_num][mt_id]):
                    for item in replacements[day_num][mt_id][orig_name]:
                        if item['name'] == "—" and parse_number(item['weight']) == 0:
                            continue
                        p = parse_number(item['protein'])
                        f = parse_number(item['fat'])
                        c = parse_number(item['carbs'])
                        meals.append({
                            'name': item['name'],
                            'weight': item['weight'],
                            'kkal': item['kkal'],
                            'protein': p,
                            'fat': f,
                            'carbs': c,
                            'is_replacement': True,
                            'original_name': orig_name,
                            'meal_type': row['type_name']
                        })
                        total_kkal += item['kkal']
                        total_protein += p
                        total_fat += f
                        total_carbs += c
                else:
                    nut = conn.execute('SELECT protein, fat, carbs FROM dish_nutrition WHERE dish_name = ?', (orig_name,)).fetchone()
                    p = parse_number(nut['protein']) if nut else 0
                    f = parse_number(nut['fat']) if nut else 0
                    c = parse_number(nut['carbs']) if nut else 0
                    meals.append({
                        'name': orig_name,
                        'weight': row['weight'],
                        'kkal': row['kkal'],
                        'protein': p,
                        'fat': f,
                        'carbs': c,
                        'is_replacement': False,
                        'original_name': None,
                        'meal_type': row['type_name']
                    })
                    total_kkal += row['kkal']
                    total_protein += p
                    total_fat += f
                    total_carbs += c

            days_result.append({
                'day': day_num,
                'meals': meals,
                'totals': {
                    'kkal': total_kkal,
                    'protein': round(total_protein, 1),
                    'fat': round(total_fat, 1),
                    'carbs': round(total_carbs, 1)
                }
            })
        return jsonify(days_result)

# ---------------------- API ДЛЯ ПОЛЬЗОВАТЕЛЕЙ И ПРОГРАММ ----------------------
@app.route('/api/program_types', methods=['GET'])
def get_program_types():
    with get_db() as conn:
        programs = conn.execute('SELECT id, name, calories FROM programs ORDER BY id').fetchall()
        return jsonify([{'id': p['id'], 'name': p['name'], 'daily_kkal': p['calories']} for p in programs])

@app.route('/api/users', methods=['GET'])
def get_users():
    with get_db() as conn:
        users = conn.execute('''
            SELECT up.id, up.name, up.program_id, up.day_num, p.name as program_name, p.calories
            FROM user_program up
            JOIN programs p ON up.program_id = p.id
        ''').fetchall()
        result = []
        today = datetime.today().date()
        for user in users:
            user_dict = dict(user)
            start_date_str = user_dict['day_num']
            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
                delta = (today - start_date).days
                if delta < 0:
                    day_status = 'не началась'
                elif delta >= 21:
                    day_status = 'завершена'
                else:
                    day_status = f'день {delta + 1}'
            except ValueError:
                day_status = 'ошибка даты'
            user_dict['day_status'] = day_status
            user_dict['programm_type'] = user_dict['calories']
            result.append(user_dict)
        return jsonify(result)

@app.route('/api/users', methods=['POST'])
def create_user():
    data = request.json
    name = data.get('name', '').strip()
    start_date = data.get('start_date', '')
    program_id = data.get('program_id')
    programm_type = data.get('programm_type')
    
    if not name or not start_date:
        return jsonify({'error': 'Имя и дата начала обязательны'}), 400
    if program_id is None and programm_type is None:
        return jsonify({'error': 'Программа не указана (program_id или programm_type)'}), 400
    
    with get_db() as conn:
        if program_id is not None:
            prog = conn.execute('SELECT id FROM programs WHERE id = ?', (program_id,)).fetchone()
            if not prog:
                return jsonify({'error': 'Программа не найдена'}), 400
            final_program_id = prog['id']
        else:
            prog = conn.execute('SELECT id FROM programs WHERE calories = ? LIMIT 1', (programm_type,)).fetchone()
            if not prog:
                return jsonify({'error': f'Программа с калорийностью {programm_type} не найдена'}), 400
            final_program_id = prog['id']
        
        try:
            datetime.strptime(start_date, '%Y-%m-%d')
        except ValueError:
            return jsonify({'error': 'Неверный формат даты'}), 400
        
        cursor = conn.execute(
            'INSERT INTO user_program (name, program_id, day_num) VALUES (?, ?, ?)',
            (name, final_program_id, start_date)
        )
        conn.commit()
        new_id = cursor.lastrowid
    return jsonify({'id': new_id, 'name': name, 'program_id': final_program_id, 'day_num': start_date}), 201

@app.route('/api/users/<int:user_id>', methods=['PUT'])
def update_user(user_id):
    data = request.json
    with get_db() as conn:
        current = conn.execute('SELECT name, program_id, day_num FROM user_program WHERE id = ?', (user_id,)).fetchone()
        if not current:
            return jsonify({'error': 'Пользователь не найден'}), 404
        
        new_name = data.get('name', current['name']).strip()
        new_program_id = data.get('program_id')
        new_programm_type = data.get('programm_type')
        new_date = data.get('start_date', current['day_num'])
        
        if new_date:
            try:
                datetime.strptime(new_date, '%Y-%m-%d')
            except ValueError:
                return jsonify({'error': 'Неверный формат даты'}), 400
        
        if new_program_id is not None:
            final_program_id = new_program_id
        elif new_programm_type is not None:
            prog = conn.execute('SELECT id FROM programs WHERE calories = ? LIMIT 1', (new_programm_type,)).fetchone()
            if not prog:
                return jsonify({'error': f'Программа с калорийностью {new_programm_type} не найдена'}), 400
            final_program_id = prog['id']
        else:
            final_program_id = current['program_id']
        
        prog = conn.execute('SELECT id FROM programs WHERE id = ?', (final_program_id,)).fetchone()
        if not prog:
            return jsonify({'error': 'Программа не найдена'}), 400
        
        conn.execute('UPDATE user_program SET name = ?, program_id = ?, day_num = ? WHERE id = ?',
                     (new_name, final_program_id, new_date, user_id))
        conn.commit()
    return jsonify({'success': True})

@app.route('/api/users/<int:user_id>', methods=['DELETE'])
def delete_user(user_id):
    with get_db() as conn:
        conn.execute('DELETE FROM user_meal_replacements WHERE user_id = ?', (user_id,))
        conn.execute('DELETE FROM user_program WHERE id = ?', (user_id,))
        conn.commit()
    return jsonify({'success': True})

# ---------------------- API ДЛЯ ПОЛНОГО МЕНЮ ПРОГРАММЫ ----------------------
@app.route('/api/full_menu_nutrition')
def get_full_menu_nutrition():
    program_id = request.args.get('program_id', type=int)
    if not program_id:
        return jsonify({'error': 'Не указан program_id'}), 400
    with get_db() as conn:
        prog = conn.execute('SELECT id, name, calories FROM programs WHERE id = ?', (program_id,)).fetchone()
        if not prog:
            return jsonify({'error': 'Программа не найдена'}), 404
        meal_types = conn.execute('SELECT id, type_name FROM meal_type ORDER BY id').fetchall()
        mt_dict = {mt['id']: mt['type_name'] for mt in meal_types}
        days_data = []
        for day in range(1, 22):
            rows = conn.execute('''
                SELECT pd.dish_name, pd.weight, pd.kkal, pd.meal_type_id
                FROM program_dishes pd
                WHERE pd.program_id = ? AND pd.day_num = ?
                ORDER BY pd.meal_type_id
            ''', (program_id, day)).fetchall()
            meals_by_type = {}
            total_kkal = 0
            total_protein = 0.0
            total_fat = 0.0
            total_carbs = 0.0
            for row in rows:
                meal_type = mt_dict[row['meal_type_id']]
                if meal_type not in meals_by_type:
                    meals_by_type[meal_type] = []
                nut = conn.execute('SELECT protein, fat, carbs FROM dish_nutrition WHERE dish_name = ?', (row['dish_name'],)).fetchone()
                protein = nut['protein'] if nut else 0
                fat = nut['fat'] if nut else 0
                carbs = nut['carbs'] if nut else 0
                meals_by_type[meal_type].append({
                    'name': row['dish_name'],
                    'weight': row['weight'],
                    'kkal': row['kkal'],
                    'protein': round(protein, 1),
                    'fat': round(fat, 1),
                    'carbs': round(carbs, 1)
                })
                total_kkal += row['kkal']
                total_protein += protein
                total_fat += fat
                total_carbs += carbs
            days_data.append({
                'day': day,
                'meals': meals_by_type,
                'totals': {
                    'kkal': total_kkal,
                    'protein': round(total_protein, 1),
                    'fat': round(total_fat, 1),
                    'carbs': round(total_carbs, 1)
                }
            })
        return jsonify(days_data)

@app.route('/api/all_dishes_full', methods=['GET'])
def get_all_dishes_full():
    program_id = request.args.get('program_id', type=int)
    if not program_id:
        return jsonify({'error': 'Не указан program_id'}), 400
    with get_db() as conn:
        rows = conn.execute('''
            SELECT pd.id, pd.day_num, pd.dish_name, pd.weight, pd.kkal, pd.meal_type_id,
                   mt.type_name as meal_type_name
            FROM program_dishes pd
            JOIN meal_type mt ON pd.meal_type_id = mt.id
            WHERE pd.program_id = ?
            ORDER BY pd.day_num, pd.meal_type_id
        ''', (program_id,)).fetchall()
        dishes = []
        for r in rows:
            nut = conn.execute('SELECT protein, fat, carbs FROM dish_nutrition WHERE dish_name = ?', (r['dish_name'],)).fetchone()
            dishes.append({
                'id': r['id'],
                'day': r['day_num'],
                'meal_type_id': r['meal_type_id'],
                'meal_type_name': r['meal_type_name'],
                'name': r['dish_name'],
                'weight': r['weight'],
                'kkal': r['kkal'],
                'protein': parse_number(nut['protein']) if nut else 0,
                'fat': parse_number(nut['fat']) if nut else 0,
                'carbs': parse_number(nut['carbs']) if nut else 0
            })
        return jsonify(dishes)

@app.route('/api/meal_types', methods=['GET'])
def get_meal_types():
    with get_db() as conn:
        rows = conn.execute('SELECT id, type_name FROM meal_type ORDER BY id').fetchall()
        return jsonify([{'id': r['id'], 'name': r['type_name']} for r in rows])

# ---------------------- API ДЛЯ РЕДАКТОРА ПРОГРАММ ----------------------
@app.route('/api/program_meal_types', methods=['GET'])
def get_program_meal_types():
    with get_db() as conn:
        rows = conn.execute('SELECT id, type_name FROM meal_type ORDER BY id').fetchall()
        return jsonify([{'id': r['id'], 'name': r['type_name']} for r in rows])

@app.route('/api/program_menu/<int:program_id>/<int:day>', methods=['GET'])
def get_program_menu(program_id, day):
    with get_db() as conn:
        rows = conn.execute('''
            SELECT pd.id, pd.dish_name, pd.weight, pd.kkal, pd.meal_type_id, mt.type_name as meal_type_name
            FROM program_dishes pd
            JOIN meal_type mt ON pd.meal_type_id = mt.id
            WHERE pd.program_id = ? AND pd.day_num = ?
            ORDER BY pd.meal_type_id
        ''', (program_id, day)).fetchall()
        dishes = []
        for r in rows:
            nut = conn.execute('SELECT protein, fat, carbs FROM dish_nutrition WHERE dish_name = ?', (r['dish_name'],)).fetchone()
            dishes.append({
                'id': r['id'],
                'name': r['dish_name'],
                'weight': r['weight'],
                'kkal': r['kkal'],
                'meal_type_id': r['meal_type_id'],
                'meal_type_name': r['meal_type_name'],
                'protein': nut['protein'] if nut else 0,
                'fat': nut['fat'] if nut else 0,
                'carbs': nut['carbs'] if nut else 0
            })
        return jsonify(dishes)

@app.route('/api/program_menu_with_history/<int:program_id>/<int:day>')
def get_program_menu_with_history(program_id, day):
    with get_db() as conn:
        if day < 1 or day > 21:
            return jsonify({'error': 'День должен быть от 1 до 21'}), 400
        rows = conn.execute('''
            SELECT pd.id, pd.dish_name, pd.weight, pd.kkal, pd.meal_type_id, mt.type_name as meal_type_name
            FROM program_dishes pd
            JOIN meal_type mt ON pd.meal_type_id = mt.id
            WHERE pd.program_id = ? AND pd.day_num = ?
            ORDER BY pd.meal_type_id
        ''', (program_id, day)).fetchall()
        dishes = []
        total_kkal = 0
        total_protein = 0.0
        total_fat = 0.0
        total_carbs = 0.0
        for row in rows:
            nut = conn.execute('SELECT protein, fat, carbs FROM dish_nutrition WHERE dish_name = ?', (row['dish_name'],)).fetchone()
            protein = nut['protein'] if nut else 0
            fat = nut['fat'] if nut else 0
            carbs = nut['carbs'] if nut else 0
            last_used = conn.execute('''
                SELECT MAX(day_num) as last_day
                FROM program_dishes
                WHERE program_id = ? AND dish_name = ? AND day_num < ?
            ''', (program_id, row['dish_name'], day)).fetchone()
            days_ago = day - last_used['last_day'] if last_used and last_used['last_day'] else None
            repeat_text = f"{days_ago} дн. назад" if days_ago is not None else "впервые"
            dishes.append({
                'id': row['id'],
                'name': row['dish_name'],
                'weight': row['weight'],
                'kkal': row['kkal'],
                'protein': round(protein, 1),
                'fat': round(fat, 1),
                'carbs': round(carbs, 1),
                'meal_type_name': row['meal_type_name'],
                'meal_type_id': row['meal_type_id'],
                'days_ago': repeat_text
            })
            total_kkal += row['kkal']
            total_protein += protein
            total_fat += fat
            total_carbs += carbs
        return jsonify({
            'day': day,
            'dishes': dishes,
            'totals': {
                'kkal': total_kkal,
                'protein': round(total_protein, 1),
                'fat': round(total_fat, 1),
                'carbs': round(total_carbs, 1)
            }
        })

@app.route('/api/program_menu', methods=['POST'])
def add_program_dish():
    data = request.json
    required = ['program_id', 'day', 'meal_type_id', 'name', 'weight', 'kkal']
    for field in required:
        if field not in data:
            return jsonify({'error': f'Отсутствует поле {field}'}), 400
    with get_db() as conn:
        prog = conn.execute('SELECT id FROM programs WHERE id = ?', (data['program_id'],)).fetchone()
        if not prog:
            return jsonify({'error': 'Программа не найдена'}), 404
        day = int(data['day'])
        if day < 0 or day > 21:
            return jsonify({'error': 'День должен быть от 0 до 21'}), 400
        meal_type_id = int(data['meal_type_id'])
        name = data['name'].strip()
        if not name:
            return jsonify({'error': 'Название блюда обязательно'}), 400
        cursor = conn.execute('''
            INSERT INTO program_dishes (program_id, day_num, meal_type_id, dish_name, weight, kkal)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (data['program_id'], day, meal_type_id, name, data['weight'], int(data['kkal'])))
        dish_id = cursor.lastrowid
        if 'protein' in data and 'fat' in data and 'carbs' in data:
            conn.execute('''
                INSERT OR REPLACE INTO dish_nutrition (dish_name, calories, protein, fat, carbs)
                VALUES (?, ?, ?, ?, ?)
            ''', (name, float(data['kkal']), float(data['protein']), float(data['fat']), float(data['carbs'])))
        conn.commit()
        return jsonify({'id': dish_id, 'success': True}), 201

@app.route('/api/program_menu/<int:dish_id>', methods=['PUT'])
def update_program_dish(dish_id):
    data = request.json
    required = ['program_id', 'day', 'meal_type_id', 'name', 'weight', 'kkal']
    for field in required:
        if field not in data:
            return jsonify({'error': f'Отсутствует поле {field}'}), 400
    with get_db() as conn:
        prog = conn.execute('SELECT id FROM programs WHERE id = ?', (data['program_id'],)).fetchone()
        if not prog:
            return jsonify({'error': 'Программа не найдена'}), 404
        day = int(data['day'])
        if day < 0 or day > 21:
            return jsonify({'error': 'День должен быть от 0 до 21'}), 400
        meal_type_id = int(data['meal_type_id'])
        name = data['name'].strip()
        if not name:
            return jsonify({'error': 'Название блюда обязательно'}), 400
        exists = conn.execute('SELECT id FROM program_dishes WHERE id = ?', (dish_id,)).fetchone()
        if not exists:
            return jsonify({'error': 'Блюдо не найдено'}), 404
        conn.execute('''
            UPDATE program_dishes
            SET day_num = ?, meal_type_id = ?, dish_name = ?, weight = ?, kkal = ?
            WHERE id = ?
        ''', (day, meal_type_id, name, data['weight'], int(data['kkal']), dish_id))
        if 'protein' in data and 'fat' in data and 'carbs' in data:
            conn.execute('''
                INSERT OR REPLACE INTO dish_nutrition (dish_name, calories, protein, fat, carbs)
                VALUES (?, ?, ?, ?, ?)
            ''', (name, float(data['kkal']), float(data['protein']), float(data['fat']), float(data['carbs'])))
        conn.commit()
        return jsonify({'success': True})

@app.route('/api/program_menu/<int:dish_id>', methods=['DELETE'])
def delete_program_dish(dish_id):
    with get_db() as conn:
        row = conn.execute('SELECT dish_name FROM program_dishes WHERE id = ?', (dish_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Блюдо не найдено'}), 404
        dish_name = row['dish_name']
        conn.execute('DELETE FROM program_dishes WHERE id = ?', (dish_id,))
        other_count = conn.execute('SELECT COUNT(*) FROM program_dishes WHERE dish_name = ?', (dish_name,)).fetchone()[0]
        if other_count == 0:
            conn.execute('DELETE FROM dish_nutrition WHERE dish_name = ?', (dish_name,))
            conn.execute('DELETE FROM dish_ingredients WHERE dish_name = ?', (dish_name,))
        conn.commit()
        return jsonify({'success': True})

@app.route('/api/program_available_dishes/<int:program_id>/<int:meal_type_id>')
def get_program_available_dishes(program_id, meal_type_id):
    current_day = request.args.get('current_day', type=int)
    if not current_day:
        return jsonify({'error': 'Не указан текущий день'}), 400
    with get_db() as conn:
        rows = conn.execute('''
            SELECT DISTINCT dish_name, weight, kkal
            FROM program_dishes
            WHERE program_id = ? AND meal_type_id = ?
            ORDER BY dish_name
        ''', (program_id, meal_type_id)).fetchall()
        dishes = []
        for r in rows:
            nut = conn.execute('SELECT protein, fat, carbs FROM dish_nutrition WHERE dish_name = ?', (r['dish_name'],)).fetchone()
            last_used = conn.execute('''
                SELECT MAX(day_num) as last_day
                FROM program_dishes
                WHERE program_id = ? AND dish_name = ? AND day_num < ?
            ''', (program_id, r['dish_name'], current_day)).fetchone()
            days_ago = current_day - last_used['last_day'] if last_used and last_used['last_day'] else None
            dishes.append({
                'name': r['dish_name'],
                'weight': r['weight'],
                'kkal': r['kkal'],
                'protein': nut['protein'] if nut else 0,
                'fat': nut['fat'] if nut else 0,
                'carbs': nut['carbs'] if nut else 0,
                'days_ago': days_ago,
                'meal_type_id': meal_type_id
            })
        return jsonify(dishes)

# ---------------------- API ДЛЯ ИМПОРТА БЛЮД ИЗ CSV ----------------------
@app.route('/api/import_dishes', methods=['POST'])
def import_dishes():
    program_id = request.form.get('program_id', type=int)
    if not program_id:
        return jsonify({'error': 'Не указан program_id'}), 400
    
    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'Файл не загружен'}), 400
    
    content = file.stream.read()
    try:
        try:
            text = content.decode('utf-8-sig')
        except:
            text = content.decode('utf-8')
    except Exception as e:
        return jsonify({'error': f'Ошибка декодирования файла: {str(e)}'}), 400
    
    lines = text.splitlines()
    if not lines:
        return jsonify({'error': 'Файл пуст'}), 400
    first_line = lines[0]
    delimiter = None
    for sep in [';', ',', '\t']:
        if sep in first_line:
            delimiter = sep
            break
    if not delimiter:
        return jsonify({'error': 'Не удалось определить разделитель (ожидается ; , или табуляция)'}), 400
    
    try:
        stream = io.StringIO(text)
        reader = csv.DictReader(stream, delimiter=delimiter)
        if not reader.fieldnames:
            return jsonify({'error': 'CSV не содержит заголовков'}), 400
    except Exception as e:
        return jsonify({'error': f'Ошибка чтения CSV: {str(e)}'}), 400
    
    fieldnames = [f.strip().lower() for f in reader.fieldnames]
    
    col_map = {
        'day': ['day', 'день', 'day_num', 'день_номер', 'день номер'],
        'meal_type': ['meal_type', 'тип_приема', 'тип приема', 'meal_type_name', 'тип_приёма', 'тип приёма'],
        'dish_name': ['dish_name', 'блюдо', 'name', 'название', 'dish name'],
        'weight': ['weight', 'вес', 'weight_grams', 'вес_грамм', 'вес грамм'],
        'kkal': ['kkal', 'калории', 'calories', 'kcal', 'ккал', 'калорийность'],
        'protein': ['protein', 'белки', 'protein_grams', 'белок'],
        'fat': ['fat', 'жиры', 'fat_grams', 'жир'],
        'carbs': ['carbs', 'углеводы', 'carb_grams', 'углевод']
    }
    
    def get_col(field):
        for possible in col_map[field]:
            if possible in fieldnames:
                idx = fieldnames.index(possible)
                return reader.fieldnames[idx]
        return None
    
    day_col = get_col('day')
    meal_type_col = get_col('meal_type')
    name_col = get_col('dish_name')
    weight_col = get_col('weight')
    kkal_col = get_col('kkal')
    protein_col = get_col('protein')
    fat_col = get_col('fat')
    carbs_col = get_col('carbs')
    
    if not all([day_col, meal_type_col, name_col, weight_col, kkal_col]):
        missing = []
        if not day_col: missing.append('день')
        if not meal_type_col: missing.append('тип приёма')
        if not name_col: missing.append('блюдо')
        if not weight_col: missing.append('вес')
        if not kkal_col: missing.append('калории')
        return jsonify({'error': f'CSV не содержит обязательных колонок: {", ".join(missing)}. Заголовки: {", ".join(reader.fieldnames)}'}), 400
    
    with get_db() as conn:
        meal_types = conn.execute('SELECT id, type_name FROM meal_type').fetchall()
        meal_type_map = {mt['type_name'].lower(): mt['id'] for mt in meal_types}
        meal_type_synonyms = {
            'завтрак': 'завтрак', 'breakfast': 'завтрак',
            'ланч': 'ланч', 'lunch': 'ланч',
            'обед': 'обед', 'dinner': 'обед',
            'полдник': 'полдник', 'snack': 'полдник',
            'ужин': 'ужин', 'supper': 'ужин'
        }
        
        prog = conn.execute('SELECT id FROM programs WHERE id = ?', (program_id,)).fetchone()
        if not prog:
            return jsonify({'error': 'Программа не найдена'}), 404
        
        conn.execute('DELETE FROM program_dishes WHERE program_id = ?', (program_id,))
        
        inserted = 0
        errors = []
        for row_num, row in enumerate(reader, start=2):
            try:
                day_str = row.get(day_col, '').strip()
                if not day_str:
                    errors.append(f'Строка {row_num}: пустой день')
                    continue
                day = int(float(day_str))
                if day < 1 or day > 21:
                    errors.append(f'Строка {row_num}: день должен быть от 1 до 21 (получено {day})')
                    continue
                
                meal_type_raw = row.get(meal_type_col, '').strip().lower()
                if not meal_type_raw:
                    errors.append(f'Строка {row_num}: тип приёма не указан')
                    continue
                meal_type_key = meal_type_synonyms.get(meal_type_raw, meal_type_raw)
                meal_type_id = meal_type_map.get(meal_type_key)
                if not meal_type_id:
                    if meal_type_raw.isdigit():
                        meal_type_id = int(meal_type_raw)
                    else:
                        errors.append(f'Строка {row_num}: неизвестный тип приёма "{meal_type_raw}"')
                        continue
                
                dish_name = row.get(name_col, '').strip()
                if not dish_name:
                    errors.append(f'Строка {row_num}: название блюда пустое')
                    continue
                
                weight = row.get(weight_col, '').strip()
                if not weight:
                    errors.append(f'Строка {row_num}: вес не указан')
                    continue
                
                kkal_str = row.get(kkal_col, '').strip()
                if not kkal_str:
                    errors.append(f'Строка {row_num}: калории не указаны')
                    continue
                kkal = int(float(kkal_str.replace(',', '.')))
                
                protein = 0.0
                if protein_col and row.get(protein_col, '').strip():
                    try:
                        protein = float(row[protein_col].replace(',', '.'))
                    except:
                        pass
                fat = 0.0
                if fat_col and row.get(fat_col, '').strip():
                    try:
                        fat = float(row[fat_col].replace(',', '.'))
                    except:
                        pass
                carbs = 0.0
                if carbs_col and row.get(carbs_col, '').strip():
                    try:
                        carbs = float(row[carbs_col].replace(',', '.'))
                    except:
                        pass
                
                conn.execute('''
                    INSERT INTO program_dishes (program_id, day_num, meal_type_id, dish_name, weight, kkal)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (program_id, day, meal_type_id, dish_name, weight, kkal))
                
                existing_nut = conn.execute('SELECT 1 FROM dish_nutrition WHERE dish_name = ?', (dish_name,)).fetchone()
                if not existing_nut:
                    conn.execute('''
                        INSERT INTO dish_nutrition (dish_name, calories, protein, fat, carbs)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (dish_name, float(kkal), protein, fat, carbs))
                
                inserted += 1
            except Exception as e:
                errors.append(f'Строка {row_num}: {str(e)}')
        
        conn.commit()
        return jsonify({
            'success': True,
            'inserted': inserted,
            'errors': errors,
            'message': f'Импортировано {inserted} блюд. Ошибок: {len(errors)}'
        })

# ---------------------- API ДЛЯ СОЗДАНИЯ, ПЕРЕИМЕНОВАНИЯ И УДАЛЕНИЯ ПРОГРАММ ----------------------
@app.route('/api/create_program', methods=['POST'])
def create_program():
    data = request.json
    name = data.get('name', '').strip()
    calories = data.get('calories')
    if not name:
        return jsonify({'error': 'Название программы обязательно'}), 400
    if not calories or not isinstance(calories, int) or calories < 500 or calories > 3000:
        return jsonify({'error': 'Калорийность должна быть целым числом от 500 до 3000'}), 400
    with get_db() as conn:
        cursor = conn.execute('INSERT INTO programs (name, calories) VALUES (?, ?)', (name, calories))
        program_id = cursor.lastrowid
        conn.commit()
        return jsonify({'id': program_id, 'name': name, 'calories': calories, 'success': True}), 201

@app.route('/api/rename_program/<int:program_id>', methods=['PUT'])
def rename_program(program_id):
    data = request.json
    new_name = data.get('name', '').strip()
    if not new_name:
        return jsonify({'error': 'Название не может быть пустым'}), 400
    with get_db() as conn:
        prog = conn.execute('SELECT id FROM programs WHERE id = ?', (program_id,)).fetchone()
        if not prog:
            return jsonify({'error': 'Программа не найдена'}), 404
        conn.execute('UPDATE programs SET name = ? WHERE id = ?', (new_name, program_id))
        conn.commit()
        return jsonify({'success': True})

@app.route('/api/delete_program/<int:program_id>', methods=['DELETE'])
def delete_program(program_id):
    with get_db() as conn:
        prog = conn.execute('SELECT id FROM programs WHERE id = ?', (program_id,)).fetchone()
        if not prog:
            return jsonify({'error': 'Программа не найдена'}), 404
        conn.execute('DELETE FROM program_dishes WHERE program_id = ?', (program_id,))
        conn.execute('DELETE FROM programs WHERE id = ?', (program_id,))
        conn.commit()
        return jsonify({'success': True})

# ---------------------- API ДЛЯ ОБЪЕДИНЁННОГО СПИСКА БЛЮД ----------------------
@app.route('/api/all_dishes_unified', methods=['GET'])
def get_all_dishes_unified():
    program_id = request.args.get('program_id', type=int)
    with get_db() as conn:
        if program_id:
            prog = conn.execute('SELECT id, name, calories FROM programs WHERE id = ?', (program_id,)).fetchone()
            if not prog:
                return jsonify({'error': 'Программа не найдена'}), 404
            program_list = [(prog['id'], prog['name'], prog['calories'])]
        else:
            program_list = [(p['id'], p['name'], p['calories']) for p in conn.execute('SELECT id, name, calories FROM programs').fetchall()]
        
        all_dishes = []
        for pid, pname, pcal in program_list:
            rows = conn.execute('''
                SELECT pd.id, pd.day_num, pd.dish_name, pd.weight, pd.kkal, pd.meal_type_id,
                       mt.type_name as meal_type_name
                FROM program_dishes pd
                JOIN meal_type mt ON pd.meal_type_id = mt.id
                WHERE pd.program_id = ?
            ''', (pid,)).fetchall()
            for r in rows:
                nut = conn.execute('SELECT protein, fat, carbs FROM dish_nutrition WHERE dish_name = ?', (r['dish_name'],)).fetchone()
                all_dishes.append({
                    'id': r['id'],
                    'program_id': pid,
                    'program_name': pname,
                    'program_calories': pcal,
                    'day': r['day_num'],
                    'meal_type_id': r['meal_type_id'],
                    'meal_type_name': r['meal_type_name'],
                    'name': r['dish_name'],
                    'weight': r['weight'],
                    'kkal': r['kkal'],
                    'protein': nut['protein'] if nut else 0,
                    'fat': nut['fat'] if nut else 0,
                    'carbs': nut['carbs'] if nut else 0
                })
        return jsonify(all_dishes)

# ---------------------- СТРАНИЦЫ ----------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/full_menu_view')
def full_menu_view():
    return render_template('full_menu.html')

@app.route('/all_dishes_view')
def all_dishes_view():
    return render_template('all_dishes.html')

@app.route('/food_admin')
def food_admin():
    return render_template('food_admin.html')

@app.route('/user_full_menu/<int:user_id>')
def user_full_menu(user_id):
    return render_template('user_full_menu.html', user_id=user_id)

@app.route('/programs')
def programs():
    return render_template('programs.html')

if __name__ == '__main__':
    init_db()
    app.run(debug=True)