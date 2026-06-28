# SRE / DevOps / Infra platform

Apply SRE principles when planning or reviewing infrastructure tasks:

- **Reliability**: design for failure; prefer idempotent operations.
- **Observability**: emit metrics, logs with context, and traces. Use structured logging.
- **Runbooks**: document every manual step; automate what is repeated.
- **IaC**: prefer declarative configuration (Ansible, Terraform) over ad-hoc scripts.
- **Security**: least privilege for service accounts and IAM roles; rotate secrets.
- **Rollback**: every change must be reversible; test the rollback procedure.
- **Capacity**: size for peak + 30%; alert before saturation, not after.
- **Dependencies**: pin versions; document external dependencies and their SLAs.
