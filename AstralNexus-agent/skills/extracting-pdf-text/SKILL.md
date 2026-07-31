---
name: extracting-pdf-text
description: Choose and verify PDF extraction and OCR workflows for text, tables, scans, and complex layouts.
platforms: [windows, linux, macos]
metadata:
  astralnexus:
    source: https://www.skills.sh/letta-ai/skills/extracting-pdf-text
    install_command: npx skills add https://github.com/letta-ai/skills --skill extracting-pdf-text
    adapter: curated
---

# PDF Extraction

Classify the PDF as born-digital, table-heavy, scanned, or complex layout
before choosing a parser. Prefer PyMuPDF for ordinary text, pdfplumber for
tables, and OCR for scans. Record page, block, table and confidence metadata;
validate extracted text against the source page before it becomes evidence.
