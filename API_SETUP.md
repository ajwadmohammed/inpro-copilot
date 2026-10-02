# APIs and keys: what you need, what it costs

**Short version:** you need **nothing** to run the app. With no key it reads invoices with its own
rules (free, offline, 96% field accuracy on the test invoices). An AI key makes it better at unusual
layouts and scans. The recommended setup costs **$0**: one free Google Gemini key, plus an optional free
Groq key as a backup.

## 1. What each piece is

| Thing | Needed? | What it does | Cost |
|---|---|---|---|
| Python packages (`requirements.txt`) | Yes | The app itself | Free |
| Tesseract OCR | Only for scans/photos | Turns a picture of an invoice into text, on your laptop | Free |
| **Gemini API key** | Recommended | AI reader, first choice | **Free tier** |
| Groq API key | Optional | Backup AI reader when Gemini is busy | **Free tier** |
| OpenRouter key / local Ollama | Optional | More free or fully private options | Free |
| Anthropic (Claude) key | Optional | Paid option, for when you need a guaranteed, contract-backed service | Paid |

## 2. Get the free keys (5 minutes)

1. **Gemini**: open https://aistudio.google.com/apikey, sign in with your Google account,
   click *Create API key*. No card needed.
2. **Groq** (optional backup): https://console.groq.com/keys, sign in, *Create API Key*.
3. In the project folder, open `.env` (copied from `.env.example`) and paste:
   ```
   GEMINI_API_KEY=your-key-here
   GROQ_API_KEY=your-other-key-here
   ```
   If billing is enabled on your Gemini key (paid tier), also add `INPRO_GEMINI_PAID=1`.
   Both Gemini key formats work: the older `AIza...` and the newer `AQ....` that AI Studio issues now.
4. Test them: `python check_setup.py`. Each model gets one tiny request (about 20 tokens) and you see
   OK or the exact problem.

Keep `.env` private. It is in `.gitignore`, so it is never uploaded to GitHub.

## 3. Free tier limits (October 2026; providers change these often)

| Provider | Free models used | Limits | Notes |
|---|---|---|---|
| Google Gemini | `gemini-3.5-flash-lite` (first), `gemini-3.8-flash` (escalation) | Per model, shown in AI Studio. Community reports for Flash models range from a few hundred to about 1,500 requests/day | Pro models are paid-only. **Free-tier data may be used by Google to improve its products** |
| Groq | `openai/gpt-oss-120b` | 30 requests/min, 1,000/day, 8,000 tokens/min | The token-per-minute limit is the tight one: about 3 invoices a minute. The app paces itself |
| OpenRouter | any model ending in `:free` | 20/min, 50/day (1,000/day after a one-time $10 credit purchase) | Free model list changes almost weekly |
| Ollama (local) | e.g. `qwen3:8b` | Unlimited | Runs on your laptop's GPU; slower, fully private |

The app never goes over a limit on purpose: it paces requests per provider, moves to the next
provider when one says "rate limited", and stops using AI for the day at `INPRO_LLM_DAILY_LIMIT`.

## 4. How the app keeps AI cheap (maximum output per request)

1. **Rules first.** Every invoice is read by the free rules reader. The AI is asked **only** when
   something is missing, the numbers don't add up, the document is a scan, or the vendor is unknown.
   On the 12 real test invoices, 8 of 12 digital PDFs needed **no AI request at all**.
2. **Text, not images.** The AI gets the invoice's text (from PyMuPDF or the free local OCR), not a picture.
   That works with every provider, including text-only free models, and it is what makes the grounding
   check possible: every AI value is looked up in that same text before it is accepted.
3. **Trimmed text.** Long documents are cut to the header plus the totals block.
4. **Cheapest model first, escalate only if needed.** Flash-Lite first; the stronger Flash model is asked
   only if the first answer is incomplete after verification (max 2 tries).
5. **Cache.** The same document text is never sent twice (stored in the database by a fingerprint of the text).
6. **Hard caps.** `INPRO_LLM_DAILY_LIMIT` (requests per day) and `INPRO_LLM_MONTHLY_BUDGET_USD` (paid spend).
7. **Low "thinking" effort.** Newer models think before answering and you pay for those tokens. Invoice
   reading doesn't need deep reasoning, so the app asks for low effort.

