# Onboarding Guide: Research & Knowledge Retrieval

> **Target audience:** Students using the aider-factory RAG pipeline through opencode.
> **Primary tools:** `aider-oracle` (query your knowledge base) and `aider-research` (find documents on the web).
> **OS:** Linux (native). macOS (native). Windows (WSL — follow Linux steps).

---

## 1. What You're Getting

`aider-oracle` is a private research assistant that reads documents for you and answers questions from what it read. It builds a local vector database (LanceDB) from PDFs, HTML pages, and code — all on your machine. No data leaves your computer unless you explicitly configure a cloud model.

`aider-research` is a private metasearch tool. It queries search engines (including academic ones like arXiv, CrossRef, and Google Scholar) through a local SearXNG container. You use it to find documents, then hand the URLs to `aider-oracle` to ingest and query.

Together: **find → ingest → ask.**

---

## 2. Prerequisites

You need:

| Requirement | Check command | Install (if missing) |
|:---|:---|:---|
| Python 3.12 | `python3 --version` | `sudo apt install -y python3.12 python3.12-venv` |
| `uv` (package manager) | `uv --version` | `curl -LsSf https://astral.sh/uv/install.sh \| sh && source $HOME/.local/bin/env` |
| Internet connection | — | — |
| Podman or Docker (for SearXNG) | `podman --version` | `sudo apt install -y podman` |
| Build tools (for tree-sitter) | — | `sudo apt install -y build-essential cmake curl git universal-ctags` |
| An API key (your own endpoint) | — | Provided by your instructor / course |

After installing `uv`, close and reopen your terminal (or run `source $HOME/.local/bin/env`) so the `uv` command is available.

---

## 3. Install aider-factory

```bash
uv tool install --force git+https://github.com/bryan506/aider-factory.git
```

Verify the installation:

```bash
aider-oracle --list
```

An empty response (no error) means everything is installed correctly.

---

## 4. Bootstrap Your Workspace

Navigate to your project directory and run:

```bash
mkdir -p ~/my-research && cd ~/my-research
aider-helper bootstrap
cp .aider_factory/.env_*.yml .aider_factory/.env.yml 2>/dev/null || true
python3 -c "import glob; [open(p, 'w').write(open(p).read().replace('http://<your-router-host>:4000/v1', '')) for p in glob.glob('.aider_factory/.env*.yml')]" 2>/dev/null || true
```

This single command:

1. Detects your API key from the environment.
2. Queries your endpoint for available models (architect, embedder, reranker).
3. Detects your test framework from filesystem markers.
4. Writes your working config to `.aider_factory/.env.yml`.
5. Starts the SearXNG web search container.
6. Provisions MiniCheck (the grounding validator).
7. Creates the `.aider_factory/` directory structure.

Takes ~30 seconds. You should see a summary table listing your detected models, framework, and target file.

If it reports "No API key detected," export yours:

```bash
export GEMINI_API_KEY="your-key-here"
```

Then re-run `aider-helper bootstrap`.

---

## 5. Your Configuration Files

After bootstrap, two files matter:

| File | Purpose |
|:---|:---|
| `.aider_factory/.env.yml` | **Your working config.** Auto-generated. Edit this to change models, endpoints, or pipeline behavior. |
| `src/aider_factory/default_configs/sample_yaml_config/complete_env.yml` | **Full reference.** Every available option with comments. Open it to understand what's possible. You don't need to change it. |

### The `batch` Setting

Inside your `.aider_factory/.env.yml`, look for:

```yaml
batch: true
```

