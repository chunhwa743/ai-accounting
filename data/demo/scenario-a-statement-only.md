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

| Day | Description                                | Reference   | Out      | In       | Account | Tax | Why this is hard                                                                                                                                                |
|-----|--------------------------------------------|-------------|----------|----------|---------|-----|-----------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 2   | GIRO PAYMENT TELCOVA BROADBAND 2891004     | GIRO2891004 | 132.60   |          | 489     | TX  | Recurring and unambiguous. Should code cleanly and post without review.                                                                                         |
| 3   | PAYMENT RECEIVED - MERIDIAN RETAIL PTE LTD | FAST91887   |          | 11800.00 | 200     | SR  | Money in. Must be revenue, not a transfer or a refund.                                                                                                          |
| 5   | GIRO RENT APR2026 TANJONG PAGAR            | GIRO4410    | 4800.00  |          | 469     | TX  | Recurring. No invoice arrives each month because there is a lease.                                                                                              |
| 7   | GRAB *TRIP 7712 SG                         |             | 21.80    |          | 493     | TX  | Travel, not staff meals. GRABFOOD would be a different account.                                                                                                 |
| 11  | CPF BOARD CPF SUBMISSION 202603            | CPF202603   | 3240.00  |          | 825     | OP  | Clears a liability already accrued from payroll. Not an expense, and carries no GST.                                                                            |
| 11  | SALARY PAYMENT APR2026                     | GIRO7781    | 18600.00 |          | 477     | OP  | Wages are not a supply, so there is no GST on payroll.                                                                                                          |
| 16  | SERVICE CHARGE                             |             | 12.00    |          | 404     | EP  | Financial services are exempt, not standard-rated.                                                                                                              |
| 21  | NETS TECHPOINT ELECTRONICS #02-11          |             | 268.00   |          | 453     | TX  | A technology retailer. Without a receipt there is no way to tell whether this was consumables or equipment that should be capitalised. Compare with scenario B. |
| 24  | PAYNOW-LOW MEI CHEN                        | PN68120     | 6000.00  |          | 980     | OP  | A payment to a director. Drawings reduce equity and are not deductible. High-risk account, so it is reviewed regardless of confidence.                          |
| 29  | TRF 5589021                                |             | 780.00   |          | none    |     | No merchant name at all. There is nothing to reason from, so any confident answer would be invented. Goes to the client as a query.                             |