You can see the real numbers any time: the dashboard footer shows requests, tokens and estimated cost,
`GET /api/ai/usage` returns them as JSON, and every invoice's trace lists the model, tokens and cost of its request.

## 5. What it would cost on paid plans (estimates)

A typical invoice is about 1,000 input tokens and 300 output tokens per AI request.

| Model | Price per 1M tokens (in / out) | Per AI-read invoice | 1,000 invoices, hybrid (about 40% need AI) |
|---|---|---|---|
| Gemini 3.5 Flash-Lite | $0.30 / $2.50 | about $0.001 | about $0.40 |
| Gemini 3.8 Flash | $0.75 / $3.75 | about $0.002 | about $0.75 |
| Claude Haiku 4.5 | $1.00 / $5.00 | about $0.0025 | about $1.00 |

Prices from Google's and Anthropic's pricing pages, October 2026. Run
`python eval/run_benchmark.py --mode hybrid` to measure the real token counts for your setup.

## 6. Which setup for which situation

| Situation | Use |
|---|---|
| Demo for Hanif, public sample invoices | Gemini free + Groq free (`.env` as above) |
| Real company invoices (confidential) | Gemini **paid** tier (data not used for training), Claude, or local Ollama |
| No internet / strict privacy | Rules only, or local Ollama |

## 7. Settings reference

All settings live in `.env`; see `.env.example` for the full list with comments. The important ones:

| Setting | Default | Meaning |
|---|---|---|
| `INPRO_EXTRACTOR` | `auto` | `auto` = rules + AI when needed (if a key exists); `rules` = never AI; `ai` = AI on every invoice |
| `INPRO_GEMINI_MODELS` | `gemini-3.5-flash-lite,gemini-3.8-flash` | Order to try. `check_setup.py` lists valid names if one is wrong |
| `INPRO_LLM_ORDER` | `gemini,groq,openai,anthropic` | Provider order |
| `INPRO_LLM_DAILY_LIMIT` | `300` | Max AI requests per day |
| `INPRO_LLM_MONTHLY_BUDGET_USD` | `1.00` | Max estimated paid spend per month |
| `INPRO_LLM_MAX_TRIES` | `2` | Max successful AI answers per document (escalation) |
| `INPRO_INBOX_DIR` | `inbox` | Watched folder for PDFs, photos and saved e-mails (.eml) |
| `INPRO_INTAKE_INTERVAL` | `20` | Seconds between checks of the folder and the mailbox (`INPRO_INTAKE=0` switches intake off) |
| `INPRO_IMAP_HOST` / `_USER` / `_PASSWORD` | (empty) | A real mailbox to read invoices from (see section 8) |
| `INPRO_MANUAL_MINUTES` / `INPRO_REVIEW_MINUTES` | `7` / `2` | The two time assumptions behind "checking time saved" on the Overview page |

## 8. Connect a real mailbox (optional, free)

No API key is needed: the app reads e-mail over IMAP, which every major mail service offers.

**Gmail** (personal or Google Workspace):
1. Turn on 2-Step Verification for the Google account.
2. Create an app password at <https://myaccount.google.com/apppasswords> (16 letters).
3. Add to `.env`:
   ```
   INPRO_IMAP_HOST=imap.gmail.com
   INPRO_IMAP_USER=invoices.yourname@gmail.com
   INPRO_IMAP_PASSWORD=the16letterapppassword
   ```
4. Restart `run.bat`. The Inbox page shows "Connected". Every 20 seconds, unread mails are fetched, their PDF/image
   attachments are checked, and the mails are marked as read.

Tip: use a separate address just for invoices, not your personal inbox.

**Zoho Mail** works the same way (`imap.zoho.in` or `imap.zoho.com`, with a Zoho app password).
**Outlook.com / Microsoft 365** no longer accept passwords over IMAP; Microsoft requires its OAuth sign-in. That needs
a Microsoft Graph connection, which is the next step on the plan (see TECH_STACK.md). Until then, save an Outlook
message as a file and drop it into the `inbox` folder or upload it on the Inbox page.
