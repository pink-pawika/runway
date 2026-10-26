# Runway

Runway answers one question: **how long will my money last?**

It is a small spending tracker for a student budget. You upload a receipt or a statement, or you type a purchase. The app saves the amount in cents, sorts it into a category, and shows this month's spending against one monthly budget. An I-20 financial section can be turned into a monthly living amount, then rent is saved as a repeating charge and the budget becomes what is left.

The four screens are Activity, Analytics, Review, and I-20.

## Screenshots

Add your own images in a `screenshots/` folder before you submit the report. The app runs without these files.

![Activity](screenshots/activity.png)

![Analytics](screenshots/analytics.png)

![Review](screenshots/review.png)

![I-20](screenshots/i20.png)

![Usage footer](screenshots/usage.png)

## Setup on a Mac

Use Python 3.13. On some Macs, `python3` is 3.9, and that version cannot install the model library.

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 . The first visit asks you to create an account. The first account keeps a copy of any existing `data/spendsmart.db`. Later accounts start empty.

## Setup on Windows

```bat
py -3.13 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 and create an account the same way.

## Where the API key goes

Copy `.env.example` to a file named `.env`. Put that file in **one** of these places:

1. This project folder (the folder that contains `app/` and `README.md`).
2. The folder one level above this project. A fresh clone can stay key-free if the grader's `.env` is already in the parent folder.

The app checks the project folder first. If a key there is blank or missing, it uses the parent folder. A key already in the terminal is used only when neither file has one.

```
PORTKEY_API_KEY=your-course-key
```

`PORTKEY_API_KEY` always calls `gpt-6-luna`. `OPENAI_API_KEY` is used only when the course key is empty, and then `OPENAI_MODEL` is required. Do not commit `.env`. The app never prints the key.

After you add or move `.env`, stop the app and start it again. If the key is missing, the login screen and the app show a banner that names both folders.

Check the key without printing it:

```bash
python -m app.check_model
```

On Windows, use the same command after `.venv\Scripts\activate`. The source line should say `repo .env` or `parent .env`. That call counts toward the daily limit of 40 runs.

## Tests

From the project folder, with the virtual environment active:

```bash
python -W error::ResourceWarning -m unittest discover -s tests -v
```

Windows uses the same command. The tests do not call the real model. `tests/test_env.py` builds a temporary parent folder, writes a `.env` there, and checks that the app finds it.

## Sample data

`sample_data/` has small fake files. Upload them from Activity after you log in. They are not real receipts.

| File | What to expect |
| --- | --- |
| `injection_receipt.pdf` | A $4.50 cafe receipt with a hidden instruction and a merchant written as an HTML tag. The amount stays $4.50, the category stays Other, and the row waits in Review. The merchant is shown as text. |
| `mismatch_statement.pdf` | Two lines, $20.00 and $15.00, with a printed total of $50.00. Both lines wait in Review with a total-mismatch reason. |
| `thirteen_pages.pdf` | Rejected. PDFs are limited to 10 pages. |
| `not_a_pdf.pdf` | Rejected. The file is plain text with a `.pdf` name. The app reads the bytes, not the name. |
| `i20_financial.txt` | Fake yearly figures for reading. It is not an uploadable PDF. Type the living-expense number on the I-20 screen instead. |

A file larger than 10 MB is rejected before the model sees it. These sample checks do not need a model call except the two real PDFs, which do.

Safety scripts, each of which uses a temporary database and does not change your account:

```bash
python scripts/demo_injection.py
python scripts/measure_tokens.py
```

`demo_injection.py` spends one real model run. `measure_tokens.py` spends several, because it uploads two PDFs twice. Skip them if you are close to the daily limit. The footer shows how many runs are left.

## What the app stores

Amounts are integer cents. Expenses are positive. A green amount on a statement screenshot is money received and is left out of the lists. One monthly budget number covers all spending. Rent saved from the I-20 screen repeats every month and is included in the spent total. The budget becomes living expenses minus rent.

The usage footer shows tokens and cost for the last model call, today's totals, and the runs left today. Costs use the lecture prices in `app/llm.py`: input $0.10, cached input $0.01, and output $0.50 per million tokens.

## Fresh-clone check

A new folder should be running in under 10 minutes. This does not call the model.

1. Clone or copy the project into a new folder.
2. Put `.env` one level above that folder, with `PORTKEY_API_KEY`. Leave the project folder itself without a `.env`.
3. Follow the Mac or Windows setup above.
4. Run `python -m app.check_model` only if you want to confirm the source line. That call spends one daily run. Skip it for the timed check.
5. Run the tests. `tests/test_env.py` confirms the parent `.env` is found.
6. Open http://127.0.0.1:8000 and create an account.

The same check can be timed on this machine with a fake parent key:

```bash
python scripts/fresh_clone_check.py
```

It copies the project to a temporary folder, writes a placeholder key in the parent folder, installs the requirements, runs the tests, and prints the elapsed time. It fails if that takes more than 10 minutes.

## Known limits

- The model can be called 40 times per UTC day. One document is one run, including its single retry. The banner appears when the key is missing or the cap is reached.
- A receipt with a printed total is saved as one store row. A statement is saved line by line. A minus, red ink, or plain amount is spending. A green amount, or a plus, is money received and is left out.
- Category is chosen in code. A new merchant starts as Other. Approving it in Review remembers that category for next time. The document cannot set the category.
- A scanned PDF sends only the first 3 pages. Files are limited to 10 MB, 10 PDF pages, and 20 million pixels.
- The I-20 screen uses yearly living expenses only. It is not financial or immigration advice.
- Passwords stay in a local database. There is no email reset. The session cookie is for local HTTP, so it is not marked Secure.
- `cryptography` is pinned to 48.0.1 so older Macs can install a wheel.
- `python -m app.check_model`, `scripts/ping_llm.py`, and `scripts/test_image.py` each spend one real run.
