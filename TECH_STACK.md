# Tech stack, explained simply

For each piece: **what it is**, **what it does here**, **why I chose it**. Section B covers what is planned next.
Everything in section A is already in the code.

## A. Used right now

### The language: Python 3.10 or newer
The everyday language of AI/ML and data work. Every library below has a Python version, so the whole project is one language.

### 1. Reading the document
| Tool | What it is | What it does here | Why this one |
|---|---|---|---|
| **PyMuPDF** | A fast PDF library | Pulls out every word *with its position* on the page, and draws PDF pages as pictures for the preview | Positions matter: "the number to the right of *Invoice No.*" needs coordinates. Very fast (about 8 ms a document). |
| **Tesseract OCR** (via **pytesseract**) | Free open-source "reading a picture" engine by Google | Turns a photo or scan into words with positions, same format as PyMuPDF | Free, runs on your own machine, so invoices never leave the building. Needed for scans; on normal PDFs it also reads text inside pictures (a logo, or the supplier's details pasted as an image), keeping only confident words. Without it, normal PDFs still work. |
| **Pillow** | Python image library | Loads photos, rotates/cleans them before OCR; also used to make the simulated scans in the benchmark | Standard. |

### 2. Understanding the document (extraction)
| Tool | What it does | Why |
|---|---|---|
| **Rules extractor** (`extractor_rules.py`, `normalize.py`) | Looks for labels ("Invoice No", "Total", "BTW", "CGST") and reads the value next to/below them; understands dates and amounts in many formats (`1.234,56` vs `1,234.56`, Dutch/French/German month names) | Fast, free, offline, fully explainable. Always runs first. |
| **Smart extraction** (`smart_extract.py`) | Decides whether the AI is worth asking, caches answers, escalates from a cheap to a stronger model, merges the two readings | This is the cost control: AI is used only where it adds something. |
| **AI providers** (`llm.py`) via **httpx** | One small layer that talks to Gemini, Groq, OpenRouter, local Ollama (all through the "OpenAI-compatible" format) and Claude (via the **anthropic** SDK) | Swap providers by editing `.env`; no code change, no lock-in. Free tiers first. |
| **Grounding check** (`extractor_llm.py`) | Throws away any AI value that does not literally appear in the document | Stops the AI from inventing numbers. This is what makes AI safe for money. |
| **Vendor matching** (`checks.match_approved_vendor`) | Maps "OYO" / "Oravel Stays Pvt. Ltd." / a misread to the one approved vendor name, by name, tax ID or name printed on the page | Keeps duplicate detection reliable, and fixes many vendor misreads without any AI request. |

### Which AI models, and why these
| Model | Provider | Cost | Role |
|---|---|---|---|
| Gemini 3.5 Flash-Lite | Google | Free tier (paid: $0.30 / $2.50 per 1M tokens) | First choice: fast, cheap, good at reading forms |
| Gemini 3.8 Flash | Google | Free tier (paid: $0.75 / $3.75) | Escalation when Flash-Lite's answer is incomplete |
| gpt-oss-120b | Groq | Free tier | Backup when Gemini is rate-limited |
| any `:free` model / qwen3 via Ollama | OpenRouter / your laptop | Free | Extra free capacity / fully private option |
| Claude Haiku 4.5 | Anthropic | $1 / $5 | Paid option with a business contract |

Details, limits and privacy notes: [API_SETUP.md](API_SETUP.md).

### 3. The checks (plain Python, no AI)
| Tool | Used for |
|---|---|
| **RapidFuzz** | "Fuzzy" text matching: `Coolblue B.V.` vs `Coolblue BV`, invoice `99354890` vs `993548901`. Powers duplicate and vendor matching. |
| **Checksum code** (`taxid.py`) | India's GSTIN has a built-in check character; a mistyped one is detectable mathematically. Also UAE TRN and EU VAT formats. |
| **IBAN checksum** (`bank.py`, ISO 13616 "mod 97") | Every IBAN carries two check digits, so a mistyped account number is caught by maths. Also reads Indian account number + IFSC. Repairs typical scan misreads (`NLSO...` -> `NL50...`) only when the checksum then proves it. |
| **Bank-account memory** (`checks.check_bank`) | Compares the account printed on the invoice with the one on file (or on the vendor's earlier approved invoices). A changed account is the classic invoice-fraud trick, so it is a hard stop. |
| **Plain arithmetic** | subtotal + tax = total, line items add up, PO balance. |

### 4. The decision and the record
| Tool | What it is | Why |
|---|---|---|
| **Decision policy** (`decision.py`) | A short, readable set of rules: recommend reject / recommend approve / needs review | A manager can read and change it. No black box decides money: the checks only recommend; accounts payable reviews every invoice and a manager gives the final approval. |
| **SQLite** | A database that lives in one file | Zero setup for a prototype. Stores invoices, vendors, purchase orders and the audit log. The store is isolated in one file (`store.py`), so moving to SQL Server/PostgreSQL later is a contained change. |

### 4b. Sign-in and security (`auth.py`)
| Piece | What it does | Why |
|---|---|---|
| **scrypt** password hashing (Python's `hashlib`) | Stores a salted, deliberately slow fingerprint of each password, never the password | A stolen database does not reveal passwords |
| **Session cookie** (HttpOnly, SameSite=Strict) | Keeps you signed in; JavaScript cannot read it, other sites cannot send it | Standard protection against session theft and cross-site attacks |
| **Roles + approval limits** | Accounts payable / procurement / manager; a manager can have a maximum amount they may approve | Mirrors how finance teams split the work; no role can record and approve the same invoice |
| **Lockout + throttling** | 5 wrong passwords lock an account for 15 minutes; too many attempts from one computer are slowed | Stops password guessing |
| **Security headers** (CSP, X-Frame-Options ...) | Tell the browser to refuse foreign scripts and framing | Defence against injection and click-jacking |
| **Service token** | Lets a Power Automate flow call the API without a person's password | Safe integration |

### 4c-2. Hand-offs and segregation of duties (`workflow.py`)
Plain code, no new library. Each waiting invoice has a stage, derived from its accounts payable review and its check
results, and each stage belongs to a role: `ap_review` (accounts payable approves or rejects, adding a new supplier
with the review), `vendor_verify` (procurement), `vendor_approve` (manager), `po_missing` / `po_approval` (an order
placed outside the app), `receipt` (only with `INPRO_REQUIRE_RECEIPT=1`) and `approval` (manager). A supplier moves
pending -> verified -> approved (or rejected) and is payable only when approved. The server refuses an approval by
the person who uploaded or corrected the invoice, and a supplier verification or approval by whoever added or verified
it. Corrections keep the first reading and log old and new values; after any change the checks run again
(`Pipeline.recheck`), counting only earlier invoices as possible duplicates.

### 4c-2b. Notifications (`notices.py`, tables `notifications` and `notification_seen`)
Plain code, no new library. After every action the server compares each waiting invoice's stage before and after, and
tells the role whose turn it now is; decisions are also told to the people they affect (uploader, reviewer, whoever
added a supplier). A note is addressed to a role or to one person, is never shown to the person who caused it, and
each user has a "seen up to" pointer for the unread count. The screen asks for them every 30 seconds and after every
action, so no WebSocket server is needed.

### 4c-3. Purchase requests (`workflow.py`, table `purchase_requests`)
Plain code, no new library. A request moves requested -> prepared (procurement: supplier and price) -> approved or
rejected (manager). Approval creates the purchase order with the next free number; the open invoices are then checked
again, so an invoice already waiting for that order moves on by itself. No role can create a purchase order directly
(the `po_import` permission belongs only to the integration token, for orders already approved in an ERP). An invoice
that quotes an order placed outside the app gets an *after-the-fact* request (kind `after_the_fact`, linked to the
invoice, keeping the supplier's PO number) recorded by procurement with a reason; the invoice waits at the
`po_approval` step until a manager approves it, and rejecting it rejects the invoice.

### 4d. Learning from approvals
When a person approves an invoice, the copilot remembers that vendor's bank account (only if
the checks did not object). The next invoice is compared with what was approved, so a later change stands out.
No AI and no new library: one column in SQLite, and every "remembered" step appears in the audit log.

### 4e. The Overview page (`insights.py`)
Computed from the invoices and the audit trail: value of invoices stopped before payment, where every invoice went
(with accounts payable / procurement / the manager, approved, rejected), what each check caught, checking time saved, AI cost per invoice.
The only assumptions are two settings (7 minutes to check an invoice by hand, 2 minutes to review one the copilot
already checked), and the page prints them.

### 5. The service and the screen
| Tool | What it is | Why |
|---|---|---|
| **FastAPI** | Python web framework | Automatic API docs at `/docs`, and it checks incoming data for you. |
| **Uvicorn** | The server that runs FastAPI | Standard pair with FastAPI. |
| **Pydantic** | Data validation | Rejects bad input (empty note, negative PO amount) before it reaches the logic. |
| **Plain HTML + CSS + JavaScript** (one file, `ui/index.html`) | Sign-in page, side menu with the notifications bell, overview, upload, invoice queue, review screen with pros and cons, a "done" page after each action, vendors, purchase orders, activity log, AI usage, users | No build step, nothing to install; works on phone and desktop, light and dark. Text is escaped before display so a hostile invoice cannot inject code into the page. |
| **IBM Plex Sans** (Google Fonts) | The typeface | Calm, professional, with even-width figures for amounts |

### 5b. Where the problem is (`evidence.py`)
| Piece | What it does | Why |
|---|---|---|
| **Word positions from the reader** | Finds where the problem value is printed (PDF text, or OCR boxes for scans and photos), ignoring spacing differences such as `FR76 1010...` vs `FR761010...` | The reviewer sees the exact spot instead of hunting for it |
| **Page close-ups** | The page picture, zoomed on that spot with a box around it; for a duplicate, the earlier invoice's close-up next to this one | Proof in one glance, also on a phone |
| **What it is compared with** | The account or tax ID on file, subtotal + tax, the earlier invoice, what is left on the PO; the changed characters highlighted | Explains the decision, not just the verdict |

### 5c. Hosting and automatic updates
| Tool | What it does | Why |
|---|---|---|
| **Docker** (`Dockerfile`) | Packs the app, Python and Tesseract into one container | Runs the same everywhere |
| **Render** (`render.yaml`) | Free hosting for the container, with an https link | Free without a card; builds straight from GitHub; opens from any country |
| **GitHub Actions** (`.github/workflows/tests.yml`) | On every push: install the app and run all tests | Render publishes only after this check passes, so a broken version never goes live |
| **Keep-awake job** (`.github/workflows/keep-awake.yml`) | Opens the health page every 5 minutes | Free services sleep after 15 idle minutes; this keeps the demo instant |
| **huggingface_hub** (`deploy/hf_deploy.py`) | Optional: publish to a Hugging Face Space instead | Docker Spaces need Hugging Face PRO since July 2026 |

### 6. Quality
| Tool | Used for |
|---|---|
| **pytest** (173 tests) | Automatic tests for parsing, checks, tax IDs, IBANs, where-the-problem-is proof, public-demo protections, the review flow (accounts payable, then new suppliers through procurement and a manager, then the manager's approval), notifications, hand-offs and segregation of duties, learning on approval, sign-in and roles, the AI-grounding guard, AI providers (faked, no network), caching, budget caps, merging, the API, and the missing-Tesseract case |
| **Benchmark scripts** (`eval/`) | Measures accuracy on 12 real invoices and 130 altered/scanned copies, in rules, hybrid or AI mode, with AI requests/tokens/cost |
| **Setup check** (`check_setup.py`) | Checks packages, Tesseract and sends one tiny test request to every configured AI model |

## B. Planned next (in priority order)

Each is small enough to finish in a day or two. I'd do them in this order because each adds something Hanif can *see*.

1. **Measure the AI reader on live models.** (Code done.) Add a free Gemini key and run `python eval/run_benchmark.py --mode hybrid`. Report the result next to the rules reader: this is the honest answer to "does the AI help, and what does it cost?".
2. **Learn from approver corrections.** Bank accounts are already learned on approval. Next: when an approver fixes a field, store it so the next invoice from that vendor uses the correction. Stack: a new SQLite table, no new library.
3. **Power Automate / SharePoint hand-off.** The `POST /api/invoices` endpoint already returns the verdict as JSON. Add an API key header, a webhook back to the workflow, and a small Power Automate flow. **Needs**: a Microsoft 365 developer tenant (free) to test. I can't verify how InPro itself is wired inside, so this is the first thing to ask Hanif.
4. **Sign in with Microsoft (Entra ID).** Local sign-in, roles and approval limits are done; single sign-on is the production step.
5. **Move to PostgreSQL / SQL Server + Docker.** Packaging the app as a container so it deploys anywhere (Azure App Service is the natural home for a Microsoft-based product).
6. A **table reader** for line items. (Reading text inside logos and pasted pictures is done: it fixed the last miss in the benchmark.)

## C. How this fits a Microsoft/SharePoint product

* The Copilot is a separate small service. InPro does not have to change to try it: the approval workflow sends the file, gets JSON back, and uses it to route the task.
* Nothing needs a GPU. Reading and checking run on an ordinary server. Only the optional AI reader needs internet (or none at all with a local Ollama model).
* Data stays under the customer's control by default: rules reader + local OCR means **no invoice leaves the server** unless a cloud AI key is added; with a local Ollama model, not even then.

## D. Why these choices, in one line
Plain, explainable code for anything that touches money; AI only where documents are messy and only with a guard; everything free to run and easy to show.
