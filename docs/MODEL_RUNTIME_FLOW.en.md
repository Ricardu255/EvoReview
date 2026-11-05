[简体中文](MODEL_RUNTIME_FLOW.md) | **English**

# EvoReview Runtime Flow

A review starts from an API request or a GitHub webhook, then goes through task orchestration, multi-agent collaboration, evidence gates, and report persistence.

```mermaid
flowchart TD
    A["API diff or GitHub PR"] --> B["Create review task and save diff"]
    B --> C["Planning: parse diff"]
    C --> D["Executing: rule scans and memory recall"]
    D --> E["Lead delegates assignments"]
    E --> F["Security / Correctness-Reliability review in parallel"]
    F --> G{"High risk?"}
    G -- Yes --> H["Lead assesses results; one worker revision if needed"]
    G -- No --> I["Merge candidate findings"]
    H --> I
    I --> J["Critic challenges findings"]
    J --> K["Lead makes final decision"]
    K --> L["Apply evidence gates and generate report"]
    L --> M["Save report and execution ledger"]
```

A finding enters the final report only after deterministic checks for location, evidence, confidence, and the extra requirements for high-risk findings. The `ExecutionLedger` keeps the role, token usage, cost, duration, and error for every model and tool call, so the run remains auditable.

A normal review does not automatically compare versions. Use a dedicated evaluation framework outside EvoReview when you need version-level quality assessment.
