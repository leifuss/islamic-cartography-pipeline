# Cross-Cutting: Research Scope & Questions

> **Goal:** The researcher defines the scope of their project and their
> key research questions.  This context is used throughout the platform
> to personalise search suggestions, citation discovery, and AI
> commentary.

## 1. User Stories

- *As a researcher setting up my library, I want to describe what my
  project is about so the tool can give me relevant suggestions.*
- *As a researcher, I want to update my research questions as my
  project evolves.*
- *As a researcher, I want search and discovery features to understand
  what I'm working on, not just what I've collected.*

## 2. What the User Defines

### Research Scope

A brief description of the research project — what the bibliography
is about, what period, what region, what discipline.

```
Example:
"Ottoman cartographic practices and their relationship to earlier
Islamic and Ptolemaic mapping traditions, from the 10th to 18th
century.  Particular focus on the transmission of astronomical and
geographic knowledge through Arabic intermediaries."
```

This is free text, 1-5 sentences.

### Research Questions

Specific questions the researcher is investigating. These are more
actionable than the scope and directly drive discovery and commentary.

```
Example:
1. "How did Ottoman cartographers adapt Ptolemaic projection methods?"
2. "What role did maritime trade routes play in Islamic map-making?"
3. "How were astronomical observations integrated into cartographic practice?"
4. "What was the relationship between portolan charts and Islamic mapping?"
```

Typically 2-5 questions.  Each is a natural-language sentence.

### Keywords / Topics (optional)

A list of key terms that the researcher considers central to their
work.  These supplement the questions with specific vocabulary.

```
Example:
["portolan charts", "Ptolemaic projection", "al-Idrisi", "Ottoman",
 "qibla determination", "astronomical tables", "Mediterranean"]
```

## 3. Storage: `data/research_scope.json`

```json
{
  "version": 1,
  "updated_at": "2026-02-20T16:00:00Z",
  "scope": "Ottoman cartographic practices and their relationship to earlier Islamic and Ptolemaic mapping traditions, from the 10th to 18th century.  Particular focus on the transmission of astronomical and geographic knowledge through Arabic intermediaries.",
  "questions": [
    "How did Ottoman cartographers adapt Ptolemaic projection methods?",
    "What role did maritime trade routes play in Islamic map-making?",
    "How were astronomical observations integrated into cartographic practice?",
    "What was the relationship between portolan charts and Islamic mapping?"
  ],
  "keywords": [
    "portolan charts",
    "Ptolemaic projection",
    "al-Idrisi",
    "Ottoman",
    "qibla determination",
    "astronomical tables",
    "Mediterranean"
  ]
}
```

## 4. Where It Is Used

### Stage 1: Setup Wizard

The setup wizard includes a "Research Scope" step:

```
┌─────────────────────────────────────────────────────────┐
│  Step 3 of 4: Describe your research                    │
│                                                         │
│  What is this bibliography about?                       │
│  ┌───────────────────────────────────────────────────┐  │
│  │ Ottoman cartographic practices and their          │  │
│  │ relationship to earlier Islamic and Ptolemaic...  │  │
│  └───────────────────────────────────────────────────┘  │
│                                                         │
│  What questions are you investigating? (one per line)   │
│  ┌───────────────────────────────────────────────────┐  │
│  │ How did Ottoman cartographers adapt Ptolemaic...  │  │
│  │ What role did maritime trade routes play in...     │  │
│  │ How were astronomical observations integrated...  │  │
│  └───────────────────────────────────────────────────┘  │
│                                                         │
│  Key terms (comma-separated, optional):                 │
│  ┌───────────────────────────────────────────────────┐  │
│  │ portolan charts, Ptolemaic projection, al-Idrisi  │  │
│  └───────────────────────────────────────────────────┘  │
│                                                         │
│  [Skip for now]                    [Save & continue →]  │
└─────────────────────────────────────────────────────────┘
```

