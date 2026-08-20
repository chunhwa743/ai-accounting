<!--
  SYNTHETIC TEST DATA - NOT A REAL FINANCIAL RECORD.

  This file defines a fictional bank statement for a fictional company, and the
  files rendered from it are specimens for exercising software. No transaction
  described here occurred. The account holder, the issuing bank, every vendor
  that issues an invoice or receipt, every person, every account number and
  every registration number are invented.

  Some descriptions name real businesses, because that is what a real bank
  statement looks like and the categorisation logic has to cope with it. Those
  are nominative references only: no affiliation, sponsorship or endorsement is
  implied, nothing is asserted about the named business, and no document here is
  presented as issued by one. Every rendered file carries the same notice on its
  face - see SPECIMEN_NOTICE in src/aiacct/testdata/render.py.
-->

# Lumina Design Studio Pte Ltd — April 2026

- Client: 202512345A
- Bank: Straits Union Bank Ltd
- Account: 003-88291-1
- Period: 2026-04-01 to 2026-04-30
- Opening balance: 20000.00
- Render as: pdf
- Print balances: yes

## Transactions

| Day | Description                                | Reference   | Out      | In       | Account | Tax | Why this is hard                                                                                              |
|-----|--------------------------------------------|-------------|----------|----------|---------|-----|---------------------------------------------------------------------------------------------------------------|
| 2   | GIRO PAYMENT TELCOVA BROADBAND 2891004     | GIRO2891004 | 132.60   |          | 489     | TX  | Recurring, and now corroborated by the invoice.                                                               |
| 3   | PAYMENT RECEIVED - MERIDIAN RETAIL PTE LTD | FAST91887   |          | 11800.00 | 200     | SR  | Revenue, with the sales invoice attached. Still above materiality, so a person signs it off.                  |
| 5   | GIRO RENT APR2026 TANJONG PAGAR            | GIRO4410    | 4800.00  |          | 469     | TX  | The lease invoice removes the guesswork.                                                                      |
| 7   | GRAB *TRIP 7712 SG                         |             | 21.80    |          | 493     | TX  | The receipt confirms travel rather than a meal.                                                               |
| 11  | CPF BOARD CPF SUBMISSION 202603            | CPF202603   | 3240.00  |          | 825     | OP  | The contribution statement confirms it clears the accrued liability.                                          |
| 11  | SALARY PAYMENT APR2026                     | GIRO7781    | 18600.00 |          | 477     | OP  | The payroll summary agrees to the cent, and it is still held: payroll is far above materiality.                |
| 16  | SERVICE CHARGE                             |             | 12.00    |          | 404     | EP  | The bank advice confirms an exempt financial service.                                                         |
| 21  | NETS TECHPOINT ELECTRONICS #02-11          |             | 268.00   |          | 453     | TX  | The receipt shows toner and cables, below the capitalisation threshold, so this is an expense not an asset.    |
| 24  | PAYNOW-LOW MEI CHEN                        | PN68120     | 4200.00  |          | 310     | TX  | Without a document this reads as a director's drawing. The invoice shows contracted design work, so it is a subcontractor cost - the document changes the answer, and it posts without review. |
| 29  | TRF 5589021                                |             | 780.00   |          | 453     | TX  | Unreadable on its own. The supplier invoice resolves it, which is the whole point of this scenario.            |

## Supporting documents

### TC-2026-004411 — Telcova Communications Pte Ltd
- Kind: invoice
- Render as: pdf
- Date: 2026-04-01
- Total: 132.60
- Tax: 10.95
- Summary: Business fibre broadband and mobile, April 2026
- Settles: GIRO PAYMENT TELCOVA BROADBAND 2891004
- Items:
  - Business fibre 1Gbps monthly
  - Mobile plan x2

### LDS-INV-0442 — Lumina Design Studio Pte Ltd
- Kind: invoice
- Render as: pdf
- Date: 2026-03-28
- Total: 11800.00
- Tax: 974.31
- Summary: Brand identity programme, phase 2, billed to Meridian Retail
- Settles: PAYMENT RECEIVED - MERIDIAN RETAIL PTE LTD
- Items:
  - Brand identity phase 2 - design
  - Artwork production and handover

### TP-APR-2026 — Tanjong Pagar Estates Pte Ltd
- Kind: invoice
- Render as: pdf
- Date: 2026-04-01
- Total: 4800.00
- Tax: 396.33
- Summary: Office rent, April 2026, unit 04-18
- Settles: GIRO RENT APR2026 TANJONG PAGAR
- Items:
  - Monthly rent - unit 04-18

### GR-7712 — Grab
- Kind: receipt
- Render as: jpg
- Date: 2026-04-07
- Total: 21.80
- Tax: 1.80
- Summary: Ride from office to client meeting
- Settles: GRAB *TRIP 7712 SG
- Items:
  - Trip 7712 - Tanjong Pagar to Marina Bay

### CPF-202603 — CPF Board
- Kind: invoice
- Render as: pdf
- Date: 2026-04-10
- Total: 3240.00
- Tax: 0.00
- Summary: CPF contributions for March 2026, employer and employee shares
- Settles: CPF BOARD CPF SUBMISSION 202603
- Items:
  - Employer contribution - 6 employees
  - Employee contribution - 6 employees

### PAY-2026-04 — Lumina Design Studio Pte Ltd
- Kind: payroll
- Render as: docx
- Date: 2026-04-11
- Total: 18600.00
- Tax: 0.00
- Summary: Payroll summary, April 2026, net pay to 6 employees
- Settles: SALARY PAYMENT APR2026
- Items:
  - Net pay - 6 employees

### SUB-APR-0012 — Straits Union Bank Ltd
- Kind: invoice
- Render as: pdf
- Date: 2026-04-16
- Total: 12.00
- Tax: 0.00
- Summary: Monthly account service charge, an exempt financial service
- Settles: SERVICE CHARGE
- Items:
  - Account maintenance - April 2026

### R-990412 — TechPoint Electronics Pte Ltd
- Kind: receipt
- Render as: jpg
- Date: 2026-04-21
- Total: 268.00
- Tax: 22.13
- Summary: Printer toner and USB-C cables, all consumables
- Settles: NETS TECHPOINT ELECTRONICS #02-11
- Items:
  - Printer toner cartridge x2
  - USB-C cable 2m x4

### LMC-2026-03 — Low Mei Chen
- Kind: invoice
- Render as: pdf
- Date: 2026-04-20
- Total: 4200.00
- Tax: 346.79
- Summary: Contracted illustration work for the Meridian Retail programme
- Settles: PAYNOW-LOW MEI CHEN
- Items:
  - Illustration set - 12 pieces
  - Revisions and handover

### NS-4471 — Northwind Stationery Pte Ltd
- Kind: invoice
- Render as: pdf
- Date: 2026-04-27
- Total: 780.00
- Tax: 64.40
- Summary: Studio consumables - paper stock, mounting board and print supplies
- Settles: TRF 5589021
- Items:
  - Heavyweight paper stock x20
  - Mounting board x15
