# InPro Copilot

**An AI "pre-reviewer" for invoice approvals.** A prototype showing how an AI agent could take
the repetitive checking off the person who approves invoices in a workflow product like InPro.

> Independent prototype, built from InPro's public product description. It is **not** part of InPro
> and **not** affiliated with Novigo. The InPro integration below is a design sketch, not something
> tested against InPro itself.

---

## 1. The problem, in simple words

In an approval workflow, an invoice arrives and a person has to open it and ask the same questions every time:

* Is this invoice already paid? (duplicates)
* Do the numbers add up?
* Is this a vendor we actually work with? Is the tax number right?
* Did we order this, and is the bill bigger than the order?
* Is the bank account the one we always pay?

The last one is how most invoice fraud works: a real-looking invoice that asks for payment to a "new" bank account.

That is slow, boring, and easy to get wrong when the pile is big. InPro already routes the invoice to the right
approver. **Copilot does the checking first**, so the approver opens an invoice that already says
"safe, approve" or "look at this, and here's why".

## 2. What happens behind the scenes (5 steps)

```
upload -> 1 READ -> 2 EXTRACT -> 3 CHECK (7 checks) -> 4 DECIDE -> 5 HAND-OFF to the right person + AUDIT LOG
                                                                                  |
                                         approved by a person -> remember the vendor's bank account
```

1. **READ.** Turn the file into words with positions. If the PDF has real text, we read it directly
   (PyMuPDF, instant). If it is a photo or scan, we run OCR (Tesseract), which "reads" the picture.
2. **EXTRACT.** Find the vendor, invoice number, date, currency, subtotal, tax, total and the bank account to pay.
   * First, free **layout rules** look at labels and positions ("the number next to *Invoice No.*").
   * Only if that result is incomplete, doesn't add up, comes from a scan, or names an unknown vendor, an
     **AI model** reads the text too (Gemini free tier by default; Groq, OpenRouter, a local Ollama model or
     Claude also work). Cheapest model first, a stronger one only if needed, and answers are cached.
   * Every AI value passes a **grounding check**: if it is not literally printed on the document, it is
     thrown away, so the AI can't invent numbers. The two readings are merged and every disagreement is shown.
   * The vendor is matched to the approved-vendor list, so one supplier always has one name.
3. **CHECK.** Seven small, plain-code checks. No AI here, so every result is repeatable and explainable:

   | Check | What it asks |
   |---|---|
   | Required fields | Are the invoice number and total there? |
   | Arithmetic | subtotal + tax = total (counting printed shipping, handling, rounding or discount lines)? Do line items add up? |
   | Duplicates | Same file, same number, same number but new amount, near-identical number, same amount and date? |
   | Vendor | On the approved list? Does the tax ID match the one on file? |
   | Tax ID | Is the GSTIN (India) valid by its real checksum? UAE TRN / EU VAT format |
   | Purchase order | Is the bill larger than what's left on the PO? Right vendor and currency? |
   | Bank account | Is the IBAN valid (its two check digits)? Is it the account on file, or on this vendor's earlier approved invoices? Is it another vendor's account? |

4. **RECOMMEND.** A written policy, not a guess:
   *recommend reject* if any hard check fails; *recommend approve* only if the core checks pass and the amount is
   under the limit (default 50,000); otherwise *needs your review*. Every recommendation comes with one sentence of
   reasons. **The checks never approve or reject on their own**: every invoice is reviewed by accounts payable and
   given its final approval by a manager (section 3a), and the recommendation is shown to both as pros and cons.
   On scans and photos, numbers that don't add up go to *needs review* ("compare with the image") instead of
   *reject*, because the photo reader can misread a digit; on digital PDFs they are a hard failure.
5. **SAVE + AUDIT.** Everything is stored with a step-by-step trace ("how the recommendation was reached") and an
   audit log. A human can always override, and overrides are logged.

**Why not "just ask an LLM"?** Approvals need repeatable, auditable answers. The AI is used for reading messy
documents; the decisions come from transparent checks.

## 3. The screens

* **Overview** (the first page after sign-in): the value of invoices stopped before payment, where every invoice
  went (with accounts payable / with procurement / with the manager / approved / rejected), what each check caught and why, checking
  time saved, and AI cost per invoice.
* **Upload**: drop an invoice (PDF or photo); it is read and checked straight away and opens for review.
* **Invoices** and the **review screen**: same layout idea as InPro, approval history on the left, the original
  document on the right, and the decision of whoever's turn it is at the bottom (accounts payable: Approve / Reject;
  procurement: verify the supplier; manager: approve the supplier or the order, then Approve / Reject). Plus: the AI
  verdict, the review as **pros and cons** (what needs attention, what could not be checked, what looks good),
  extracted fields, who uploaded it, the hand-off steps, and the trace.