This step is optional ("Skip for now") — the tool works without it,
but becomes smarter with it.

### Stage 2: Explorer

A "Research Scope" panel in the explorer sidebar:
- Shows the scope statement and questions
- "Edit" button opens an inline editor
- Questions link to pre-filled search queries (Stage 4)

The explorer can also show a "relevance" indicator per item: how
closely each item's title/abstract matches the research scope (simple
keyword overlap, no LLM needed).

### Stage 4: Search

- Research questions appear as "Suggested searches" above the search
  box
- Keywords are used to boost search results that contain them
- The scope statement is shown as context in RAG prompts

### Stage 5: Citation Discovery

Research questions are the primary driver of personalised discovery:

```python
def rank_recommendations(refs, research_questions):
    for ref in refs:
        # Boost works cited in passages that discuss the user's questions
        question_relevance = compute_question_relevance(
            ref, research_questions)
        ref["relevance_score"] += question_relevance * 15
```

The gap analysis can be framed as: "Based on your research question
about X, your sources frequently cite Y, but you don't have it."

### Stage 6: Commentary

Research questions orient the AI commentary:

```
System prompt:
"The researcher is investigating: {scope}

Their key questions are:
1. {question_1}
2. {question_2}

When generating commentary on this text, focus on aspects relevant
to these questions.  Highlight where this text addresses them and
where it leaves gaps."
```

## 5. Editing the Research Scope

The scope must be editable at any time — research evolves.

**In the explorer:** A "Research Scope" section (collapsible) at the
top of the page with an "Edit" button.

**How edits are saved:** Same mechanism as annotations — GitHub
Contents API commit or localStorage + periodic sync.

**When the scope changes:**
- No automatic re-processing needed
- But the user can manually trigger "Re-run discovery" or
  "Re-generate commentary" to use the updated questions
- A banner: "Your research scope was updated.  Some features may
  benefit from re-running. [Re-run discovery] [Re-generate commentary]"

## 6. Implementation Tasks

| # | Task | Effort | Stage | Priority |
|---|------|--------|-------|----------|
| S.1 | `data/research_scope.json` schema | 0.25 day | Alpha | P0 |
| S.2 | Setup wizard: research scope step | 0.5 day | Alpha | P0 |
| S.3 | Explorer: scope display panel | 0.5 day | Alpha | P0 |
| S.4 | Explorer: inline scope editor | 0.5 day | Alpha | P1 |
| S.5 | Save scope via GitHub API | 0.25 day | Alpha | P0 |
| S.6 | Search: suggested queries from questions | 0.5 day | v1.0 | P1 |
| S.7 | Discovery: question-aware relevance boost | 0.5 day | v1.5 | P0 |
| S.8 | Commentary: scope-aware prompt engineering | 0.5 day | v2.0 | P0 |
| **Total (P0)** | | **~2.5 days** | | |
| **Total (all)** | | **~3.5 days** | | |

## 7. Testing Plan

| Test case | Method |
|-----------|--------|
| Setup wizard saves research_scope.json | End-to-end test |
| Explorer displays scope statement | Visual check |
| Scope editor saves changes | Edit, refresh, verify |
| Empty scope (user skipped setup step) | Verify graceful handling |
| Search suggestions match questions | Visual check |
| Discovery ranking changes with different questions | Compare results |
| Commentary references user's questions | Read generated commentary |

## 8. Open Questions

1. **Auto-generated scope:** Could we infer the research scope from
   the bibliography itself (LLM summarisation of titles and abstracts)?
   This would provide a starting point for users who skip the setup
   step.  **For later** — nice-to-have but not MVP.

2. **Multiple scopes per collection:** If a user has multiple
   collections (multi-collection support), should each have its own
   scope?  **Yes** — `research_scope.json` should be per-collection
   when multi-collection is active.

3. **Scope versioning:** Should we keep a history of scope changes?
   **Not for MVP** — git history provides this implicitly.
