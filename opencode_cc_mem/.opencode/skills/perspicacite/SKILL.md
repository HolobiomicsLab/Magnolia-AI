---
name: perspicacite
description: "Perspicacite v2 literature RAG (localhost:8000): agentic/literature_survey modes, knowledge-base workflow, DOI ingest pitfalls, server lifecycle. Use for literature search, paper discovery, building/querying paper KBs, field surveys. Do NOT use for paywalled full-text or supplements; use institutional access or Sci-Hub instead."
metadata:
  version: "1.0"
  last_verified: "2026-06-09"
  tags: "[literature, search, rag, perspicacite, papers, academic]"
---

# Perspicacité v2 — Scientific Literature RAG

Perspicacité is a local-first RAG system for searching, downloading, and reasoning
over academic literature. It runs as a FastAPI server + MCP endpoint on
`localhost:8000`. Magnolia uses it when a task requires finding papers, extracting
binding data, or surveying a field — **before** downloading individual PDFs from
publishers or Sci-Hub.

## When to use it

| Task | Mode |
|------|------|
| Find papers on a protein/ligand/topic | `agentic` or `literature_survey` |
| Extract specific data from papers (e.g. compound lists, IC50) | `agentic` |
| Build a literature knowledge base for repeated queries | KB workflow (see below) |
| One-off fact retrieval ("what does paper X say about Y?") | `basic` or `advanced` |

Do NOT use perspicacité to access papers behind paywalls — it respects
publisher access controls. For paywalled papers, fall back to institutional
access, Sci-Hub, or manual download.

## Setup (one-time)

```bash
# The repo is at ~/repos/perspicacite_v2
cd ~/repos/perspicacite_v2

# Copy .env if not already present
cp .env.example .env
# Edit .env to add DEEPSEEK_API_KEY or whichever LLM provider key
```

Start the server:

```bash
cd ~/repos/perspicacite_v2 && ./dev.sh
```

This boots the backend on `:8000` and the web UI on `:3000`. The backend takes
~60 seconds to load ML models (PyTorch, sentence-transformers).

**Heads-up — only one instance at a time.** `./dev.sh` binds ports 8000 and
3000. If it's already running, do not start a second one.

## Server lifecycle (for headless sessions)

The agent must manage the server without user intervention:

```bash
# 1. Check if already running
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/

# 2. If it returns 000 or an error, start it in background:
cd ~/repos/perspicacite_v2 && nohup ./dev.sh > /tmp/perspicacite.log 2>&1 &

# 3. Wait for it to become ready (up to 90s):
for i in $(seq 1 18); do
  code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/)
  [ "$code" != "000" ] && break
  sleep 5
done

# 4. Verify
curl -s http://localhost:8000/api/kb | python3 -m json.tool | head -5
```

Never restart the server unless it's actually down. The `./dev.sh` script brings
up both the backend and the Next.js frontend — the frontend port conflict on
`:3000` is the most common reason a second `./dev.sh` fails.

## Core API

All interaction goes through the REST API at `http://localhost:8000`. The MCP
endpoint is at `/mcp` (SSE protocol) but for ad-hoc queries, the `/api/chat`
endpoint is simpler.

### 1. Literature search via chat

```bash
curl -s -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "query": "<your research question>",
    "mode": "agentic",
    "stream": false,
    "max_papers": 10
  }'
```

### 2. RAG modes

| Mode | What it does | Cost (LLM calls) | Use for |
|------|-------------|------------------|---------|
| `basic` | Single KB retrieval + answer | 1 | Quick fact checks |
| `advanced` | Multi-step retrieval + synthesis | 2-3 | Structured answers |
| `agentic` | LLM-planned multi-step research with tool use | 6+ | Complex questions, data extraction |
| `literature_survey` | Structured survey with gap analysis | 4-6 | Field overviews |
| `contradiction` | Finds conflicting claims across papers | 4-6 | Identifying disagreements |

`agentic` is the go-to for Magnolia tasks — it searches databases, screens
abstracts, and extracts structured data.

### 3. Knowledge base workflow (for repeated queries on a topic)

```bash
# Step 1: Create a KB
curl -s -X POST http://localhost:8000/api/kb \
  -H "Content-Type: application/json" \
  -d '{"name": "my_topic", "description": "Papers about X"}'

# Step 2: Bulk-add papers by DOI
curl -s -X POST "http://localhost:8000/api/kb/my_topic/dois/async" \
  -H "Content-Type: application/json" \
  -d '{"dois": ["10.1073/pnas.1711437114", "10.3390/s18103248"]}'

# Step 3: Query your KB
curl -s -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"query": "...", "kb_name": "my_topic", "mode": "basic"}'
```

### 4. Listing KBs and papers

```bash
curl -s http://localhost:8000/api/kb                          # list all KBs
curl -s "http://localhost:8000/api/kb/my_topic/papers"        # list papers in KB
curl -s "http://localhost:8000/api/paper?doi=10.xxx/yyy"      # get paper details
```

### 5. Checking async job status

DOI ingestion is async. Track with:

```bash
curl -s "http://localhost:8000/api/jobs/<job_id>"
```

**DOI ingest reliability — expect partial failures.** Batch DOI ingestion often
stalls on publisher paywalls. Observed behavior (2026-06-09, 6 DOIs submitted):

- 2 papers ingested successfully
- 4 jobs stalled at `status: "running"` indefinitely