* **After every action** a page says what happened, who was told, and what is next for you (the next invoice to
  review, upload another one). The **bell** next to the logo shows each person what reached their step and the
  outcome of decisions that affect them, with a link to it; it updates on its own every 30 seconds.
* **Where the problem is**: when an invoice fails a check, its review screen shows the proof. The problem value is
  marked on a close-up of the document and set next to what it is compared with: the bank account on file, the
  earlier invoice it repeats (both close-ups side by side), subtotal + tax for a wrong total, the tax ID on the vendor
  record, or what is left on the purchase order. The changed characters are highlighted.
* **Vendors** (with tax ID and bank account on file, and new ones waiting for procurement or a manager), **Purchase orders**, **Activity** (audit trail),
  **AI usage**, **Users**.

Click **Load demo data** on the Invoices page to fill it with 13 examples built from real invoices.

## 3a. Who does what: hand-offs and segregation of duties

The copilot checks every invoice the moment it arrives. Then people decide, in this order, and the review page shows
the whole path (received, checked, review, supplier, order, manager) with who did each step:

| Step | Who | What they do |
|---|---|---|
| Review the invoice | Accounts payable | Every invoice, first. The page shows what needs attention and what looks good, next to the document. **Approve** sends it on with an optional note for the manager. **Reject** needs a reason: the invoice is closed and never paid, its record is kept, and the upload page opens for the corrected invoice. Fields read wrongly can be corrected during the review (the first reading is kept and every change is logged); a number or date the document does not print is simply accepted with the approval. The supplier, the amount and the currency are needed to approve. |
| Add a new supplier | Accounts payable | When the supplier is not on the vendor list, the review shows a card pre-filled from the invoice (name, tax ID with its check digit, bank account with its IBAN checksum); approving the invoice adds it as *waiting for procurement*. Until a manager approves the supplier, its invoices wait. |
| Verify the supplier | Procurement | Confirms the supplier is real (by phone, on a known number) or rejects it with a reason. Never the person who added it. |
| Approve the supplier | Manager | The supplier becomes active and payable only now; then the manager decides on the invoice. Never the person who added or verified it. |
| Record the order | Procurement | Only when an invoice quotes a PO that is not on file because it was ordered outside the app (an *after-the-fact* order): the PO number, supplier and currency come pre-filled, the amount must come from the order itself (never from the invoice), and a reason is required. |
| Approve the order | Manager | The same approval every order needs. Approving opens the PO under the number the supplier was given and the invoice moves on; rejecting it rejects the invoice too, because an order nobody approved is not paid. |
| Confirm delivery | Procurement | Optional (`INPRO_REQUIRE_RECEIPT=1`): for invoices that cite a purchase order, the three-way match (order, invoice, delivery) before the manager decides. Off by default. |
| Approve or reject | Manager | The final approval for every invoice, with the accounts payable note and the pros and cons in front of them. May approve at any step, even with fields missing or checks failing: what is still open is listed above the **Approve anyway** button, and the decision records exactly what the manager accepted. Nobody approves an invoice they uploaded or corrected, and a manager can be given an approval limit on the Users page; the manager also manages users. |

Whoever's turn it is hears about it under the bell ("Waiting for your approval: invoice 30064443 from QualityHosting"),
and the people a decision affects hear the outcome (the uploader and the reviewer when the manager approves or
rejects; whoever added a supplier when it is verified, approved or rejected). Nobody is notified about their own action.

Three roles, and no role can take an invoice from upload to payment alone: accounts payable reviews invoices but never
gives the final approval, procurement verifies suppliers and records orders but never approves, managers approve but
never record invoices or add suppliers. On top of the roles, the server refuses an approval by anyone who uploaded or
corrected that invoice, and a supplier verification or approval by the person who added or verified it.
Each person's **My tasks** list shows only what is waiting for them, and an empty list says when that role gets work.
Once an invoice is decided, its page shows a short record (approved for payment or rejected, by whom, when, supplier,
amount, order and delivery, who to pay) with the full review folded underneath, and changing the decision is one
deliberate click away. An invoice uploaded by mistake (wrong file, a second copy, the supplier will re-issue) can be
**removed** by accounts payable or a manager, with a reason, until a person has decided it: it leaves the queue and is
never paid, but nothing is deleted, so the Activity log still shows who removed it and why.

