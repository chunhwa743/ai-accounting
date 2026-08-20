# AI Accounting Assistant

Reads a client's bank statement and supporting documents, codes each transaction
to a general ledger account, scores how sure it is, and routes what it cannot
resolve to an accountant. Corrections become durable memory, so the same client
needs less review each month.

Built for Singapore private limited companies: SGD, IRAS GST at 9%, CPF, and the
statement formats the local banks issue.

---

## What it does

```
bank statement (PDF, scan, CSV)          →  transactions extracted and verified
invoices, receipts, payroll (PDF, JPG,   →  matched to the payments that settled
DOCX, XLSX)                                 them
                                         →  every transaction coded to an account
                                            and a GST code
                                         →  a confidence score, and a decision
                                            about who needs to look
                                         →  a review pack, journal entries, and a
                                            list of questions for the client
```

The design assumption throughout: **the value is not in coding the easy 70%.**
A keyword matcher does that. The value is in reliably knowing which 30% it got
wrong, and in getting cheaper every month as the accountant teaches it.

---

## Setup

Requires **PostgreSQL 17+** running locally and **Python 3.12+**.

```bash
# 1  Databases
psql -U postgres -c "CREATE DATABASE aiacct;"
psql -U postgres -c "CREATE DATABASE aiacct_test;"

# 2  Application
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # Windows
# .venv/bin/python -m pip install -e ".[dev]"       # macOS / Linux

# 3  Configuration
cp .env.example .env        # set OPENAI_API_KEY; DATABASE_URL only if not default

# 4  Schema and master data
alembic upgrade head        # creates the nine tables
python scripts/seed_db.py   # chart of accounts, demo clients, firm staff

# 5  Test files
python scripts/generate_test_files.py   # renders data/testdata/*.md into PDFs, scans, CSVs

# 6  Run
python -m pytest                        # 83 tests, no API key needed
python scripts/demo_learning_loop.py    # the headline demo
python scripts/evaluate.py              # accuracy and calibration
uvicorn aiacct.api.main:app --reload    # API docs at /docs
```

Only `OPENAI_API_KEY` genuinely has to be set. Everything else has a working
default in `src/aiacct/config.py`, including `DATABASE_URL`.

The example uses the `postgres` superuser because that is the shortest path to a
working local setup. For anything shared, give the application its own role that
owns only its own database:

```bash
psql -U postgres -c "CREATE USER aiacct WITH PASSWORD '...';"
psql -U postgres -c "CREATE DATABASE aiacct OWNER aiacct;"
```

**Live versus offline.** Everything above runs offline against a deterministic
provider, so the tests and both demos need no API key. Add `--live` to use the
real model:

```bash
python scripts/demo_learning_loop.py --live
python scripts/evaluate.py --live
```

**Resetting:**

```bash
alembic downgrade base && alembic upgrade head && python scripts/seed_db.py
```

Note that LangGraph's checkpoint tables are not managed by Alembic and survive a
reset. That is why a run's thread id carries a random suffix rather than being
keyed on the run id alone - ids restart at 1 after a rebuild, and a bare
`run-1` would resume a previous run's checkpoint.

---

## Where things live

| Path | |
| --- | --- |
| `data/testdata/*.md` | **The test data, written down.** Transactions and documents in markdown, editable by someone who knows accounting rather than Python. Committed. |
| `data/generated/` | The PDFs, scans, images and CSVs rendered from that markdown. Build output, not committed. |
| `data/seeds/` | Master data loaded into the database: the chart of accounts, demo clients, firm staff. |
| `data/tax_codes.sg.yaml` | Fixed by legislation, read at runtime rather than seeded. |
| `data/confidence.yaml` | Scoring weights and routing thresholds. |
| `alembic/` | Migrations. The schema lives here, not in the models. |

The markdown is the source of truth for test transactions, and the answer key
lives beside each one:

```markdown
| Day | Description                    | Reference | Out     | In | Account | Tax | Why this is hard |
| 10  | PAYNOW-ACME SUPPLIES-88291     | PN88291   | 1090.00 |    | 720     | TX  | The description is legible and still says nothing about what was bought. Only the invoice reveals a laptop, which is capitalised rather than expensed. |
| 18  | LOAN REPAYMENT DBS 88291       | LN88291   | 1000.00 |    | split:900=800.00,437=200.00 | OP | One line, two accounts. The ratio comes from the loan schedule, which no model can know. |
| 26  | TRF 8891234                    |           | 780.00  |    | none    |     | No merchant token at all. Any confident answer would be invented. |
```

