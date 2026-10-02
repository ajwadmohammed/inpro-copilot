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
| **Tesseract OCR** (via **pytesseract**) | Free open-source "reading a picture" engine by Google | Turns a photo or scan into words with positions, same format as PyMuPDF | Free, runs on your own machine, so invoices never leave the building. Only needed for scans. |
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

### 3. The eight checks (plain Python, no AI)
| Tool | Used for |
|---|---|
| **RapidFuzz** | "Fuzzy" text matching: `Coolblue B.V.` vs `Coolblue BV`, invoice `99354890` vs `993548901`. Powers duplicate and vendor matching, and spotting look-alike e-mail domains. |
| **Checksum code** (`taxid.py`) | India's GSTIN has a built-in check character; a mistyped one is detectable mathematically. Also UAE TRN and EU VAT formats. |
| **IBAN checksum** (`bank.py`, ISO 13616 "mod 97") | Every IBAN carries two check digits, so a mistyped account number is caught by maths. Also reads Indian account number + IFSC. Repairs typical scan misreads (`NLSO...` -> `NL50...`) only when the checksum then proves it. |
| **Bank-account memory** (`checks.check_bank`) | Compares the account printed on the invoice with the one on file (or on the vendor's earlier approved invoices). A changed account is the classic invoice-fraud trick, so it is a hard stop. |
| **Sender verification** (`sender.py`) | For invoices that arrived by e-mail: is the sender's domain the supplier's real one? Catches look-alikes (`azure-interiors.com` vs `azure-interior.com`, `coo1blue.nl`, Cyrillic letters that look Latin), free Gmail-type accounts, and a Reply-To that sends your answer somewhere else. |
| **Plain arithmetic** | subtotal + tax = total, line items add up, PO balance. |

### 4. The decision and the record
| Tool | What it is | Why |
|---|---|---|
| **Decision policy** (`decision.py`) | A short, readable set of rules: reject / auto-approve / needs review | A manager can read and change it. No black box decides money. |
| **SQLite** | A database that lives in one file | Zero setup for a prototype. Stores invoices, vendors, purchase orders and the audit log. The store is isolated in one file (`store.py`), so moving to SQL Server/PostgreSQL later is a contained change. |

### 4b. Sign-in and security (`auth.py`)
| Piece | What it does | Why |
|---|---|---|
| **scrypt** password hashing (Python's `hashlib`) | Stores a salted, deliberately slow fingerprint of each password, never the password | A stolen database does not reveal passwords |
| **Session cookie** (HttpOnly, SameSite=Strict) | Keeps you signed in; JavaScript cannot read it, other sites cannot send it | Standard protection against session theft and cross-site attacks |
| **Roles + approval limits** | Viewer / approver / admin; approvers have a maximum amount they may approve | Mirrors how finance teams delegate authority |
| **Lockout + throttling** | 5 wrong passwords lock an account for 15 minutes; too many attempts from one computer are slowed | Stops password guessing |
| **Security headers** (CSP, X-Frame-Options ...) | Tell the browser to refuse foreign scripts and framing | Defence against injection and click-jacking |
| **Service token** | Lets a Power Automate flow call the API without a person's password | Safe integration |

### 4c. Invoices that arrive by themselves (`intake.py`)
| Piece | What it does | Why |
|---|---|---|
| **Watched folder** | Drop a PDF, photo or saved e-mail (.eml) into `inbox/`; within ~20 seconds it is read, checked and moved to `inbox/processed` (or `inbox/failed`) | A scanner or shared drive can feed it with no clicks |
| **E-mail reading** (Python's built-in `email` library) | Takes the sender, Reply-To, subject and every PDF/image attachment from an e-mail | The e-mail itself is evidence: who sent the invoice matters as much as what it says |
| **Real mailbox** (Python's built-in `imaplib`, IMAP over SSL) | Optional: fetches unread mail from Gmail, Zoho or a company mail server with an app password (Microsoft 365 needs OAuth: see plan item 3) | How accounts payable really receives invoices (`invoices@company.com`) |
| **Background thread** | Checks the folder and mailbox every 20 seconds while the server runs | No extra service or scheduler to install |
| **Fingerprints** | Every message/attachment gets a SHA-256 fingerprint so nothing is processed twice | Safe to re-scan, restart, or receive the same mail twice |

### 4d. Learning from approvals
When a person approves an invoice, the copilot remembers that vendor's bank account and e-mail domain (only if
the checks did not object). The next invoice is compared with what was approved, so a later change stands out.
No AI and no new library: two columns in SQLite, and every "remembered" step appears in the audit log.

### 4e. The Overview page (`insights.py`)
Computed from the invoices and the audit trail: value of invoices stopped before payment, where every invoice went
(cleared / waiting / decided by a person / stopped), what each check caught, checking time saved, AI cost per invoice.
The only assumptions are two settings (7 minutes to check an invoice by hand, 2 minutes to review one the copilot
already checked), and the page prints them.

### 5. The service and the screen
| Tool | What it is | Why |
|---|---|---|
| **FastAPI** | Python web framework | Automatic API docs at `/docs`, and it checks incoming data for you. |
| **Uvicorn** | The server that runs FastAPI | Standard pair with FastAPI. |
| **Pydantic** | Data validation | Rejects bad input (empty note, negative PO amount) before it reaches the logic. |
| **Plain HTML + CSS + JavaScript** (one file, `ui/index.html`) | Sign-in page, side menu, overview, invoice queue, inbox, review screen, vendors, purchase orders, activity log, AI usage, users | No build step, nothing to install; works on phone and desktop, light and dark. Text is escaped before display so a hostile invoice cannot inject code into the page. |
| **IBM Plex Sans** (Google Fonts) | The typeface | Calm, professional, with even-width figures for amounts |

### 5b. The Fraud lab (`lab.py`)
| Piece | What it does | Why |
|---|---|---|
| **PyMuPDF redaction + text** | Whites out the original value on a real invoice and prints the fraudster's value in the same spot, same size | Makes realistic forgeries in milliseconds, from the real documents |
| **Same pipeline** | The forgery goes through exactly the same READ, EXTRACT, CHECK, DECIDE as any invoice | Nothing is special-cased: the result is whatever the checks decide |
| **"Without history" re-check** | Runs the checks again with no earlier invoices on file | Shows whether a trick is caught on its own merits, not only because the original was already there |

### 5c. Hosting and automatic updates
| Tool | What it does | Why |
|---|---|---|
| **Docker** (`Dockerfile`) | Packs the app, Python and Tesseract into one container | Runs the same everywhere |
| **Hugging Face Spaces** | Free hosting for the container, with an https link | Free, no card, 16 GB RAM, opens from any country except mainland China; a natural home for an AI project |
| **GitHub Actions** (`.github/workflows/deploy.yml`) | On every push: install, run all tests, and only if they pass, publish to the Space | Automatic updates, and a broken version never goes live |
| **huggingface_hub** (`deploy/hf_deploy.py`) | Creates the Space, stores the AI keys as encrypted secrets, uploads the app, waits until it answers | One script for both the automatic and the by-hand deploy |

### 6. Quality
| Tool | Used for |
|---|---|
| **pytest** (153 tests) | Automatic tests for parsing, checks, tax IDs, IBANs, look-alike domains, the Fraud lab, public-demo protections, e-mail/folder/mailbox intake (with a fake mailbox), learning on approval, sign-in and roles, the AI-grounding guard, AI providers (faked, no network), caching, budget caps, merging, the API, and the missing-Tesseract case |
| **Benchmark scripts** (`eval/`) | Measures accuracy on 12 real invoices and 130 altered/scanned copies, in rules, hybrid or AI mode, with AI requests/tokens/cost |
| **Setup check** (`check_setup.py`) | Checks packages, Tesseract and sends one tiny test request to every configured AI model |

## B. Planned next (in priority order)

Each is small enough to finish in a day or two. I'd do them in this order because each adds something Hanif can *see*.

1. **Measure the AI reader on live models.** (Code done.) Add a free Gemini key and run `python eval/run_benchmark.py --mode hybrid`. Report the result next to the rules reader: this is the honest answer to "does the AI help, and what does it cost?".
2. **Learn from approver corrections.** Bank accounts and e-mail domains are already learned on approval. Next: when an approver fixes a field, store it so the next invoice from that vendor uses the correction. Stack: a new SQLite table, no new library.
3. **Microsoft 365 mailbox via Microsoft Graph.** IMAP with an app password works today for Gmail and Zoho, but Microsoft is retiring password sign-in for IMAP; Graph (with Entra ID sign-in) is how a Microsoft 365 customer would connect `invoices@company.com`.
4. **Power Automate / SharePoint hand-off.** The `POST /api/invoices` endpoint already returns the verdict as JSON. Add an API key header, a webhook back to the workflow, and a small Power Automate flow. **Needs**: a Microsoft 365 developer tenant (free) to test. I can't verify how InPro itself is wired inside, so this is the first thing to ask Hanif.
5. **Sign in with Microsoft (Entra ID).** Local sign-in, roles and approval limits are done; single sign-on is the production step.
6. **Move to PostgreSQL / SQL Server + Docker.** Packaging the app as a container so it deploys anywhere (Azure App Service is the natural home for a Microsoft-based product).
7. **OCR for logos/embedded images** (fixes the one miss in the benchmark), and a **table reader** for line items.

## C. How this fits a Microsoft/SharePoint product

* The Copilot is a separate small service. InPro does not have to change to try it: the approval workflow sends the file, gets JSON back, and uses it to route the task.
* Nothing needs a GPU. Reading and checking run on an ordinary server. Only the optional AI reader needs internet (or none at all with a local Ollama model).
* Data stays under the customer's control by default: rules reader + local OCR means **no invoice leaves the server** unless a cloud AI key is added; with a local Ollama model, not even then.

## D. Why these choices, in one line
Plain, explainable code for anything that touches money; AI only where documents are messy and only with a guard; everything free to run and easy to show.
