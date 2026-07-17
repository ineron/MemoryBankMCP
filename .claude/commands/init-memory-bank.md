---
description: Initialize Memory Bank structure with metacognitive awareness and self-reflection
---

# Initialize Memory Bank System with Metacognitive Flow

## Your Task

Think deeply about the project context and initialize the Memory Bank system using a metacognitive approach that ensures profound understanding and thoughtful implementation.

### Initial Setup: Task Tracking

Use the TodoWrite tool to create a structured task list for this complex initialization process:

```
todos:
- Stage 1: Understanding and Context Recognition (pending)
- Stage 2: Preliminary Judgment Formation (pending)
- Stage 3: Critical Evaluation and Deep Analysis (pending)
- Stage 4: Adaptive Strategy with Confidence Scoring (pending)
- Stage 5: Reflective Implementation (pending)
- Stage 6: Final Metacognitive Assessment (pending)
```

Update each task status as you progress:
- Mark as `in_progress` when starting a stage
- Mark as `completed` only when fully satisfied with that stage
- Add sub-tasks if a stage reveals complex requirements

### Stage 1: Understanding and Context Recognition

**Metacognitive Understanding Phase**
Before any action, deeply understand:
- What is the true purpose of this initialization?
- What assumptions am I making about this project?
- What context clues should I be aware of?

Use the Task tool with this enhanced prompt:

"Perform a metacognitive analysis of the current project:
1. Initial observations about project state
2. What this tells me about the project's nature
3. What I might be assuming incorrectly
4. Alternative interpretations of the project structure
5. Confidence level in my initial assessment (0-100%)

Then provide standard analysis:
- Current working directory path
- Memory bank registration status: is this project's slug already registered?
  Call `project_list()` and check; if `.claude/settings.json` has no
  `project.slug` yet, that's also a signal this is a fresh init.
- Whether a **legacy `memory-bank/*.md` tree** exists from before the
  Postgres MCP migration — if so, this is a migration, not a fresh init;
  flag it for Stage 4/5 (`memory_import` should do the heavy lifting, not
  manual re-entry of content that already exists in the files)
- Project type detection (package.json, requirements.txt, etc.)
- Technologies and frameworks
- Project structure overview
- Existing documentation"

**Self-Reflection Questions**:
- Am I seeing the full picture?
- What might I be missing?
- How does my interpretation align with reality?

### Stage 2: Preliminary Judgment Formation

Based on initial understanding, form hypotheses:
- What type of Memory Bank would best serve this project?
- What initialization approach seems most appropriate?
- What are my intuitions telling me?

**Self-Reflection Prompts**:
- Why do I think this approach is best?
- What biases might be influencing my judgment?
- What would an alternative perspective suggest?
- How confident am I in this preliminary judgment? (0-100%)

### Stage 3: Critical Evaluation and Deep Analysis

Use the Task tool for comprehensive analysis with critical lens:

"Critically analyze the project with these considerations:
1. Standard project analysis (files, structure, technologies)
2. What contradicts my initial assumptions?
3. What edge cases or special considerations exist?
4. How confident am I in each finding (0-100%)?
5. What information am I missing that could change my approach?
6. What patterns do I see that might not be obvious?"

**Metacognitive Checkpoints**:
- Are my interpretations consistent?
- What evidence supports or refutes my approach?
- How would this look from a beginner's perspective?
- How would an expert view this differently?

### Stage 4: Adaptive Strategy with Confidence Scoring

Based on critical evaluation, determine strategy with confidence levels:

```
Strategy Decision Tree:
├── Fresh Installation — no project row, no legacy files (Confidence: X%)
│   └── Why: [Metacognitive reasoning]
├── Partial Update — project row exists, some node kinds missing (Confidence: Y%)
│   └── Why: [Metacognitive reasoning]
└── Migration — legacy memory-bank/*.md tree found (Confidence: Z%)
    └── Why: [Metacognitive reasoning] — use `memory_import`, don't
        hand-transcribe file contents into memory_upsert calls one by one
```

**Decision Factors**:
- Existing memory bank state (`project_list()` result, presence of a
  legacy `memory-bank/` directory)
- Project complexity and maturity
- User's explicit needs
- Confidence in understanding

### Stage 5: Reflective Implementation