The `Account`, `Tax` and `Why this is hard` columns are the answer key. The
generator strips them out of everything it renders, so the pipeline only ever
sees the files - the markdown reaches nothing but the generator and
`scripts/evaluate.py`.

---

## The learning loop

Three months of one client, with a review between each. This is
`scripts/demo_learning_loop.py --live`, against `gpt-5.6-luna`:

```
period      txns  accuracy  auto-post  from rules  model calls  to review
2026-01       30      79%         7%           0           14         28
2026-02       20     100%         0%          17            2         20
2026-03       20     100%        53%          18            2         10
```

February auto-posts nothing, and that is the system working. The model misread
one line on the degraded scan - the per-row balance check caught it and named
line 9 exactly, `out by -60.00` - so the statement is recorded as not
reconciling, and every transaction on it carries that penalty. The coding was
still right; it simply was not trusted enough to post unreviewed.

Model calls fall from 14 to 2 and the review queue from 28 to 8, because
transactions resolved from rules the accountant taught rise from 0 to 18. Those
rules bypass the model entirely, so the improvement is in cost and latency as
well as accuracy.

Rules are **per client and applied deterministically**. `GRAB` means travel for a
design agency and a delivery cost for a restaurant, so a rule learned for one
never reaches the other — there is a test for exactly that.

---

## Is the confidence score real?

`scripts/evaluate.py --live` scores a first run — no corrections yet, so no
memory of the client at all — against the answer key the pipeline never sees:

```
  account accuracy            69%
  auto-posted                 5 (17%)
  auto-post precision        100%   <- of what nobody checked, how much was right
  raised as client queries    10, of which 2 were genuinely unanswerable

  Calibration - does the score mean anything?
    band              n   correct    actual
    0.90 - 1.00      11        11     100%
    0.75 - 0.90       6         6     100%
    0.60 - 0.75       3         1      33%
    below 0.60        9         2      22%
```