**Before the invoice: purchase requests.** The industry rule is that whoever needs something asks, procurement turns
it into an order, and the order is approved before it is issued, so no single person controls a purchase. Here, on the
Purchase orders page, accounts payable can *request a purchase* (what is needed, supplier, estimated amount, reason).
Procurement prepares the order (confirms the supplier and the agreed price) or rejects it with a reason; a manager
gives the final approval, within their limit and never on a request they made or prepared. Only then does it become a
purchase order (numbered automatically), and an invoice that quotes it is matched against it. **No person can open a
purchase order directly**: there is one way in, and an order placed outside the app is recorded from its invoice and
approved the same way (see the table above). Orders already approved in the company's ERP can be loaded through the
API with the integration token. Every step is in the Activity log.

## 3b. How invoices get in

* **Upload**: accounts payable drops PDFs or photos on the Upload page or the Invoices page (several at once is fine).
* **From InPro**: a SharePoint or Power Automate step sends the scanned file to the API with the service token and
  gets the verdict back (see section 8).
* **It learns from approvals**: when a person approves an invoice, the vendor's bank account is remembered (only if
  the checks didn't object), so a later change stands out. Each "remembered" step is in the audit log.

## 4. Sign-in and security

An approval is only worth something if you know **who** approved, so the app has proper sign-in:

* **No public sign-up.** A manager creates accounts on the Users page (a finance tool must not let strangers in).
* **Roles** (see 3a): *accounts payable*, *procurement* and *manager* (approves, optionally up to a personal
  **approval limit**, and manages users). The rules are enforced by the server, not only
  hidden in the screen: uploader or corrector cannot approve, whoever added a supplier cannot verify or approve it.
* **Passwords** are stored only as salted scrypt hashes; 5 wrong attempts lock the account for 15 minutes.
* **Sessions** use an HttpOnly, SameSite=Strict cookie and end after 8 hours without activity.
* **Protections:** a required header against cross-site request forgery, strict security headers
  (Content-Security-Policy, no framing), every file and API call behind sign-in.
* **Audit trail:** sign-ins, failed attempts, user changes and every decision appear on the Activity page.
* **Integrations** (Power Automate / SharePoint) use a service token instead of a password.
* In demo mode the sign-in page has one button, **Open the demo**. You start as Ananya (accounts payable), where an
  invoice starts; click your name to switch to Vikram (procurement) or Rahul (manager). The switch works only between
  these demo accounts and is recorded in the Activity log.
  For production, "Sign in with Microsoft" (Entra ID) is the natural next step, since InPro customers already use Microsoft 365.

## 5. Put it online (free, opens anywhere)

The public demo runs on **Render's free plan**: no card, an `https://...onrender.com` link that opens from any
country, and the app in its own Docker container (`Dockerfile`, `render.yaml`).

* **Automatic updates.** Every push to `main` runs all tests on GitHub (`.github/workflows/tests.yml`). Render
  publishes the new version only after that check has passed (`autoDeployTrigger: checksPass`), so a broken version
  never goes live.
* **One-time setup.** In Render: New > Blueprint > pick this repository > paste `GEMINI_API_KEY` and `GROQ_API_KEY`
  when asked (stored encrypted by Render, never in the code) > Apply.
* **Always awake.** Free services sleep after 15 minutes without visitors and take about a minute to wake.
  `.github/workflows/keep-awake.yml` opens the health page every 5 minutes, which fits inside Render's 750 free hours
  a month for one service.
* **Free means small**: 0.1 CPU and 512 MB (the app uses about 180 MB), so pages answer in about half a second
  instead of a twentieth.
* **Alternative:** a Hugging Face Space (`deploy/hf_deploy.py`, `deploy_hf.bat`, or the manual
  "Deploy to Hugging Face" workflow). Since July 2026, Docker Spaces need a Hugging Face PRO plan.

On the public demo: the demo accounts are one click away and cannot be changed or locked by visitors, uploads are rate-limited, the AI budget caps still apply, the server's folders are never
shown, and the demo story is loaded automatically whenever the server starts (the free disk is not permanent).

