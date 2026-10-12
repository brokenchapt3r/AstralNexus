---
name: rag-retrieval
description: Design and evaluate production RAG pipelines with parsing, chunking, hybrid retrieval, reranking, citations, and multimodal context.
platforms: [windows, linux, macos]
metadata:
  astralnexus:
    source: https://www.skills.sh/yonatangross/orchestkit/rag-retrieval
    install_command: npx skills add https://github.com/yonatangross/orchestkit --skill rag-retrieval
    adapter: curated
---

# RAG Retrieval

For enterprise knowledge-base tasks, separate ingestion, parsing, chunking,
indexing, query rewriting, hybrid retrieval, reranking, answer generation and
citation verification. Preserve document and section identifiers. Evaluate
retrieval recall, answer correctness, citation precision and hallucination
rate. Use multimodal parsing for scanned pages and tables; never claim a
retrieved fact without an evidence reference.