69% accuracy on a cold run looks low until you see where it goes: the model
returns *no account* for `GRAB *TRIP` (travel or staff meals?), for groceries at
NTUC (pantry or the director's shopping?), and for a PayNow to an individual
(contractor or drawings?). Those are counted as wrong here, but refusing to
guess on them is the behaviour the prompt asks for and the reason auto-post
precision is 100%.

**Auto-post precision is the number that matters.** Overall accuracy is easy to
reach and not very useful: a system that flags everything scores well on accuracy
while saving nobody any time. What counts is whether the things it posted without
anyone looking were right.

The calibration table answers the other half — a confidence of 0.9 should mean
roughly 90% correct. If it does not, the score is decoration.

---

## How it works

Three model calls, two phases, two gates.

```
UPLOAD    accountant picks the client, uploads files
          per file: hash, dedupe, store, create a Document row

PHASE 1 - per FILE, "what does this paperwork say?"
  call 1  CLASSIFY   page one only -> bank statement / invoice / receipt / other
  call 2  EXTRACT    the WHOLE file, with a schema for that type
          VALIDATE   deterministic, no model:
                       opening + money in - money out == closing
                       per row: balance_after chains correctly
                       dates fall inside the period
                       unclear fields x how much redundancy they carry
                     FAIL -> retry once with the error, then stop for a person

  gate 1  rarely fires: only when something genuinely could not be read

PHASE 2 - per TRANSACTION, "what does it mean?"
  1       RULE LOOKUP    learned patterns, longest first        no model
  2       MATCH DOCS     amount + date + vendor scoring         no model
  call 3  CATEGORISE     ONE batch, only what is still unresolved
  4       SCORE          deterministic blend                     no model
  5       ROUTE          hard gates, then bands                  no model

  gate 2  the normal one: everything uncertain, batched into one review session

REVIEW    approve, correct, or split; "always do this" writes a rule
EXPORT    review pack (XLSX), journal entries, client query list
```

A typical month — one statement of 45 lines plus three invoices — costs about
nine model calls, falling as rules accumulate.

### Why a graph and not an agent

The sequence of steps is known before the run starts, so agency buys nothing and
costs determinism, testability, and the ability to answer *"why was this coded to
489?"* with a stable record. You cannot build an audit trail on a trajectory that
differs every run.

The branching — digital PDF versus scan, rule hit versus model call — is a fixed
decision tree, which is a conditional edge rather than reasoning. There is
exactly one cycle in the whole graph: extract → validate → extract, capped at two
attempts, where **the validator decides to loop, never the model**.

That is the rule the whole system follows: *deterministic code decides control
flow; the model only ever fills in content.*

---

## Design decisions worth knowing

### Most outgoing payments are not expenses

A system that assumes they are books loan repayments as costs and understates
profit. The other side of a bank line may equally be a liability settled (GST to
IRAS, CPF, loan principal), an asset acquired (equipment above the client's
capitalisation threshold), equity withdrawn (director's drawings, which are not
deductible), or a transfer between the client's own accounts.

The chart of accounts marks the dangerous ones `risk_level: HIGH`, and those go
to a human regardless of confidence — because **confidence and consequence are
independent**. A model can be very sure a large transfer is drawings and be
wrong, and drawings change the tax computation.

### Money in and money out are stored exactly as printed

Never a signed amount, never a derived direction flag. A bank statement is
written from the *bank's* point of view, which is the mirror of the client's:
money arriving is a credit on the statement and a debit in the books. Conversion
to debits and credits happens in one function at journal-generation time, with
tests on it.

### The score is computed by code, not returned by the model

Three reasons it cannot come from the categorisation call:

- Most transactions never reach that call — they are resolved by a learned rule —
  and every allocation still needs a confidence.
- Two of the penalties come from extraction facts the call never sees: whether
  the description was partly guessed, and whether the statement reconciled.
- Self-reported floats are uncalibrated. Models cluster near 0.9 regardless of
  input, so routing on one would auto-post nearly everything.

What *is* trustworthy from the model is its **ranked alternatives**. A 0.05 gap
between first and second choice is an observation about how close the call was.

### Unclear fields: what matters is what kind of field it is

A single "how confident are you" number is uninterpretable — 0.6 could mean the
*amounts* were blurry, which arithmetic verifies, or the *descriptions* were,
which it cannot see. So the model reports, per field, what it could not read
cleanly, and a constant in the code decides what that means:

| Class | Fields | When unclear |
| --- | --- | --- |
| Redundant | descriptions, vendor names | Context recovers meaning → proceed with a penalty |
| Verifiable | amounts, balances, dates | Arithmetic and period bounds prove them → let the check decide |
| Identifier | account numbers, references, invoice numbers | Nothing can verify them → **escalate** |

Natural language is roughly half redundant, so `SINGT?L` has one plausible
reading. An identifier has none: one wrong digit points at a different real
thing. That is why an unclear account number stops a run and an unclear
description does not.

### Hardcoded vocabulary is avoided, and where it remains it is bounded

A list of English payment-rail words, or of bank header names, needs a new entry
for every bank, rail and language — and fails *silently* when it falls behind.
So the system asks the model for observations and keeps the policy in code:

| Question | How it is answered |
| --- | --- |
| Does this description name anybody? | The model reports `identifiable`. The only local claim is that a string with no letters names nobody — true in any language. |
| Which column is the date? | An alias table covers the known layouts for free; an unfamiliar header goes to the model rather than failing. |
| Is `03/05/2026` March or May? | Inferred from the whole column — any day above 12 settles it. If every date is ambiguous, that is **logged**, not guessed at silently. |
| What pattern should this rule match? | The model proposes it, once, at correction time. |

What stays hardcoded is bounded by something outside our control that does not
drift: file magic bytes and extensions are format standards; `FIELD_CLASS` and
the issue codes classify *our own* schema, so nothing external can invalidate
them. The one remaining word list is `RAIL_PREFIXES`, used only to derive a rule
pattern when no model is reachable — a degraded path, and its output is shown to
the accountant before anything is saved.

### A guessed merchant name never becomes a rule

The allocation still stands — a person approved it — but a rule keyed on a
half-read name would silently miscode every month until somebody noticed.

### `reconciles` has three states

`true` the arithmetic verified, `false` it did not, `null` **the check could not
run** because the export printed no balances. Collapsing null into either one
would mean recording an unverified extraction as verified, or escalating every
CSV for no reason.

---

## The test data

Two clients, written down in `data/testdata/*.md` and rendered by a seeded
generator, so regeneration reproduces the same files. The one exception is the
scanned statement: Pillow stamps a creation date into the PDF it writes, so its
bytes differ between runs even though its content does not.

The answer key lives in the markdown and is read only by `scripts/evaluate.py`,
never by the pipeline.

**Lumina Design Studio Pte Ltd** — GST-registered design agency, DBS:

| Period | Format | What it exercises |
| --- | --- | --- |
| Jan | Digital PDF | Text-layer extraction, a clean baseline |
| Feb | Scanned PDF | Vision path, and a smudged account number that stops at gate 1 |
| Mar | CSV, no balances | Deterministic parse, and the unverifiable case |

Plus supplier invoices (PDF), a receipt photo (JPG), and a payroll summary (DOCX).

**Kopi & Co Pte Ltd** — a coffee shop, to prove memory does not leak between
clients.

The transactions are chosen to be hard for an *accountant*, not merely hard for a
regex:

- **Not expenses at all** — an inter-account transfer, director's drawings, a
  loan repayment that splits into principal and interest, an IRAS GST payment
  that clears a liability, a refund that credits an expense rather than revenue.
- **Genuinely ambiguous** — `GRAB *TRIP` versus `GRABFOOD`; a PayNow to an
  individual who could be a contractor or a director; groceries that could be
  pantry supplies or personal shopping.
- **Tax nuance** — overseas SaaS needing reverse charge; medical, private car and
  club costs where input tax is blocked even with a valid tax invoice.
- **Data quality** — a duplicate-looking payment, an annual premium that is
  really a prepayment, a cheque deposit with no reference at all.

The `ACME SUPPLIES` case is the clearest illustration of why supporting documents
matter: the description is perfectly legible and still says nothing about what
was bought. Only the invoice reveals a laptop, which is a capitalised asset
rather than an office expense — a different account, on a different financial
statement.

---

## Layout

```
src/aiacct/
  config.py  models.py  reference.py    settings, enums, tax codes
  db/models.py                          SQLAlchemy models - the single definition
  db/repo.py  db/session.py             repositories, engine, connection check
  llm/           client.py              OpenAI via the official SDK
                 stub.py                deterministic offline provider
  ingestion/     router.py              magic bytes, text-layer density
                 readers.py             CSV, XLSX, PDF text, DOCX
  extraction/    classify.py            call 1
                 extract.py             call 2
                 field_policy.py        redundancy classes and the decision matrix
                 validators.py          the five checks
  matching/      documents.py           invoice to transaction scoring
  categorisation/ memory.py             rules and past corrections
                  categorise.py         call 3
  confidence/    scorer.py              the blend, the gates, the bands
  graph/         pipeline.py            LangGraph assembly
  testdata/      parser.py render.py    markdown -> objects -> files
  review.py                             approve, correct, split, learn
  export.py                             review pack, journals, client queries
  api/                                  FastAPI
alembic/                                migrations
scripts/         seed_db.py, generate_test_files.py,
                 demo_learning_loop.py, evaluate.py
```

`HANDOFF_FRONTEND.md` is the contract for the UI, which is built separately.

---

## Known limitations

Stated rather than hidden:

- **Inter-account transfers are not detected deterministically.** The client's own
  account numbers are in the prompt, but nothing guarantees the model applies
  them. A five-line check before the categorisation call would close this.
- **One payment settling several invoices is not matched.** That is a subset-sum
  problem; such payments stay unmatched and surface as a query rather than being
  matched wrongly.
- **Accrual dates are surfaced, not automated.** An invoice dated 28 December and
  paid 15 January is a December expense. The invoice date is captured and shown,
  but periods are not adjusted — that is real accrual accounting.
- **The date-order fallback assumes day-first** when a file's dates are all
  ambiguous. Correct for Singapore, wrong for a US-format export — it is logged
  rather than silently applied, and the period check catches the worst cases.
- **The offline provider's keyword table is deliberately weak**, and wrong on
  several cases on purpose, so an offline run still exercises the review and
  correction paths instead of producing a suspiciously clean result. The
  accuracy figures above are a floor, not a measure of the model.
