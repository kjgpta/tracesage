# tracesage showcase — 30 agentic apps, before & after

A gallery of **real, runnable** LangChain / LangGraph applications across the use cases
teams actually build — each shipped in **two versions**:

- **`before.py`** — the plain LangChain/LangGraph app. No tracesage.
- **`after.py`** — the *same* app with tracesage added. `diff before.py after.py` shows
  exactly how little it takes (usually `import tracesage` + one `with` block).

Run `before.py` to see the app work; run `after.py` to get the same result **plus** a
live trace UI. The point is the diff and what the trace reveals.

## Setup

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=...                 # or set LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY
# optional: export LLM_MODEL=gpt-4o-mini  LLM_PROVIDER=openai
```

Every app uses `init_chat_model`, so you can point it at any provider with `LLM_PROVIDER`
/ `LLM_MODEL`. Each app's README lists any extra dependency (search, vector store, MCP).

## Adding tracesage (the whole integration)

Synchronous apps:

```python
import tracesage

with tracesage.trace():        # starts the UI + captures every LangChain call
    result = chain.invoke(...)
```

Async / LangGraph apps:

```python
from tracesage import TraceSage

async with TraceSage.session(install=True) as tl:
    result = await graph.ainvoke(...)
    await tl.flush()
```

That's it — no `callbacks=[...]` threading. A `🔍 tracesage: …` link prints on the first
run; traces persist to disk, so `tracesage serve` reopens them later.

**Each demo is its own application.** Because the topology map and "Tools by source"
aggregate every run in a data dir, each `after.py` writes to its own dir
(`~/.tracesage/<folder-name>`, e.g. `~/.tracesage/01_support_faq_router`) so demos never
merge into one graph — the same pattern you should use to keep your own apps separate:

```python
from pathlib import Path
import tracesage

DATA_DIR = Path.home() / ".tracesage" / "my-app"          # one dir per application
with tracesage.trace(tracesage.TraceSageConfig(data_dir=DATA_DIR)):
    result = chain.invoke(...)
```

Each script prints its `Data dir:` and `tracesage runs -d <dir>` on startup; inspect a
demo with `tracesage runs -d ~/.tracesage/<folder-name>`. See
[Configuration → Isolating multiple applications](../../docs/configuration.md).

## The gallery

### A. Foundational patterns
| # | App | Domain · base · pattern |
|---|---|---|
| 01 | [Support FAQ Router](01_support_faq_router/) | customer support · LangChain · classify-and-route |
| 02 | [Web Research ReAct Agent](02_web_research_agent/) | research · LangChain AgentExecutor · tool-calling |
| 03 | [Text-to-SQL Analyst](03_text_to_sql_analyst/) | data/BI · LangChain · NL→SQL→answer (+error retry) |
| 04 | [Marketing Copy Generator](04_marketing_copy/) | marketing · LangChain LCEL · sequential chain |
| 05 | [Content Safety Pipeline](05_content_safety/) | trust & safety · LangChain · parallel fan-out |

### B. RAG & knowledge
| # | App | Domain · base · pattern |
|---|---|---|
| 06 | [Internal Docs Q&A](06_internal_docs_qa/) | enterprise knowledge · LangChain · retrieve-then-answer |
| 07 | [Multi-Query RAG](07_multi_query_rag/) | search · LangGraph · query-expansion fan-out |
| 08 | [Agentic RAG](08_agentic_rag/) | knowledge · LangGraph · retrieval loop |
| 09 | [RAG + Reranker](09_rag_reranker/) | search quality · LangChain · retrieve→rerank→cite |
| 10 | [Conversational RAG (memory)](10_conversational_rag/) | assistant · LangGraph · multi-turn sessions |

### C. Multi-agent systems
| # | App | Domain · base · pattern |
|---|---|---|
| 11 | [Supervisor Research Team](11_supervisor_research_team/) | research · LangGraph · supervisor |
| 12 | [Hierarchical Writing Org](12_hierarchical_writing/) | content ops · LangGraph · nested subgraphs |
| 13 | [Support Triage + Specialists](13_support_triage_specialists/) | customer support · LangGraph · specialist routing |
| 14 | [Competitive Intelligence Crew](14_competitive_intel_crew/) | strategy · LangGraph · parallel agents + synthesis |
| 15 | [Code-Migration Crew](15_code_migration_crew/) | software eng · LangGraph · dynamic fan-out |
| 16 | [Sales Lead Enrichment & Outreach](16_sales_lead_enrichment/) | sales/CRM · LangGraph · enrich→qualify→draft |
| 17 | [Debate-to-Decision](17_debate_to_decision/) | decision support · LangGraph · multi-persona loop |

### D. Tools & MCP
| # | App | Domain · base · pattern |
|---|---|---|
| 18 | [Personal Assistant (MCP)](18_personal_assistant_mcp/) | productivity · LangGraph · 2 MCP servers + local tool |
| 19 | [GitHub Issue Triage](19_github_issue_triage/) | software eng · LangChain · tool agent |
| 20 | [Multi-MCP Travel Planner](20_multi_mcp_travel/) | travel · LangGraph · single agent → many MCPs |
| 21 | [DevOps Incident Responder](21_devops_incident_responder/) | SRE · LangGraph · tool-heavy diagnose |
| 22 | [E-commerce Shopping Concierge](22_ecommerce_concierge/) | e-commerce · LangChain · action tools |

### E. Reasoning loops & evaluation
| # | App | Domain · base · pattern |
|---|---|---|
| 23 | [Reflexion Writer](23_reflexion_writer/) | content · LangGraph · writer-critic loop |
| 24 | [Plan-and-Execute Agent](24_plan_and_execute/) | automation · LangGraph · plan/execute |
| 25 | [Self-Correcting Code Generator](25_self_correcting_codegen/) | software eng · LangGraph · gen→test→fix loop |
| 26 | [LLM-as-Judge Eval Harness](26_llm_judge_eval/) | ML ops · LangGraph · eval + pytest fixture |
| 27 | [Map-Reduce Long-Doc Summarizer](27_map_reduce_summarizer/) | doc processing · LangChain · map-reduce |

### F. Domain verticals (structured extraction)
| # | App | Domain · base · pattern |
|---|---|---|
| 28 | [Invoice / Expense Extractor](28_invoice_extractor/) | finance/AP · LangChain · structured output + validation |
| 29 | [Contract Clause Risk Analyzer](29_contract_clause_analyzer/) | legal · LangGraph · parallel classify |
| 30 | [Insurance Claim Intake & Routing](30_insurance_claim_intake/) | insurance · LangGraph · extract→validate→route |

Each app folder has its own `README.md` explaining what the trace reveals.
