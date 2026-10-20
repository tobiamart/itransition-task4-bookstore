"""Natural-language Q&A over the cleaned store data.

Gemini never sees the raw data and never does arithmetic: it calls the tools below,
which compute exact answers with DuckDB, and only phrases the result.
POST /api/chat  {"message": str, "store": "STORE1", "history": [{"role": "user"|"model", "text": str}]}
"""
import datetime
import decimal
import json
import os
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
TABLES = ROOT / 'store_data'
SUMMARIES = ROOT / 'data'
MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash')
FALLBACK_MODEL = os.environ.get('GEMINI_FALLBACK_MODEL', 'gemini-3.5-flash-lite')
MAX_ROWS = 100
MAX_STEPS = 8
TIME_BUDGET = 52  # seconds; Vercel maxDuration is 60

STORES = json.loads((SUMMARIES / 'index.json').read_text())


# ---------------------------------------------------------------- data

def connect():
    con = duckdb.connect()
    src = TABLES.as_posix()
    con.execute(f"""
        CREATE TABLE orders AS SELECT * REPLACE (
            CAST(unit_price_usd AS DECIMAL(10,2)) AS unit_price_usd,
            CAST(paid_price AS DECIMAL(12,2)) AS paid_price)
        FROM read_parquet('{src}/*_orders.parquet');
        CREATE TABLE users AS SELECT * FROM read_parquet('{src}/*_users.parquet');
        CREATE TABLE books AS SELECT * FROM read_parquet('{src}/*_books.parquet');
    """)
    # Sandbox: no file/network access and settings cannot be changed back by queries.
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con


CON = connect()


def to_json(v):
    if isinstance(v, decimal.Decimal):
        return float(v) if v != v.to_integral_value() else int(v)
    if isinstance(v, (datetime.date, datetime.datetime)):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [to_json(x) for x in v]
    return v


def query(sql, params=None):
    cur = CON.cursor()
    rel = cur.execute(sql, params or [])
    cols = [d[0] for d in rel.description]
    rows = rel.fetchmany(MAX_ROWS + 1)
    out = {'columns': cols, 'rows': [[to_json(v) for v in r] for r in rows[:MAX_ROWS]]}
    if len(rows) > MAX_ROWS:
        out['truncated'] = f'only the first {MAX_ROWS} rows are shown; aggregate or add LIMIT'
    return out


# ---------------------------------------------------------------- tools

def run_sql(sql):
    try:
        statements = CON.cursor().extract_statements(sql)
        if len(statements) != 1:
            return {'error': 'exactly one statement is allowed'}
        if statements[0].type != duckdb.StatementType.SELECT:
            return {'error': 'only SELECT / WITH queries are allowed'}
        return query(statements[0].query)
    except duckdb.Error as e:
        return {'error': str(e).splitlines()[0][:300]}


def get_store_summary(store):
    if store not in STORES:
        return {'error': f'unknown store, use one of {STORES}'}
    d = json.loads((SUMMARIES / f'{store}.json').read_text())
    d.pop('daily_revenue', None)
    return d


def find_user(text, store=None):
    """Search user records and return every record (alias) of the matching real persons."""
    t = text.strip().lower()
    digits = re.sub(r'\D', '', t)
    cond = ["lower(name) LIKE ?", "lower(email) LIKE ?", "lower(address) LIKE ?"]
    params = [f'%{t}%'] * 3
    if len(digits) >= 4:
        cond.append("phone LIKE ?")
        params.append(f'%{digits}%')
    if t.isdigit():
        cond.append("user_id = ?")
        params.append(int(t))
    where = '(' + ' OR '.join(cond) + ')'
    if store:
        where += ' AND store = ?'
        params.append(store)
    persons = CON.cursor().execute(
        f"SELECT DISTINCT store, person_id FROM users WHERE {where} LIMIT 21", params).fetchall()
    if not persons:
        return {'matches': 0}
    result = []
    for st, pid in persons[:20]:
        recs = query("SELECT user_id, name, address, phone, email FROM users "
                     "WHERE store = ? AND person_id = ? ORDER BY user_id", [st, pid])
        stats = query("SELECT count(*) AS orders, coalesce(sum(quantity),0) AS books_bought, "
                      "coalesce(sum(paid_price),0) AS total_spent_usd FROM orders "
                      "WHERE store = ? AND person_id = ?", [st, pid])
        result.append({'store': st, 'person_id': pid, 'records': recs,
                       **dict(zip(stats['columns'], stats['rows'][0]))})
    return {'matches': len(persons), 'persons': result,
            **({'note': 'more than 20 persons match, refine the search'} if len(persons) > 20 else {})}


