# Demo scenarios

Two scenarios covering the two cases an accountant actually meets: a statement
that arrives on its own, and a statement that arrives with the invoices behind
it. Both are written for **Lumina Design Studio Pte Ltd** (UEN `202512345A`),
which is already seeded, so they run against a client that exists.

Generate the files:

```bash
python scripts/generate_test_files.py --source data/demo --out demo --flat
```

`demo/` is build output and is wiped on every run, which is why this guide lives
here in the source directory rather than beside the generated files.

---

## Scenario A — statement only

`scenario-a-statement-only.md` → April 2026, 10 transactions, one PDF, no
supporting documents.

This is the harder of the two, and the more common. Every answer has to come
from a bank description and nothing else, so it shows where the ceiling is:

| Transaction                          | What it demonstrates                                                                                                |
|--------------------------------------|---------------------------------------------------------------------------------------------------------------------|
| `GIRO PAYMENT TELCOVA BROADBAND`     | Clean and recurring. Should code confidently and post without review.                                               |
| `PAYMENT RECEIVED - MERIDIAN RETAIL` | Money in has to be recognised as revenue, not a transfer or a refund.                                               |
| `CPF BOARD CPF SUBMISSION`           | Clears a liability already accrued at payroll. Not an expense, and no GST.                                          |
| `SERVICE CHARGE`                     | Financial services are exempt, not standard-rated. The tax code matters as much as the account.                     |
| `NETS TECHPOINT ELECTRONICS`         | A technology retailer with no receipt. Consumables or capital equipment? Unknowable here — compare with scenario B. |
| `PAYNOW-LOW MEI CHEN`                | A payment to a director. Drawings reduce equity and are not deductible, so it is reviewed regardless of confidence. |
| `TRF 5589021`                        | No merchant name at all. Any confident answer would be invented, so it goes to the client as a query.               |

The last row is the point of the scenario. A system that answers this one
confidently is guessing, and the useful behaviour is to say so.

**Expect:** most lines coded, the director payment held for review on account
risk rather than low confidence, and `TRF 5589021` raised as a client query.

---

## Scenario B — statement with invoices

`scenario-b-statement-with-invoices.md` → May 2026, 12 transactions, one PDF,
three supporting documents (two PDF invoices, one JPG receipt).

Same client, one month later, with the documents attached. The headline case:

```
PAYNOW-ACME SUPPLIES PTE LTD-94420      1962.00
```

The description is perfectly legible and still says nothing about what was
bought. Only the invoice reveals a laptop, which is above the capitalisation
threshold and belongs on the balance sheet (**720**) rather than in the profit
and loss (**453**). No amount of reasoning over the bank line alone gets there.

Also here:

| Transaction                         | What the document changes                                                                                                                                                         |
|-------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `NETS TECHPOINT ELECTRONICS` 268.00 | Same retailer as the laptop, but the receipt shows keyboards and cables — below the threshold, so this one *is* an expense. The vendor does not decide the account; the goods do. |
| `PAYNOW-LIM CONSTRUCTION` 3200.00   | Repair or capitalised improvement? The invoice says repainting and partition repair, so it is a repair.                                                                           |
| `RAFFLES MEDICAL CLINIC` 184.00     | Medical expense. GST is blocked (`BL`) and cannot be reclaimed even with a valid tax invoice.                                                                                     |
| `PAYNOW-TAN WEI MING` 2900.00       | A payment to an individual with no document. Subcontractor or director's withdrawal? Only the client can say, so it is queried even though the statement is perfectly clear.      |

**Expect:** the Acme line coded to **720** with the invoice cited as the reason,
the TechPoint line to **453** despite the shared vendor, the medical line flagged
for blocked input tax, and the payment to an individual queried.

Running B after A on the same client also shows the learning loop: the merchant
patterns confirmed in April are applied deterministically in May, so those lines
need no model call at all.

---

## A note on the data

Every company, bank, person and registration number in these files is invented.
The client, the bank that issues the statement, and every vendor that issues an
invoice or receipt are all fictional, because these files render as documents —
a statement PDF carries the bank's name as its header, and an invoice PDF
carries the vendor's name and a GST registration number. Real names do not
belong on a fabricated financial record.

`IRAS` and `CPF Board` are the exceptions and appear as themselves. They are
statutory bodies rather than commercial counterparties, they appear on every
Singapore company's bank statement, they issue none of the generated documents,
and there is no fictional equivalent that would not make the data misleading.
