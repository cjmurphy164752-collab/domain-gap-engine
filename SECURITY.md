# Security and privacy

No hosted backend or remote model API is required. Inference calls the fixed local
Ollama endpoint. Official retrieval APIs contact their publishers; Telegram
delivery sends the bounded digest to your configured n8n endpoint and bot.

Keep local configuration, source exports, permission evidence, reports, databases,
and credentials outside Git. Use the default ignored `local/` directory. Generated
credential files use owner-only permissions on POSIX filesystems; Windows mounts
may apply different ACL semantics. Restrict the containing folder yourself.

The service binds only to loopback and requires a shared authentication header.
Do not expose it or Ollama directly to the Internet. Use HTTPS if you deliberately
configure a remote n8n endpoint. A local process with access to your configuration
can read the shared credential. Protect n8n's encryption key and database.

Never publish unredacted n8n exports from a configured private instance. This
generator emits unconfigured templates without credential IDs, tokens or chat IDs.
Do not share personal source documents through public GitHub issues.

Source text is untrusted data. The model has no tool execution capability. Verbatim
quote and citation checks constrain output but cannot prove semantic entailment
or eliminate prompt injection. Inspect evidence and counterevidence yourself.

Imports require explicit local-analysis rights declarations; the application cannot
verify ownership or license validity. Local analysis permission does not imply
redistribution, training rights, or permission to send full source text elsewhere.

Report suspected vulnerabilities through GitHub private vulnerability reporting
if enabled. Otherwise use a private maintainer contact before disclosing exploit
details; never include live credentials. No audit can guarantee absence of defects.
