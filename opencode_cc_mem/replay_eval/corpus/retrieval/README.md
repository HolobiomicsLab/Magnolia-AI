# Retrieval eval set (§6.5 use #3)

Query -> expected-memories pairs for recall@5/@10 + MRR on the embedding
model question (bge-m3 vs Qwen3-Embedding-4B vs EmbeddingGemma).

`pairs.jsonl`, one JSON object per line:

    {"id": "q001", "query": "...", "expected": ["<entry id or exact title>", ...]}

Rules:
- `expected` lists memory entries a correct retrieval MUST return in top-k
  (use entry ids from projects/*/.magnolia/entries/, or exact titles).
- Target 30-50 pairs before the first eval run; label where a decision is
  pending (human labels are finite — spend them on the comparison at hand).
- Like the rest of the corpus: frozen per version; add NEW pairs as a new
  version, never because an arm scored poorly on an existing one.
