"""Task 4: load, clean and analyse book-store data (orders / users / books).

Usage: python analysis.py
Reads raw files from data/STORE*, writes cleaned results to dashboard/data/*.json and charts to charts/*.png.
"""
import json
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
import yaml

ROOT = Path(__file__).parent
RAW = ROOT / 'data'
OUT = ROOT / 'dashboard' / 'data'
CHARTS = ROOT / 'charts'
TABLES = ROOT / 'dashboard' / 'store_data'

EUR_TO_USD = Decimal('1.2')
CENT = Decimal('0.01')
MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
MISSING = {'', 'null', 'nan', 'none', 'n/a', '-'}


# ---------------------------------------------------------------- loading

def load_store(folder):
    books = pd.DataFrame(yaml.safe_load((folder / 'books.yaml').read_text(encoding='utf-8')))
    books.columns = [c.lstrip(':') for c in books.columns]
    return {
        'name': folder.name,
        'orders': pd.read_parquet(folder / 'orders.parquet'),
        'users': pd.read_csv(folder / 'users.csv', dtype=str, keep_default_na=False),
        'books': books,
    }


def discover_stores():
    return [load_store(d) for d in sorted(RAW.iterdir()) if d.is_dir() and (d / 'orders.parquet').exists()]


# ---------------------------------------------------------------- cleaning

PRICE_RE = re.compile(
    r'^\s*(?P<c1>\$|€|USD|EUR)?\s*(?P<int>\d+)(?:(?:\.|¢|\$|€)(?P<frac>\d*))?\s*¢?\s*(?P<c2>\$|€|USD|EUR)?\s*$',
    re.I)


def parse_price(raw):
    """'$70¢50', '21€99¢', 'USD 16.', '49.5 €', 'EUR14.99' -> Decimal USD rounded to cents."""
    s = str(raw).strip()
    m = PRICE_RE.match(s)
    if not m:
        raise ValueError(f'bad price: {raw!r}')
    cur = (m['c1'] or m['c2'] or '').upper()
    if not cur:  # currency between dollars and cents, e.g. 21€99¢
        cur = '€' if '€' in s else '$'
    frac = (m['frac'] or '').ljust(2, '0')[:2]
    value = Decimal(f"{m['int']}.{frac}")
    if cur in ('€', 'EUR'):
        value *= EUR_TO_USD
    return value.quantize(CENT, ROUND_HALF_UP)


def parse_timestamp(raw):
    s = str(raw).strip()
    # date part
    if m := re.search(r'(\d{4})-(\d{1,2})-(\d{1,2})', s):              # 2025-09-20
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    elif m := re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', s):         # 20.04.2025
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
    elif m := re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', s):         # 04/23/26 (US)
        mo, d, y = int(m[1]), int(m[2]), int(m[3])
        y += 2000 if y < 100 else 0
    elif m := re.search(r'(\d{1,2})-([A-Za-z]+)-(\d{4})', s):         # 6-October-2025
        d, mo, y = int(m[1]), MONTHS[m[2][:3].lower()], int(m[3])
    elif m := re.search(r'[A-Za-z]{3}\s+([A-Za-z]{3})\s+(\d{1,2})\s.*?(\d{4})$', s):  # Wed Jan 14 22:43:51 2026
        mo, d, y = MONTHS[m[1].lower()], int(m[2]), int(m[3])
    else:
        raise ValueError(f'bad timestamp: {raw!r}')
    # time part
    t = re.search(r'(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*([AaPp])?\.?\s*[Mm]?', s)
    h, mi, sec = int(t[1]), int(t[2]), int(t[3] or 0)
    if t[4]:
        h = h % 12 + (12 if t[4].lower() == 'p' else 0)
    return pd.Timestamp(year=y, month=mo, day=d, hour=h, minute=mi, second=sec)


def clean_orders(o):
    o = o.copy()
    o = o.drop_duplicates()
    o = o.drop_duplicates(subset=[c for c in o.columns if c != 'id'])
    o = o.drop_duplicates(subset='id')
    o['quantity'] = pd.to_numeric(o['quantity'], errors='coerce')
    o = o[o['quantity'] > 0]
    o['unit_price'] = o['unit_price'].map(parse_price)
    o['timestamp'] = o['timestamp'].map(parse_timestamp)
    o['shipping'] = o['shipping'].map(lambda v: None if pd.isna(v) or str(v).strip().lower() in MISSING
                                      else str(v).strip())
    o['paid_price'] = [p * int(q) for p, q in zip(o['unit_price'], o['quantity'])]
    o['date'] = o['timestamp'].dt.date
    o['year'] = o['timestamp'].dt.year
    o['month'] = o['timestamp'].dt.month
    o['day'] = o['timestamp'].dt.day
    return o.astype({'id': 'int64', 'user_id': 'int64', 'book_id': 'int64', 'quantity': 'int64'})


def norm_text(v):
    v = re.sub(r'\s+', ' ', str(v)).strip()
    return None if v.lower() in MISSING else v


def clean_users(u):
    u = u.copy()
    u['id'] = u['id'].astype('int64')
    u = u.drop_duplicates(subset='id')
    for c in ('name', 'address', 'email'):
        u[c] = u[c].map(norm_text)
    u['email'] = u['email'].str.lower()
    u['phone'] = u['phone'].map(lambda v: re.sub(r'\D', '', str(v)) or None)
    return u


