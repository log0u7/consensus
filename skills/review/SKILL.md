# Code review heuristics

Focus on real defects, not style preferences:

- **Security**: injection (SQL, command, SSTI), auth bypass, insecure deserialization,
  secrets in code, path traversal, SSRF, unsafe crypto.
- **Correctness**: off-by-one errors, integer overflow, null/None dereference,
  race conditions, incorrect error handling, wrong assumptions about input ranges.
- **Resource leaks**: unclosed files, connections, sockets; missing finally/context managers.
- **Performance traps**: N+1 queries, unbounded memory growth, blocking I/O in async code.
- **Maintainability**: magic numbers, dead code, missing validation, overly complex logic.

Rate findings by severity: critical (exploitable/data loss) > high > medium > low.
Do not invent issues to appear thorough.