**If a job stays "running" >5 minutes, stop waiting.** Query the KB directly —
partially ingested papers may have metadata (title, abstract, DOI) even if full
text didn't download. The `/api/chat` endpoint can still work with abstracts
alone in `agentic` mode.

**Fallback strategy for failed ingest:**

```bash
# 1. Check what the KB actually has
curl -s "http://localhost:8000/api/kb/<kb_name>/papers" | python3 -m json.tool

# 2. If a specific paper failed to ingest, try adding it individually with a
#    manual download: get the PDF via Sci-Hub, then use the PDF-dropzone endpoint
curl -s -X POST http://localhost:8000/api/pdf-dropzone \
  -F "file=@paper.pdf"

# 3. Or accept partial results — agentic mode can still work with abstracts
curl -s -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"query": "...", "kb_name": "my_topic", "mode": "agentic"}'
```

## Currently available KBs

**This static table goes stale — always verify live before relying on it:**

```bash
curl -s http://localhost:8000/api/kb                    # authoritative KB list + paper counts
curl -s http://localhost:8000/api/kb/<name>/papers      # papers + whether full-text ingested
```

**Before any literature query, check `/api/kb` live AND `memory_search` for a project KB.** Do not conclude "no KB exists" from this table alone. (A June 2026 session did exactly that: it trusted this list, missed the `Hsc70_peptide_binding` KB built earlier, and wasted a query on an abstract-only live search.)

Snapshot (verify live — counts and ingest status change over time):

| KB | Papers | Topic | Ingest status |
|----|--------|-------|---------------|
| `Hsc70_peptide_binding` | 16 | Hsc70/Hsp70 peptide binding (4PO2, BiPPred, Sahu&M 2025, Torielli, DnaK lid) + 8 CMA-degrader papers | **abstract-only** — a prior memory entry claimed "5 full-text PDFs" but all 16 retrieve as `[abs]` (full-text ingest failed). Does NOT cover NBD, co-chaperones, or the allosteric cycle |
| `AI_scientist` | 15 | AI for science | — |
| `Computational_metabolomics` | 11 | Metabolomics | — |
| `agent_memory` | ? | (verify live) | — |
| `Agentic_system_for_scientific_research` | ? | (verify live) | — |
| `OBP5NGH_research` | 0 (ingestion failed) | OBP3/5NGH | **needs rebuilding** |

For topics the relevant KB excludes (e.g. the Hsp70 allosteric cycle / interdomain interface), run a live `agentic` database search — the KB and live search are **complementary**, not interchangeable.

## Server management

```bash
# Check if running
curl -s http://localhost:8000/health

# The health endpoint may return 404 — that's OK. Check the root:
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/

# Kill and restart
pkill -f "uvicorn.*perspicacite" 2>/dev/null
cd ~/repos/perspicacite_v2 && ./dev.sh
```

## Common pitfalls

| Problem | Likely cause | Fix |
|---------|-------------|-----|
| Search returns 0 papers | Query too specific | Broaden keywords |
| All papers are "INSUFFICIENT" | Full text not ingested | Add DOIs to KB first, let async ingest complete |
| KB shows 0 papers after DOI add | Ingestion still running | Check job status; wait for completion |
| `local_paths` rejected | Server-side ingest disabled | Use DOI-based ingest only |
| DOI ingest stalls | Publisher paywall | Try open-access DOIs or add PDF manually |
| Backend won't start | Port 8000 in use | `pkill -f uvicorn` then restart |

## What perspicacité cannot do

Perspicacité searches databases and extracts text from ingested papers. These
tasks are outside its scope — use alternative methods:

| Limitation | Why | Alternative |
|------------|-----|-------------|
| **Access supplementary data** | Publisher paywalls block Excel/PDF supplements | Institutional login at journal site, email authors, Sci-Hub |
| **Distinguish primary data from reviews** | The LLM extracts facts but doesn't know which paper originally generated them | Manual cross-check of cited sources |
| **Extract data from uningested PDFs** | Only ingested full-text is searchable; PDFs added by DOI that failed to ingest have abstract-only coverage | Manual PDF download + `pdf-dropzone` endpoint, or direct reading |
| **Extract tables or figures** | The RAG pipeline extracts prose, not structured tables | Manual reading of supplementary Excel files |
| **Reproduce exact numerical values** | LLM extraction may paraphrase or round numbers | Always verify critical values against the original paper |
| **Replace targeted database searches** | For known DOIs, direct PubMed/PMC access is faster and more reliable | Use PMC for full text, PubMed for metadata, perspicacité for discovery |

## Comparison to manual literature search

| | Perspicacité | Manual (Google Scholar + PMC) |
|---|---|---|
| Speed | Seconds | Minutes to hours |
| Database coverage | Semantic Scholar + PubMed + OpenAlex + arXiv | Google Scholar only |
| Full-text access | Limited to OA; behind-paywall abstracts only | Can access behind paywalls (with institutional login) |
| Structured extraction | LLM-powered table extraction | Manual reading |
| Best for | Quick surveys, finding relevant DOIs | Getting supplementary data, full-text PDFs |

## When this rule is wrong

- The perspicacité repo moves or the API changes → re-probe endpoints with `curl localhost:8000/openapi.json`
- The server port or startup procedure changes → check `dev.sh`
- A new LLM provider is configured → check `.env` and `config.yml`
- The KB naming convention changes → check `/api/kb` response
