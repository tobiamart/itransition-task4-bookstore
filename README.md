# Task 4 – Bookstore data cleaning & dashboard

Live dashboard: https://task4-bookstore-dashboard.vercel.app

## Structure

```
data/STORE*/            raw input (orders.parquet, users.csv, books.yaml)
analysis.py             cleaning + analysis (part 1)
charts/                 daily revenue charts (PNG)
dashboard/index.html    BI dashboard (Bootstrap + Chart.js)
dashboard/data/         per-store results (JSON) used by the dashboard
dashboard/store_data/   cleaned tables (parquet) queried by the chat
dashboard/api/chat.py   "Ask your data" chat backend (part 2, Vercel Python function)
test_chat.py            chat answers checked against known results
```

## Run

```bash
pip install -r requirements.txt
python analysis.py        # reads data/STORE*, writes dashboard/data, dashboard/store_data and charts
```

## Part 1 – what `analysis.py` does

- **Loading** – every `data/STORE*` folder (`orders.parquet`, `users.csv`, `books.yaml`) is processed separately.
- **Orders** – removes duplicates, parses ~130 timestamp layouts (`YYYY-MM-DD`, `DD.MM.YYYY`, US `MM/DD/YY`, `DD-Mon-YYYY`, ctime, 12/24h with `am`/`P.M.`), parses prices in `$`/`USD`/`€`/`EUR` incl. `70¢50` and `21€99¢`, converts EUR at €1 = $1.20, rounds to cents (`Decimal`) and computes `paid_price = quantity * unit_price`, plus `date`/`year`/`month`/`day`.
- **Users** – normalises whitespace, e-mail case, phone digits and empty/`NULL` values; real users are reconciled with union-find: two records are the same person when at least 3 of 4 fields (name, address, phone, e-mail) match.
- **Books** – cleans `year` (invalid values → NA); the author set of a book is the unordered set of comma-separated names.
- **Results** – top 5 days by revenue, unique users, unique author sets, most popular author set (by sold quantity), best buyer (all user ids of that person), daily revenue chart.

## Part 2 – "Ask your data" chat

Questions in natural language are answered by Gemini (`gemini-3.5-flash`, fallback `gemini-3.5-flash-lite`) with **function calling** – the model never receives the data itself, it has to call tools:

- `run_sql(sql)` – one read-only `SELECT` on in-memory DuckDB tables `orders`, `users`, `books` (all stores, cleaned). The sandbox disables external file/network access and rejects anything that is not a single SELECT statement; results are capped at 100 rows.
- `get_store_summary(store)` – the precomputed part 1 results.
- `find_user(text)` – finds a person by name / e-mail / phone / address and returns all of their alias records.

To limit hallucinations the system prompt requires every number to come from a tool result (all arithmetic is done in SQL), temperature is 0, and every answer in the UI shows the tool calls and SQL that produced it.

Local setup: put `GEMINI_API_KEY=...` in `.env`, then `python test_chat.py`. On Vercel the key is an encrypted environment variable.
