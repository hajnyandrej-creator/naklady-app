import functools
import os
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, session, redirect, make_response

# ── DB (rovnaká ako sklad) ────────────────────────────────────────────────────

DATABASE_URL = os.environ.get('DATABASE_URL', '')
POSTGRES = bool(DATABASE_URL)

if POSTGRES:
    import psycopg2
    import psycopg2.extras

    def get_db():
        import urllib.parse as up
        url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        r = up.urlparse(url)
        return psycopg2.connect(
            host=r.hostname, port=r.port or 5432,
            dbname=r.path.lstrip('/'), user=r.username,
            password=r.password, sslmode='require'
        )

    def qex(conn, sql, p=()):
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql.replace('?', '%s'), p if p else None)
        return cur

    def qrows(cur):
        return [dict(r) for r in (cur.fetchall() or [])]

    def qone(cur):
        r = cur.fetchone()
        return dict(r) if r else None

    def month_expr(col):
        return f"SUBSTRING({col}, 1, 7)"

else:
    import sqlite3
    _DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
    DB_PATH = os.path.join(_DATA_DIR, '..', 'sklad', 'sklad.db')

    def get_db():
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        return c

    def qex(conn, sql, p=()):
        return conn.execute(sql, p)

    def qrows(cur):
        return [dict(r) for r in cur.fetchall()]

    def qone(cur):
        r = cur.fetchone()
        return dict(r) if r else None

    def month_expr(col):
        return f"strftime('%Y-%m', {col})"


# ── Flask setup ───────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'naklady-dev-key')
APP_PASSWORD = os.environ.get('APP_PASSWORD', '')
WAREHOUSES = ['Bratislava', 'Trenčín']
MONTHS_SK = ['Január', 'Február', 'Marec', 'Apríl', 'Máj', 'Jún',
             'Júl', 'August', 'September', 'Október', 'November', 'December']


def current_warehouse():
    return session.get('warehouse', WAREHOUSES[0])


def fmt_month(ym):
    if not ym:
        return '—'
    try:
        y, m = ym.split('-')
        return f"{MONTHS_SK[int(m) - 1]} {y}"
    except Exception:
        return ym


def require_auth(f):
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        if APP_PASSWORD and not session.get('ok'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'session_expired'}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return wrapped


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not APP_PASSWORD:
        return redirect('/')
    if request.method == 'POST':
        if request.form.get('password') == APP_PASSWORD:
            session['ok'] = True
            return redirect('/')
        return render_template('login.html', error='Nesprávne heslo')
    return render_template('login.html', error=None)


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login' if APP_PASSWORD else '/')


# ── Main ──────────────────────────────────────────────────────────────────────

@app.route('/')
@require_auth
def index():
    resp = make_response(render_template('index.html',
        warehouse=current_warehouse(), warehouses=WAREHOUSES))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return resp


@app.route('/api/switch-warehouse', methods=['POST'])
@require_auth
def switch_warehouse():
    wh = (request.json or {}).get('warehouse', WAREHOUSES[0])
    if wh in WAREHOUSES:
        session['warehouse'] = wh
    return jsonify({'warehouse': session.get('warehouse', WAREHOUSES[0])})


# ── API ───────────────────────────────────────────────────────────────────────