def clean_books(b):
    b = b.copy()
    b = b.drop_duplicates(subset='id')
    for c in ('title', 'author', 'genre', 'publisher'):
        b[c] = b[c].map(norm_text)
    b['year'] = pd.to_numeric(b['year'].map(lambda v: str(v).strip()), errors='coerce')
    b.loc[(b['year'] < 1000) | (b['year'] > 2026), 'year'] = pd.NA
    b['year'] = b['year'].astype('Int64')
    b['authors'] = b['author'].map(
        lambda a: tuple(sorted({x.strip() for x in a.split(',') if x.strip()})) if a else ())
    return b


# ---------------------------------------------------------------- analysis

def resolve_users(u):
    """Union-find: two records are the same person if at least 3 of 4 fields match."""
    parent = {i: i for i in u['id']}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    fields = ['name', 'address', 'phone', 'email']
    for skip in fields:
        key = [f for f in fields if f != skip]
        for _, ids in u.dropna(subset=key).groupby(key)['id']:
            ids = list(ids)
            for other in ids[1:]:
                parent[find(other)] = find(ids[0])
    return u['id'].map(find)


def analyse(store):
    orders = clean_orders(store['orders'])
    users = clean_users(store['users'])
    books = clean_books(store['books'])
    users['person'] = resolve_users(users)

    orphans = {'orders_without_user': int((~orders.user_id.isin(users.id)).sum()),
               'orders_without_book': int((~orders.book_id.isin(books.id)).sum())}

    daily = orders.groupby('date')['paid_price'].sum().sort_index()
    top5 = daily.sort_values(ascending=False, kind='stable').head(5)

    sold = orders.merge(books[['id', 'authors']], left_on='book_id', right_on='id', suffixes=('', '_b'))
    by_set = sold.groupby('authors')['quantity'].sum().sort_values(ascending=False)
    best_count = by_set.iloc[0]
    popular = [list(a) for a, q in by_set.items() if q == best_count]

    spend = orders.merge(users[['id', 'person']], left_on='user_id', right_on='id', suffixes=('', '_u'))
    by_person = spend.groupby('person')['paid_price'].sum().sort_values(ascending=False)
    best_person = by_person.index[0]
    best_ids = sorted(int(i) for i in users.loc[users.person == best_person, 'id'])

    return {
        'store': store['name'],
        'counts': {'orders': len(orders), 'user_records': len(users), 'books': len(books), **orphans},
        'top5_days': [{'date': d.strftime('%Y-%m-%d'), 'revenue': str(v)} for d, v in top5.items()],
        'unique_users': int(users['person'].nunique()),
        'unique_author_sets': int(books['authors'].nunique()),
        'most_popular_authors': popular,
        'most_popular_sold': int(best_count),
        'best_buyer_ids': best_ids,
        'best_buyer_total': str(by_person.iloc[0]),
        'total_revenue': str(orders['paid_price'].sum()),
        'daily_revenue': [{'date': d.strftime('%Y-%m-%d'), 'revenue': str(v)} for d, v in daily.items()],
    }, daily, (orders, users, books)


def export_tables(name, orders, users, books):
    """Write cleaned tables for the chat API (queried with DuckDB)."""
    persons = users.set_index('id')['person']
    o = pd.DataFrame({
        'store': name, 'order_id': orders['id'], 'user_id': orders['user_id'],
        'person_id': orders['user_id'].map(persons).astype('Int64'), 'book_id': orders['book_id'],
        'quantity': orders['quantity'], 'unit_price_usd': orders['unit_price'].astype(float),
        'paid_price': orders['paid_price'].astype(float), 'ts': orders['timestamp'],
        'date': orders['date'], 'year': orders['year'], 'month': orders['month'],
        'day': orders['day'], 'shipping': orders['shipping']})
    u = pd.DataFrame({'store': name, 'user_id': users['id'], 'person_id': users['person'],
                      'name': users['name'], 'address': users['address'], 'phone': users['phone'],
                      'email': users['email']})
    b = pd.DataFrame({'store': name, 'book_id': books['id'], 'title': books['title'],
                      'authors': books['authors'].map(list), 'author_set': books['authors'].map('; '.join),
                      'n_authors': books['authors'].map(len), 'genre': books['genre'],
                      'publisher': books['publisher'], 'year': books['year']})
    TABLES.mkdir(parents=True, exist_ok=True)
    for t, df in (('orders', o), ('users', u), ('books', b)):
        df.to_parquet(TABLES / f'{name}_{t}.parquet', index=False)


def plot(daily, title, path):
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(pd.to_datetime(daily.index), daily.astype(float).values, lw=1)
    ax.set_title(title)
    ax.set_ylabel('Revenue, $')
    ax.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    CHARTS.mkdir(exist_ok=True)
    index = []
    for store in discover_stores():
        name = store['name']
        result, daily, tables = analyse(store)
        export_tables(name, *tables)
        (OUT / f'{name}.json').write_text(json.dumps(result, indent=1), encoding='utf-8')
        plot(daily, f'{name}: daily revenue', CHARTS / f'{name}.png')
        index.append(name)
        print(f'== {name}')
        for k in ('counts', 'top5_days', 'unique_users', 'unique_author_sets', 'most_popular_authors',
                  'most_popular_sold', 'best_buyer_ids', 'best_buyer_total'):
            print(f'  {k}: {result[k]}')
    (OUT / 'index.json').write_text(json.dumps(index), encoding='utf-8')


if __name__ == '__main__':
    main()