TOOLS = {'run_sql': run_sql, 'get_store_summary': get_store_summary, 'find_user': find_user}

DECLARATIONS = [
    {'name': 'get_store_summary',
     'description': 'Official precomputed KPIs of one store: top 5 days by revenue, number of unique (real) users, '
                    'number of unique author sets, most popular author set and copies sold, best buyer ids '
                    '(all aliases) and spending, total revenue, record counts. Prefer this for these exact KPIs.',
     'parameters': {'type': 'object', 'properties': {'store': {'type': 'string', 'enum': STORES}},
                    'required': ['store']}},
    {'name': 'find_user',
     'description': 'Find a customer by part of name, e-mail, phone, address or user id. Returns every record '
                    '(alias) of each matching real person with their order count and total spending.',
     'parameters': {'type': 'object', 'properties': {
         'text': {'type': 'string'},
         'store': {'type': 'string', 'enum': STORES, 'description': 'optional store filter'}},
         'required': ['text']}},
    {'name': 'run_sql',
     'description': 'Run one read-only DuckDB SELECT query over tables orders, users, books (schema in the system '
                    'prompt). Use it for any count, sum, ranking, filter or comparison. Returns at most '
                    f'{MAX_ROWS} rows.',
     'parameters': {'type': 'object', 'properties': {'sql': {'type': 'string'}}, 'required': ['sql']}},
]

SYSTEM = f"""You are the data assistant of a bookstore BI dashboard. Stores: {', '.join(STORES)}.
You cannot see the data. Every fact and number in your answer MUST come from a tool result in this conversation;
never estimate, guess or do arithmetic yourself - compute everything (sums, counts, averages, differences, ranks)
in SQL. If the tools cannot answer, say so plainly.

DuckDB tables (all stores together; ALWAYS filter by store unless the user compares stores):
- orders(store, order_id, user_id, person_id, book_id, quantity, unit_price_usd DECIMAL, paid_price DECIMAL,
  ts TIMESTAMP, date DATE, year, month, day, shipping)
- users(store, user_id, person_id, name, address, phone, email)
- books(store, book_id, title, authors VARCHAR[], author_set VARCHAR, n_authors, genre, publisher, year)
Definitions:
- Prices are cleaned and in USD (EUR converted at 1.2). revenue = SUM(paid_price); paid_price = quantity * unit_price_usd.
- A real customer is person_id (records of the same person with a changed name/alias, address, phone or e-mail
  share person_id; person_id is unique only within a store). "Unique users" = COUNT(DISTINCT person_id) over users.
  When listing a customer, list ALL user_id values of that person_id.
- Books sold = SUM(quantity). An author set is author_set (one book's co-authors, e.g. 'A; B').
  "Most popular author" means the author SET with the most books sold (as in get_store_summary) - answer with
  that set; only if the user explicitly asks about individual authors, count per person with UNNEST(authors). Join orders.book_id = books.book_id AND orders.store = books.store.
- phone holds digits only; email is lower case.
Answering:
- The user is looking at store {{store}}; use it when no store is mentioned.
- Reply in exactly the same language as the user's last message (English question -> English answer). Be concise.
- Dates as YYYY-MM-DD, money as $1,234.56, id lists as [id1, id2]. Mention ties if the result has them.
"""


# ---------------------------------------------------------------- Gemini