@app.route('/api/months')
@require_auth
def api_months():
    """Vráti zoznam mesiacov kde existujú OUT pohyby pre daný sklad."""
    wh = current_warehouse()
    conn = get_db()
    try:
        cur = qex(conn,
            f"SELECT DISTINCT {month_expr('created_at')} AS month "
            "FROM movements WHERE type='OUT' AND COALESCE(warehouse, ?) = ? "
            "ORDER BY month DESC",
            (WAREHOUSES[0], wh))
        return jsonify([r['month'] for r in qrows(cur)])
    except Exception as e:
        app.logger.error('api_months: %s', e)
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/summary')
@require_auth
def api_summary():
    """
    Náklady podľa prevádzky za zvolený mesiac.
    Vracia: [{location, total_cost, total_qty, num_items}]
    """
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    wh = current_warehouse()
    conn = get_db()
    try:
        cur = qex(conn, f'''
            SELECT
                COALESCE(NULLIF(m.location, ''), '— bez prevádzky —') AS location,
                ROUND(CAST(SUM(m.quantity * p.unit_price) AS numeric), 2) AS total_cost,
                SUM(m.quantity) AS total_qty,
                COUNT(DISTINCT p.id) AS num_products
            FROM movements m
            JOIN products p ON p.id = m.product_id
            WHERE m.type = 'OUT'
              AND {month_expr('m.created_at')} = ?
              AND COALESCE(m.warehouse, ?) = ?
            GROUP BY location
            ORDER BY total_cost DESC
        ''', (month, WAREHOUSES[0], wh))
        rows = qrows(cur)
        # Celkové náklady za mesiac
        total = sum(float(r['total_cost'] or 0) for r in rows)
        for r in rows:
            r['total_cost'] = float(r['total_cost'] or 0)
            r['total_qty'] = float(r['total_qty'] or 0)
            r['num_products'] = int(r['num_products'] or 0)
            r['pct'] = round(r['total_cost'] / total * 100, 1) if total else 0
        return jsonify({'month': month, 'rows': rows, 'total': round(total, 2)})
    except Exception as e:
        import traceback
        app.logger.error('api_summary: %s\n%s', e, traceback.format_exc())
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/trend')
@require_auth
def api_trend():
    """
    Náklady podľa prevádzky za posledných N mesiacov.
    Vracia: {months: [...], locations: [...], data: {location: [cost_m1, cost_m2, ...]}}
    """
    wh = current_warehouse()
    n = request.args.get('n', 6, type=int)
    conn = get_db()
    try:
        # Nájdi posledných N mesiacov s OUT pohybmi
        cur = qex(conn,
            f"SELECT DISTINCT {month_expr('created_at')} AS month "
            "FROM movements WHERE type='OUT' AND COALESCE(warehouse, ?) = ? "
            "ORDER BY month DESC LIMIT ?",
            (WAREHOUSES[0], wh, n))
        months = [r['month'] for r in qrows(cur)]
        months_asc = list(reversed(months))

        if not months:
            return jsonify({'months': [], 'locations': [], 'data': {}})

        # Náklady pre každú prevádzku × mesiac
        placeholders = ','.join(['?' for _ in months])
        cur2 = qex(conn, f'''
            SELECT
                COALESCE(NULLIF(m.location, ''), '— bez prevádzky —') AS location,
                {month_expr('m.created_at')} AS month,
                ROUND(CAST(SUM(m.quantity * p.unit_price) AS numeric), 2) AS total_cost
            FROM movements m
            JOIN products p ON p.id = m.product_id
            WHERE m.type = 'OUT'
              AND {month_expr('m.created_at')} IN ({placeholders})
              AND COALESCE(m.warehouse, ?) = ?
            GROUP BY location, month
            ORDER BY location, month
        ''', (*months, WAREHOUSES[0], wh))
        rows = qrows(cur2)

        # Zostaviť štruktúru pre frontend
        locations_set = {}
        for r in rows:
            loc = r['location']
            if loc not in locations_set:
                locations_set[loc] = {m: 0.0 for m in months_asc}
            locations_set[loc][r['month']] = float(r['total_cost'] or 0)

        # Zoradiť prevádzky podľa celkových nákladov (zostupne)
        locations_sorted = sorted(
            locations_set.keys(),
            key=lambda l: sum(locations_set[l].values()),
            reverse=True
        )

        data = {loc: [locations_set[loc][m] for m in months_asc]
                for loc in locations_sorted}

        return jsonify({
            'months': months_asc,
            'locations': locations_sorted,
            'data': data
        })
    except Exception as e:
        import traceback
        app.logger.error('api_trend: %s\n%s', e, traceback.format_exc())
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/location-detail')
@require_auth
def api_location_detail():
    """Detail prevádzky: všetky produkty za zvolený mesiac s nákladmi."""
    location = request.args.get('location', '')
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    wh = current_warehouse()
    conn = get_db()
    try:
        db_location = '' if location == '— bez prevádzky —' else location
        cur = qex(conn, f'''
            SELECT p.code, p.name, p.unit,
                   SUM(m.quantity) AS qty,
                   p.unit_price,
                   ROUND(CAST(SUM(m.quantity * p.unit_price) AS numeric), 2) AS cost
            FROM movements m
            JOIN products p ON p.id = m.product_id
            WHERE m.type = 'OUT'
              AND COALESCE(NULLIF(m.location, ''), '') = ?
              AND {month_expr('m.created_at')} = ?
              AND COALESCE(m.warehouse, ?) = ?
            GROUP BY p.id, p.code, p.name, p.unit, p.unit_price
            ORDER BY cost DESC
        ''', (db_location, month, WAREHOUSES[0], wh))
        rows = qrows(cur)
        for r in rows:
            r['qty'] = float(r['qty'] or 0)
            r['unit_price'] = float(r['unit_price'] or 0)
            r['cost'] = float(r['cost'] or 0)
        total = sum(r['cost'] for r in rows)
        return jsonify({'location': location, 'month': month,
                        'rows': rows, 'total': round(total, 2)})
    except Exception as e:
        import traceback
        app.logger.error('api_location_detail: %s\n%s', e, traceback.format_exc())
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5002))
    print(f'\n  Náklady → http://localhost:{port}\n')
    app.run(debug=True, port=port, host='0.0.0.0')
