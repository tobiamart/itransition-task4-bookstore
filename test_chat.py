"""Ask the chat backend questions with known answers (from analysis.py) and check the replies.

Usage: python test_chat.py   (needs GEMINI_API_KEY in the environment or in .env)
"""
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
for line in (ROOT / '.env').read_text().splitlines() if (ROOT / '.env').exists() else []:
    k, _, v = line.partition('=')
    os.environ.setdefault(k.strip(), v.strip())
sys.path.insert(0, str(ROOT / 'dashboard' / 'api'))
import chat  # noqa: E402

S = {n: json.loads((ROOT / 'dashboard' / 'data' / f'{n}.json').read_text()) for n in chat.STORES}


def q(sql):
    return chat.run_sql(sql)['rows']


march = q("select sum(paid_price) from orders where store='STORE1' and year=2026 and month=3")[0][0]
genre = q("select genre from orders o join books b using(store, book_id) where store='STORE2' "
          "group by 1 order by sum(paid_price) desc limit 1")[0][0]
best_store = q("select store from orders group by 1 order by sum(paid_price) desc limit 1")[0][0]
olson = [r[0] for r in q("select user_id from users where store='STORE2' and person_id = "
                         "(select person_id from users where store='STORE2' and user_id=48749)")]

CASES = [
    ('STORE1', 'What are the top 5 days by revenue?', [d['date'] for d in S['STORE1']['top5_days']]),
    ('STORE2', 'How many unique users are there?', [str(S['STORE2']['unique_users'])]),
    ('STORE3', 'How many unique sets of authors are there?', [str(S['STORE3']['unique_author_sets'])]),
    ('STORE4', 'Who is the most popular author?', S['STORE4']['most_popular_authors'][0]),
    ('STORE5', 'Who is the best buyer? Give all their user ids.', [str(i) for i in S['STORE5']['best_buyer_ids']]),
    ('STORE1', 'What was the revenue in March 2026?', [f'{march:,.2f}']),
    ('STORE2', 'Which genre brought the most revenue?', [genre]),
    ('STORE1', 'Which store has the highest total revenue?', [best_store]),
    ('STORE2', 'Find the customer Katharina Olson and list all her user ids', [str(i) for i in olson]),
    ('STORE3', 'Ile unikalnych klientów jest w STORE3?', [str(S['STORE3']['unique_users'])]),
]

ok = 0
for store, question, expected in CASES:
    try:
        r = chat.answer(question, store)
    except RuntimeError as e:
        print('ERROR', e)
        time.sleep(10)
        continue
    norm = re.sub(r'[\s$,*]', '', r['answer'])
    missing = [e for e in expected if re.sub(r'[\s$,*]', '', e) not in norm]
    ok += not missing
    print(('PASS' if not missing else 'FAIL'), f'[{store}] {question}')
    print('   ', r['answer'].replace('\n', ' ')[:300])
    print('    model:', r.get('model'), 'tools:', [t['tool'] for t in r['trace']], *(['MISSING:', missing] if missing else []))
    time.sleep(20)  # stay within the free-tier rate limit
print(f'{ok}/{len(CASES)} passed')
