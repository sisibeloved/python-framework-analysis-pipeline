"""Step 7: cross-platform diff analysis via Issue bridge.

Publishes per-function analysis issues to GitCode/GitHub. Each issue contains
source code and objdump -S machine code for both ARM and x86. An external LLM
service reads the issue and posts a structured Markdown comment with line-by-line
diff analysis, root cause summary, and optimization strategies.

This module handles: issue creation (publish) and comment parsing (fetch).
The external LLM service is NOT part of this project.
"""