- **`batch: true`** — Ingest all files into LanceDB *first*, then answer queries. Good for a fixed corpus (e.g., a semester's reading list). Tables are overwritten on re-ingest.
- **`batch: false`** — Ingest and query incrementally. Good for a growing knowledge base where you add documents over time. Tables are appended to.

For this guide, the default works as-is. Change it only when you need a different workflow.

---

## 6. Collections

A **collection** is a named bucket in LanceDB. You can have many: one per research topic, one per course module, one per codebase.

- If you don't specify `--collection`, the tool defaults to whatever is configured in your `.env.yml`, or your workspace folder name as a fallback.
- **Best practice:** Always specify `--collection` explicitly until you're comfortable with config behavior. This keeps topics separated.

```bash
aider-oracle --collection my_topic "question here"
```

---

## 7. Task 1: Find & Read Two arXiv Papers

**Goal:** Search for academic papers, download them, and ask questions.

### Step A — Search

```bash
mkdir -p temp
aider-research search "high frequency market microstructure liquidity" --academic --top 5 --links-only --out temp/arxiv_urls.txt
```

This queries academic engines (arXiv, CrossRef, CORE) and writes the top 5 result URLs to `temp/arxiv_urls.txt`. Open the file to see what was found.

### Step B — Ingest

```bash
# If temp/arxiv_urls.txt contains harvested URLs:
aider-oracle --collection microstructure --add-web --file temp/arxiv_urls.txt

# Fallback (if web search was rate-limited or returned paywalled landing pages):
aider-oracle --collection microstructure --add-web "https://arxiv.org/pdf/0906.1444.pdf"
aider-oracle --collection microstructure --add-web "https://www.nber.org/papers/w13825.pdf"
```

This downloads each URL (PDFs included), converts them to clean text, splits into chunks, embeds them locally with `bge-m3` (runs on CPU — takes 1–3 minutes for the first run while the 2.2 GB model downloads), and stores them in LanceDB under the `microstructure` collection.

### Step C — Ask

```bash
aider-oracle --collection microstructure "How is market microstructure noise related to financial measures of stock liquidity?"
aider-oracle --collection microstructure "How do high-frequency transaction prices disentangle fundamental components from noise?"
```

You'll get an answer grounded in the ingested papers, with source citations.

---

## 8. Task 2: Ingest a PDF Manual (PerformanceAnalytics)

**Goal:** Index a real R package reference manual and query it.

### Ingest the PDF

```bash
aider-oracle --collection r_docs --add-web "https://cran.r-project.org/web/packages/PerformanceAnalytics/PerformanceAnalytics.pdf"
```

Wait for the embedding to finish (progress prints to terminal).

### Query

```bash
aider-oracle --collection r_docs "How do I calculate the Sharpe ratio and what are its parameters?"
aider-oracle --collection r_docs "What is the function signature for drawdown calculation?"
aider-oracle --collection r_docs "How does the package handle missing data in return series?"
```

---

## 9. Task 3: Harvest HTML Documentation (quantmod)

**Goal:** Ingest a package's web documentation (not just the PDF) for richer retrieval.

### Option A — Ingest the PDF manual + the CRAN page

```bash
aider-oracle --collection r_docs --add-web "https://cran.r-project.org/web/packages/quantmod/quantmod.pdf"
aider-oracle --collection r_docs --add-web "https://cran.r-project.org/web/packages/quantmod/index.html"
```

### Option B — Harvest a documentation site with a sitemap

For sites that publish a `sitemap.xml` or `llms.txt` manifest, you can harvest all URLs automatically. Here we harvest the live Astral `uv` documentation:

```bash
# Discover all pages published in the Astral uv sitemap:
aider-research search "https://docs.astral.sh/uv/sitemap.xml" --sitemap --links-only --out temp/all_uv_urls.txt

# Select the top 5 pages to keep the download fast:
head -n 5 temp/all_uv_urls.txt > temp/uv_urls.txt

# Batch ingest the selected documentation pages:
aider-oracle --collection uv_docs --add-web --file temp/uv_urls.txt --workers 4

# Query the ingested documentation:
aider-oracle --collection uv_docs "How do I install and manage global command-line tools using uv tool?"
aider-oracle --collection uv_docs "What is the difference between uv cache and temporary build directories?"
```

---

## 10. Make It Yours

Swap the topics, swap the packages, try different sources:

```bash
# Deep learning time series forecasting (Informer paper on arXiv):
aider-oracle --collection dl_forecast --add-web "https://arxiv.org/pdf/2012.07436.pdf"
aider-oracle --collection dl_forecast "How does the ProbSparse self-attention mechanism in Informer reduce time complexity to O(L log L)?"
aider-oracle --collection dl_forecast "What is the purpose of the self-attention distilling operation in Informer?"

# Quantitative finance R package (tidyquant manual):
aider-oracle --collection r_docs --add-web "https://cran.r-project.org/web/packages/tidyquant/tidyquant.pdf"
aider-oracle --collection r_docs "How do I compute a rolling correlation matrix using tq_transmute?"

# Ingest a local PDF you already have (replace path with your own PDF):
aider-oracle --collection my_notes --add-file ~/Downloads/lecture_slides.pdf
aider-oracle --collection my_notes "Summarize the key findings from slide 12."
```

---

## 11. Cheat Sheet

| Flag | What it does |
|:---|:---|
| `--academic` | Route search to academic engines (arXiv, CrossRef, Scholar) |
| `--links-only` | Output plain URL list (one per line) for batch ingestion |
| `--top <N>` | Return N search results (default: 10) |
| `--out <path>` | Write results to a file instead of terminal |
| `--sitemap` | Harvest URLs from a sitemap.xml or llms.txt |
| `--no-rag` | Convert to clean Markdown on disk; skip vector indexing |
| `--workers <N>`, `-w` | Parallel URL fetching during batch ingestion |
| `--collection <name>` | Target a specific LanceDB collection |
| `--clear` | Wipe the current session's conversation history |
| `--list` | Show all ingested tables/collections |
| `--add-file <path>` | Ingest a local file (PDF, MD, code, etc.) |
| `--add-web <url>` | Ingest a URL (HTML or PDF) |
| `--type code\|docs` | Restrict retrieval to code or documentation tables |
| `--no-rerank` | Skip the reranker stage; return raw similarity results |

---

## 12. Troubleshooting

| Symptom | Fix |
|:---|:---|
| `aider-research` returns 0 results | Upstream rate limit. Enable a VPN or wait 10 minutes. |
| First `--add-web` is very slow | The 2.2 GB `bge-m3` embedding model is downloading. One-time cost. Subsequent runs are fast. |
| "Collection not found" or empty results | You haven't ingested into that collection yet. Run `--add-web` or `--add-file` first. |
| "No API key detected" | Run `export GEMINI_API_KEY="..."` (or your provider's variable) and retry. |
| "Nothing returned" to a query | Broaden your question, or add `--no-rerank` to see raw results. |
| SearXNG connection refused | The container isn't running. Re-run `aider-helper bootstrap` or `podman start searxng`. |
| `aider-oracle --list` shows nothing | You haven't ingested anything yet. That's expected on a fresh workspace. |
| Connection error to `<your-router-host>` | Run `python3 -c "import glob; [open(p, 'w').write(open(p).read().replace('http://<your-router-host>:4000/v1', '')) for p in glob.glob('.aider_factory/.env*.yml')]"` |

---

## 13. Using Through opencode

All commands above work in any terminal, including opencode's integrated terminal and its `/run` command. Your opencode agent can invoke them programmatically:

```
/run aider-oracle --collection r_docs "What is the Sharpe ratio formula?"
/run aider-research search "portfolio optimization" --academic --top 5 --links-only --out temp/pf_urls.txt
```

### OpenCode Subagent Navigation
In the OpenCode interface, you can isolate tool execution:
- **Tab -> select 'researcher'** -> `aider-research search "high frequency market microstructure liquidity" --academic --top 3 --links-only --out temp/arxiv_urls.txt`
- **Tab -> select 'oracle'** -> `aider-oracle --collection microstructure "How does adverse selection affect bid-ask spreads?"`

The tools are standard CLI binaries on your `PATH`. They don't require a GUI, a notebook, or any special runtime — just a shell.

---

## 14. What's Next

Once you're comfortable with `aider-oracle` and `aider-research`:

- **Pipeline sessions:** `aider-factory .aider_factory/.env.yml <session_name>` starts a multi-phase automated workflow. Re-run with the same name to resume.
- **Session management:** See `.aider_factory/markdown/docs/session_management_and_cluster.md` for listing, switching, and clearing sessions.
- **Full config reference:** `src/aider_factory/default_configs/sample_yaml_config/complete_env.yml`
- **Project README:** `README.md` (full architecture, all CLI tools, advanced examples)