## 6. Run it on your computer (Windows)

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env          (then paste a free Gemini key into .env, optional)
python check_setup.py           (checks packages, Tesseract and every AI key)
run.bat
```

Open <http://localhost:8000> and click **Open the demo**. To load the sample invoices, switch to Rahul (manager)
and click **Load demo data** on the Invoices page. Run the tests with `python -m pytest`.
Mac/Linux: `source .venv/bin/activate`, `cp .env.example .env`, `./run.sh`.

* **Scans and photos** need Tesseract: `winget install -e --id UB-Mannheim.TesseractOCR` on Windows
  (found automatically, even if it is not on PATH); `brew install tesseract` on Mac; `apt install tesseract-ocr` on Linux.
  Digital PDFs work without it.
* **AI keys**: see [API_SETUP.md](API_SETUP.md): which APIs, how to get free keys, limits, costs, privacy.
* **Every technology used**, and why: [TECH_STACK.md](TECH_STACK.md).

## 7. Measured results

Data: **12 real invoices** (Dutch, French, German, US, Indian, English) from public test sets
(invoice2data, sparrow). I wrote the answer key by hand. Then I made **130 test documents** from them by
altering real invoices one way at a time (changed a total, removed the number, swapped a tax ID, replaced the bank
account with another valid one, mistyped one IBAN digit ...) or by simulating scans (blur, tilt, noise, JPEG).
Reading accuracy with AI measured on Windows, 1 October 2026; defect detection re-measured 2 October 2026 after the
bank-account cases were added; rules-only scan reading re-measured 3 October 2026 (dates such as "August 3°, 2014" on a
noisy scan are now read, and a total misread once on a scan loses to the amount the page repeats); rules-only digital
reading and defect detection re-measured 3 October 2026 after text inside pictures (a logo or a supplier block pasted
as an image) started being read.

| Test | Rules only (free, offline) | Rules + AI when needed |
|---|---|---|
| Reading digital PDFs (74 fields) | 72 / 74 = 97.3% | **72 / 74 = 97.3%** (AI asked on 4 of 12) |
| Reading scanned photos (OCR) | 64 / 74 = 86.5% | **68 / 74 = 91.9%** |
| Reading image-only PDFs | 64 / 74 = 86.5% | **68 / 74 = 91.9%** |
| Catching the altered invoices (the *specific* expected check must fire) | **106 / 106 = 100%** | 93 / 94 = 98.9% (measured before the 12 bank cases were added) |
| ... of which bank account changed / IBAN mistyped | 7 / 7 and 5 / 5 | |
| Clean real invoices wrongly rejected | 0 of 12 | 0 of 12 |
| AI cost for the whole benchmark (~170 documents) | $0 | **$0.06** first run, $0 on re-runs (cache) |

AI setup used: Gemini 3.5 Flash-Lite (paid key) first, Gemini 3.8 Flash for escalation, Groq gpt-oss-120b (free)
as backup. On average about 750 input + 350 output tokens per AI request, roughly $0.001 per AI-read invoice.

What the AI fixed: seller names the rules misread (e.g. the customer instead of the seller), dates and totals on
scans. Two early problems showed up in this measurement and are fixed in the code:
the AI sometimes picked a due date or an order number instead of the invoice's own (so for numbers and dates the
labelled rules reading now wins, and AI dates must be printed on the page), and it named an unrecognisable seller
differently on two copies of the same invoice (so the duplicate check now also compares number + date + amount on their own).

Reproduce: `python eval/run_benchmark.py` (rules), `python eval/run_benchmark.py --mode hybrid` (rules + AI when
needed) or `--mode ai` (AI on every document). On Windows you can also double-click `eval/run_full_benchmark.bat`.
AI modes print requests, tokens, estimated cost, and every field where the AI changed the result.

**Honest caveats**

* The reading rules were tuned while looking at these same 12 invoices, so their 97.3% is optimistic on invoices from
  new vendors. On unseen layouts the AI's share of the work will be larger than here.
* 12 invoices is a small sample: one field is 1.4 percentage points.
* Altered documents are controlled edits of real invoices, not real fraud. "Exact duplicate" is simply a file-hash match.
* The one remaining miss: a vendor name that exists only inside a logo image.
* Purchase orders, the approved-vendor list and the bank accounts on file are sample data.
* The "checking time saved" figure rests on two assumptions (7 minutes by hand, 2 with the copilot); the Overview
  prints them and they are settings.

## 8. How it would plug into InPro

InPro already scans invoices and runs approval workflows on SharePoint. The Copilot is one HTTP call:

```
POST /api/invoices   (the invoice file)  ->  JSON: fields, per-check results, recommendation, reasons
```

A Power Automate flow or SharePoint workflow step could call it when a document arrives, then route:
every invoice then goes to the accounts payable review with the recommendation and its reasons attached, and a
recommended reject can be flagged to the supplier straight away.
Not tested against InPro internals. That would be the first thing to work out together.

## 9. Code map

```
src/inpro_copilot/  reader.py (READ) | extractor_rules.py, smart_extract.py, extractor_llm.py, llm.py (EXTRACT)
                    checks.py (CHECK) | bank.py (IBAN / account numbers) | taxid.py
                    decision.py (DECIDE) | pipeline.py (incl. learning on approval) | store.py (SQLite) | api.py (FastAPI)
                    workflow.py (hand-offs, segregation of duties)
                    insights.py (Overview numbers) | evidence.py (where the problem is) | auth.py (sign-in, roles) | demo.py
ui/index.html       the screens
eval/               benchmark          tests/   173 tests          data/real   the 12 invoices
Dockerfile, render.yaml, .github/workflows/   hosting on Render, tests on every push, keep-awake ping
```