0. **Bootstrap the project row(s) first** (skip if `project_list()` already
   shows this project registered):
   - If this project belongs to a family of related products (ask the user
     if unclear — don't assume), `project_group_create(slug, name)` for the
     group first, then `project_create(slug, name, group_slug=...)`.
   - Otherwise just `project_create(slug, name)`.
   - Write the resulting `slug` into `.claude/settings.json`'s
     `project.slug` so every other command can find it.

0.5. **If a legacy `memory-bank/*.md` tree exists**, run
   `memory_import(project, path="memory-bank")` instead of manually
   recreating nodes — it splits files into nodes by heading, infers
   `kind`/`topic`, and embeds them. Only fall through to manual creation
   below for content the importer couldn't confidently classify.

For each memory bank node kind (manual creation, or filling gaps the
importer left):

1. **Pre-creation reflection**: 
   - Why is this node necessary?
   - What purpose will it serve?
   - How confident am I it's needed? (0-100%)

2. **Content consideration**:
   - What makes this content meaningful?
   - How does it connect to other nodes? (Are there `memory_link` edges
     worth creating now — e.g. a `pattern` node this `decision` depends on?)
   - What future value will it provide?
   - What `topic` tags make this findable later without a blanket dump?

3. **Implementation** — `memory_upsert(project, kind=..., title=..., body=...,
   topic=[...])` for each:
   - `kind="brief"` - Foundation document (core requirements & scope)
   - `kind="product"` - Product vision and goals
   - `kind="active"` - Current state and focus. Exactly **one** live node of
     this kind per project (see the Lazy Loading Principle in
     `claude-memory-bank.md`) — `/start` reads only this node plus
     `memory_tasks`, so it must be self-sufficient for session-start triage.
   - `kind="task"` (one per task) - seed the initial task list, graded with
     `priority` (1-9) / `importance` (1-5) / `topic` from the start — see
     `/save`'s Task Upkeep section for the grading scale
   - `kind="pattern"` - Architecture decisions
   - `kind="tech"` - Technology details
   - `kind="progress"` - Initial development status (dated)

4. **Post-creation validation**:
   - Does this achieve the intended purpose?
   - What could be improved?
   - Confidence in this node: (0-100%)

### Stage 6: Final Metacognitive Assessment

Complete the initialization with comprehensive self-evaluation:

**Overall Assessment**:
- Overall confidence in the initialization (0-100%)
- What went well and why?
- What could have been done differently?
- What learnings will inform future initializations?
- Which decisions am I least/most confident about?

**Summary Display**:
- Nodes created vs preserved (and, if applicable, node count imported from
  a legacy `memory-bank/` tree via `memory_import`)
- Confidence levels for each decision
- Key metacognitive insights gained
- Areas of uncertainty identified

**Contextual Next Steps with Reasoning**:
- For new projects: `/workflow:understand` (Why: Establishes deep context)
- For existing projects: `/workflow:plan` (Why: Builds on current state)
- For updated Memory Banks: Review changes (Why: Ensures accuracy)

**Final Reflection**:
"What did I learn about this project that wasn't immediately obvious?"

## Enhanced Implementation Patterns

### Pattern 1: Assumption Validation Loop
```
Assumption → Evidence Check → Validation/Refutation → Adjusted Approach
```

### Pattern 2: Multi-Perspective Analysis
```
Technical Perspective: How does the code structure inform initialization?
User Perspective: What would the developer expect?
Future Perspective: How will this scale as the project grows?
Alternative Perspective: What would a different approach reveal?
```

### Pattern 3: Confidence-Weighted Decisions
```
High Confidence (80-100%): Proceed with implementation
Medium Confidence (50-79%): Seek additional validation
Low Confidence (<50%): Consider alternative approaches
```

## Important Metacognitive Guidelines

- **Think Before Acting**: Every action should follow reflection
- **Question Assumptions**: Continuously validate initial judgments
- **Document Reasoning**: Include "why" for every decision
- **Embrace Uncertainty**: Low confidence is valuable information
- **Learn from Process**: Each initialization improves the next
- **Preserve with Purpose**: Understand why existing content matters

## Success Criteria with Metacognitive Metrics

- **Analysis Quality**: Task tool provides multi-layered analysis with confidence scores
- **Decision Transparency**: Each decision includes "why" reasoning
- **Alternative Consideration**: Multiple approaches evaluated and documented
- **Confidence Calibration**: Confidence levels accurately reflect certainty
- **Preservation Intelligence**: Existing content understood and respected
- **Content Meaningfulness**: Generated content reflects deep understanding
- **User Alignment**: Initialization matches user's mental model
- **Future Readiness**: Setup anticipates project evolution
- **Learning Capture**: Process insights documented for improvement

## Metacognitive Reflection

This enhanced initialization process transforms a mechanical task into a thoughtful, self-aware system that:
- Understands not just what to do, but why
- Questions its own assumptions
- Learns from each initialization
- Provides transparency through confidence scoring
- Creates more meaningful and useful Memory Banks