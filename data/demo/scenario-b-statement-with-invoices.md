# Lumina Design Studio Pte Ltd — May 2026

- Client: 202512345A
- Bank: Straits Union Bank Ltd
- Account: 003-88291-1
- Period: 2026-05-01 to 2026-05-31
- Opening balance: 18500.00
- Render as: pdf
- Print balances: yes

## Transactions

| Day | Description                            | Reference   | Out      | In      | Account | Tax | Why this is hard                                                                                                                                                                                                                                       |
|-----|----------------------------------------|-------------|----------|---------|---------|-----|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 2   | GIRO PAYMENT TELCOVA BROADBAND 2891004 | GIRO2891004 | 130.75   |         | 489     | TX  | Recurring and unambiguous.                                                                                                                                                                                                                             |
| 4   | PAYMENT RECEIVED - HARBOUR & CO        | FAST92140   |          | 9600.00 | 200     | SR  | Money in. Revenue, not a transfer.                                                                                                                                                                                                                     |
| 6   | GIRO RENT MAY2026 TANJONG PAGAR        | GIRO4410    | 4800.00  |         | 469     | TX  | Recurring, no invoice.                                                                                                                                                                                                                                 |
| 8   | PAYNOW-ACME SUPPLIES PTE LTD-94420     | PN94420     | 1962.00  |         | 720     | TX  | The headline case. The description is perfectly legible and still says nothing about what was bought. Only the invoice reveals a laptop, which is above the capitalisation threshold and belongs on the balance sheet rather than the profit and loss. |
| 12  | CPF BOARD CPF SUBMISSION 202604        | CPF202604   | 3240.00  |         | 825     | OP  | Clears a liability, not an expense.                                                                                                                                                                                                                    |
| 12  | SALARY PAYMENT MAY2026                 | GIRO7781    | 18600.00 |         | 477     | OP  | No GST on payroll.                                                                                                                                                                                                                                     |
| 15  | NETS TECHPOINT ELECTRONICS #02-11      |             | 268.00   |         | 453     | TX  | The same retailer as the laptop, but the receipt shows keyboards and cables. Below the capitalisation threshold, so this one is an expense.                                                                                                            |
| 18  | PAYNOW-LIM CONSTRUCTION PTE LTD        | PN55980     | 3200.00  |         | 473     | TX  | Repairs or a capitalised improvement? The invoice says repainting and partition repair, which is a repair.                                                                                                                                             |
| 20  | GRAB *TRIP 8845 SG                     |             | 18.40    |         | 493     | TX  | Travel.                                                                                                                                                                                                                                                |
| 22  | RAFFLES MEDICAL CLINIC                 |             | 184.00   |         | 483     | BL  | Medical expense: GST is blocked and cannot be reclaimed even with a valid tax invoice. Forces review.                                                                                                                                                  |
| 26  | SERVICE CHARGE                         |             | 12.00    |         | 404     | EP  | Exempt, not standard-rated.                                                                                                                                                                                                                            |
| 28  | PAYNOW-TAN WEI MING                    | PN46330     | 2900.00  |         | 310     | TX  | A payment to an individual with no supporting document. Subcontractor cost or a director's withdrawal? Only the client can say, so this one is queried even though the statement itself is perfectly clear.                                            |

## Supporting documents

### INV-2519 — Acme Supplies Pte Ltd
- Kind: invoice
- Render as: pdf
- Date: 2026-05-06
- Total: 1962.00
- Tax: 162.00
- Summary: Dell Latitude 5450 laptop, 1 unit, and a docking station
- Settles: PAYNOW-ACME SUPPLIES PTE LTD-94420
- Items:
  - Dell Latitude 5450 i7/16GB/512GB x1
  - Dell WD19S docking station x1

### R-882301 — TechPoint Electronics Pte Ltd
- Kind: receipt
- Render as: jpg
- Date: 2026-05-15
- Total: 268.00
- Tax: 22.13
- Summary: Keyboards, mice and USB-C cables
- Settles: NETS TECHPOINT ELECTRONICS #02-11
- Items:
  - Mechanical keyboard x2
  - Wireless mouse x2
  - USB-C cable 2m x3

### LC-5120 — Lim Construction Pte Ltd
- Kind: invoice
- Render as: pdf
- Date: 2026-05-15
- Total: 3200.00
- Tax: 264.22
- Summary: Repaint studio walls and repair three office partitions
- Settles: PAYNOW-LIM CONSTRUCTION PTE LTD
- Items:
  - Repainting - studio and corridor
  - Partition repair x3