class Busy(RuntimeError):
    """Rate limit (429) or overload (503) - worth retrying or switching model."""


def gemini(contents, system, model, timeout):
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        raise RuntimeError('GEMINI_API_KEY is not configured')
    body = {'systemInstruction': {'parts': [{'text': system}]}, 'contents': contents,
            'tools': [{'functionDeclarations': DECLARATIONS}],
            'generationConfig': {'temperature': 0}}
    req = urllib.request.Request(
        f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'x-goog-api-key': key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (TimeoutError, urllib.error.URLError) as e:
        if isinstance(e, urllib.error.HTTPError):
            raise _http_error(e)
        raise Busy(f'timeout or network error: {e}')


def _http_error(e):
    text = e.read().decode(errors='replace')
    if e.code in (429, 500, 503):
        return Busy(text)
    return RuntimeError(f'Gemini API error {e.code}: {text[:300]}')


def call_model(contents, system, state):
    """Free tier has a few requests per minute and models are sometimes overloaded: switch to the fallback
    model before any tool call (thought signatures are model-specific), otherwise wait shortly and retry.
    Everything stays within the time budget of one serverless request."""
    while True:
        left = state['deadline'] - time.monotonic()
        if left < 3:
            raise RuntimeError('The AI model is busy right now (free tier). Please try again in a minute.')
        try:
            return gemini(contents, system, state['model'], timeout=min(15 if state['fresh'] else 25, left))
        except Busy as e:
            if state['model'] != FALLBACK_MODEL and state['fresh']:
                state['model'] = FALLBACK_MODEL
                continue
            delay = re.search(r'"retryDelay":\s*"(\d+)', str(e))
            time.sleep(max(0, min(int(delay[1]) if delay else 4, 8, state['deadline'] - time.monotonic() - 5)))


def answer(message, store=None, history=()):
    store = store if store in STORES else STORES[0]
    contents = [{'role': h['role'], 'parts': [{'text': h['text']}]}
                for h in list(history)[-8:] if h.get('role') in ('user', 'model') and h.get('text')]
    contents.append({'role': 'user', 'parts': [{'text': message}]})
    system = SYSTEM.replace('{store}', store)
    trace = []
    state = {'model': MODEL, 'fresh': True, 'deadline': time.monotonic() + TIME_BUDGET}
    for _ in range(MAX_STEPS):
        resp = call_model(contents, system, state)
        state['fresh'] = False
        cand = (resp.get('candidates') or [{}])[0]
        content = cand.get('content') or {'role': 'model', 'parts': []}
        parts = content.get('parts', [])
        calls = [p['functionCall'] for p in parts if 'functionCall' in p]
        if not calls:
            text = ''.join(p.get('text', '') for p in parts if not p.get('thought')).strip()
            return {'answer': text or 'Sorry, I could not produce an answer.', 'trace': trace, 'model': state['model']}
        contents.append(content)
        responses = []
        for c in calls:
            fn = TOOLS.get(c['name'])
            args = c.get('args') or {}
            try:
                result = fn(**args) if fn else {'error': f"unknown tool {c['name']}"}
            except Exception as e:  # report tool errors back to the model instead of failing the request
                result = {'error': str(e)[:300]}
            trace.append({'tool': c['name'], 'args': args, 'result': result})
            fr = {'name': c['name'], 'response': {'result': result}}
            if 'id' in c:
                fr['id'] = c['id']
            responses.append({'functionResponse': fr})
        contents.append({'role': 'user', 'parts': responses})
    return {'answer': 'Sorry, the question needed too many steps. Please make it more specific.', 'trace': trace}


# ---------------------------------------------------------------- HTTP

class handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, {'status': 'ok', 'model': MODEL, 'stores': STORES})

    def do_POST(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
            message = str(data.get('message', '')).strip()[:2000]
            if not message:
                return self._send(400, {'error': 'message is required'})
            self._send(200, answer(message, data.get('store'), data.get('history') or []))
        except Exception as e:
            self._send(500, {'error': str(e)[:500]})
