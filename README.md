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
* Is the bank account the one we always pay? Did this e-mail really come from the supplier?

The last two are how most invoice fraud works: a fraudster sends a real-looking invoice from a look-alike e-mail
address ("azure-interiors.com" instead of "azure-interior.com") and asks for payment to a "new" bank account.

That is slow, boring, and easy to get wrong when the pile is big. InPro already routes the invoice to the right
approver. **Copilot does the checking first**, so the approver opens an invoice that already says
"safe, approve" or "look at this, and here's why".

## 2. What happens behind the scenes (5 steps)

```
upload / e-mail / watched folder -> 1 READ -> 2 EXTRACT -> 3 CHECK (8 checks) -> 4 DECIDE -> 5 SAVE + AUDIT LOG
                                                                                                 |
                                         approved by a person -> remember the vendor's bank account and e-mail domain
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
3. **CHECK.** Eight small, plain-code checks. No AI here, so every result is repeatable and explainable:

   | Check | What it asks |
   |---|---|
   | Required fields | Are the invoice number and total there? |
   | Arithmetic | subtotal + tax = total? Do line items add up? |
   | Duplicates | Same file, same number, same number but new amount, near-identical number, same amount and date? |
   | Vendor | On the approved list? Does the tax ID match the one on file? |
   | Tax ID | Is the GSTIN (India) valid by its real checksum? UAE TRN / EU VAT format |
   | Purchase order | Is the bill larger than what's left on the PO? Right vendor and currency? |
   | Bank account | Is the IBAN valid (its two check digits)? Is it the account on file, or on this vendor's earlier approved invoices? Is it another vendor's account? |
   | Sender | For e-mailed invoices: the supplier's real domain, a look-alike (`coo1blue.nl`, `azure-interiors.com`, Cyrillic letters), a free Gmail-type address, or a Reply-To that points elsewhere? |

4. **DECIDE.** A written policy, not a guess:
   *reject* if any hard check fails; *auto-approve* only if the core checks pass and the amount is under the limit
   (default 50,000); otherwise *needs review*. Every outcome comes with one sentence of reasons.
   On scans and photos, numbers that don't add up go to *needs review* ("compare with the image") instead of
   *reject*, because the photo reader can misread a digit; on digital PDFs they are a hard failure.
5. **SAVE + AUDIT.** Everything is stored with a step-by-step trace ("how the recommendation was reached") and an
   audit log. A human can always override, and overrides are logged.

**Why not "just ask an LLM"?** Approvals need repeatable, auditable answers. The AI is used for reading messy
documents; the decisions come from transparent checks.

## 3. The screens

* **Overview** (the first page after sign-in): the value of invoices stopped before payment, where every invoice
  went (cleared automatically / waiting / decided by a person / stopped), what each check caught and why, checking
  time saved, and AI cost per invoice.
* **Invoices** and the **review screen**: same layout idea as InPro, approval history on the left, the original
  document on the right, comment box and Approve / Reject at the bottom. Plus: AI verdict, per-check results,
  extracted fields, where the invoice came from (e.g. "E-mail from billing@azure-interiors.com, look-alike domain"),
  and the trace.
* **Inbox**: the watched folder, the mailbox connection, and everything received, with the sender verdict for each e-mail.
* **Fraud lab**: try to fool the checks yourself. Pick one of the real invoices and a trick (pay to a different
  account, mistype the IBAN, raise the total, resubmit with a new number, change the tax ID, e-mail it from a
  look-alike domain, or the full e-mail scam). The lab forges the PDF the way a fraudster would and sends it through
  the same pipeline as every other invoice. It shows the genuine and forged pages side by side with the change marked
  and a close-up, which checks fired, how long it took, and whether the trick's own check would have stopped it even
  with no earlier invoices on file. Lab forgeries are kept out of the Overview figures.
* **Vendors** (with tax ID, bank account and e-mail domains on file), **Purchase orders**, **Activity** (audit trail),
  **AI usage**, **Users**.

Click **Load demo data** on the Invoices page to fill it with 12 examples built from real invoices, then
**Send demo e-mails** on the Inbox page for the e-mail fraud story: the supplier's real invoice, the same invoice
re-sent from a look-alike domain with a new bank account (stopped: duplicate, bank account changed, fake sender),
and an invoice from a Gmail address (sent to a person).

## 3b. Invoices that arrive by themselves

In real accounts payable nobody uploads invoices one by one, so the copilot also takes them in by itself:

* **Watched folder**: drop a PDF, a photo or a saved e-mail (.eml) into the `inbox` folder (or point a scanner at
  it). Within about 20 seconds it is read, checked, filed, and moved to `inbox/processed`.
* **A real mailbox** (optional): give an IMAP address and an app password in `.env` (Gmail and Zoho work this way;
  see [API_SETUP.md](API_SETUP.md#8-connect-a-real-mailbox-optional-free)). Unread mail is fetched and every PDF/image
  attachment becomes an invoice.
* **The e-mail is evidence**: the sender, Reply-To and subject travel with the invoice and feed the sender check.
* **Nothing twice**: every message and attachment gets a fingerprint, so re-scans and restarts are safe.
* **It learns from approvals**: when a person approves an invoice, the vendor's bank account and e-mail domain are
  remembered (only if the checks didn't object), so a later change stands out. Each "remembered" step is in the audit log.

## 4. Sign-in and security

An approval is only worth something if you know **who** approved, so the app has proper sign-in:

* **No public sign-up.** An admin creates accounts on the Users page (a finance tool must not let strangers in).
* **Roles:** *viewer* (look only, e.g. auditors), *approver* (upload, approve up to a personal **approval limit**,
  reject anything), *admin* (everything, plus users, vendors, purchase orders).
* **Passwords** are stored only as salted scrypt hashes; 5 wrong attempts lock the account for 15 minutes.
* **Sessions** use an HttpOnly, SameSite=Strict cookie and end after 8 hours without activity.
* **Protections:** a required header against cross-site request forgery, strict security headers
  (Content-Security-Policy, no framing), every file and API call behind sign-in.
* **Audit trail:** sign-ins, failed attempts, user changes and every decision appear on the Activity page.
* **Integrations** (Power Automate / SharePoint) use a service token instead of a password.
* In demo mode the sign-in page lists three demo accounts: Priya (admin), Rahul (approver, limit 500), Meera (viewer).
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
* **Free means small**: 0.1 CPU and 512 MB (the app uses about 180 MB), so pages and the Fraud lab answer in about
  half a second instead of a twentieth.
* **Alternative:** a Hugging Face Space (`deploy/hf_deploy.py`, `deploy_hf.bat`, or the manual
  "Deploy to Hugging Face" workflow). Since July 2026, Docker Spaces need a Hugging Face PRO plan.

On the public demo: the three demo accounts are one click on the sign-in page and cannot be changed or locked by
visitors, uploads and forgeries are rate-limited, the AI budget caps still apply, the server's folders are never
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

Open <http://localhost:8000>, sign in as Priya (the sign-in page fills it in for you), click **Load demo data** on
the Invoices page and **Send demo e-mails** on the Inbox page, then open the Overview. Run the tests with `python -m pytest`.
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
bank-account cases were added.

| Test | Rules only (free, offline) | Rules + AI when needed |
|---|---|---|
| Reading digital PDFs (74 fields) | 71 / 74 = 95.9% | **72 / 74 = 97.3%** (AI asked on 4 of 12) |
| Reading scanned photos (OCR) | 62 / 74 = 83.8% | **68 / 74 = 91.9%** |
| Reading image-only PDFs | 62 / 74 = 83.8% | **68 / 74 = 91.9%** |
| Catching the altered invoices (the *specific* expected check must fire) | **105 / 106 = 99.1%** | 93 / 94 = 98.9% (measured before the 12 bank cases were added) |
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

* The reading rules were tuned while looking at these same 12 invoices, so their 95.9% is optimistic on invoices from
  new vendors. On unseen layouts the AI's share of the work will be larger than here.
* 12 invoices is a small sample: one field is 1.4 percentage points.
* Altered documents are controlled edits of real invoices, not real fraud. "Exact duplicate" is simply a file-hash match.
* The one remaining miss: a vendor name that exists only inside a logo image.
* Purchase orders, the approved-vendor list and the bank accounts on file are sample data.
* The e-mail fraud cases are e-mails the app builds itself, and the mailbox reader is tested against a fake IMAP
  server in the tests; it has not yet been run against a real company mailbox.
* The "checking time saved" figure rests on two assumptions (7 minutes by hand, 2 with the copilot); the Overview
  prints them and they are settings.

## 8. How it would plug into InPro

InPro already scans invoices and runs approval workflows on SharePoint. The Copilot is one HTTP call:

```
POST /api/invoices   (the invoice file)  ->  JSON: fields, per-check results, recommendation, reasons
```

A Power Automate flow or SharePoint workflow step could call it when a document arrives, then route:
auto-approve -> close; needs review -> send to the approver with the reasons attached; reject -> return to sender.
Not tested against InPro internals. That would be the first thing to work out together.

## 9. Code map

```
src/inpro_copilot/  reader.py (READ) | extractor_rules.py, smart_extract.py, extractor_llm.py, llm.py (EXTRACT)
                    checks.py (CHECK) | bank.py (IBAN / account numbers) | sender.py (look-alike domains) | taxid.py
                    decision.py (DECIDE) | pipeline.py (incl. learning on approval) | store.py (SQLite) | api.py (FastAPI)
                    intake.py (e-mail, folder, mailbox) | insights.py (Overview numbers) | lab.py (Fraud lab) | auth.py | demo.py
ui/index.html       the screens
eval/               benchmark          tests/   154 tests          data/real   the 12 invoices
Dockerfile, render.yaml, .github/workflows/   hosting on Render, tests on every push, keep-awake ping
```
