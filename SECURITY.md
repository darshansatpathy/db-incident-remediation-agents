# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/darshansatpathy/db-incident-remediation-agents/security/advisories/new)
for this repository. You should get an acknowledgement within three working days.

## Scope

This project is a reference implementation. The [threat model](docs/threat-model.md)
lists what it is designed to defend against and what is deliberately out of scope.

## Operating it safely

- Run `dbremedy setup` and give the agent only the two roles it creates; never a superuser.
- Start with `DBR_DRY_RUN=true` and read the reports before letting it act.
- Put the API behind a gateway that authenticates users and sets their roles, and set
  `DBR_SERVICE_TOKEN` and `DBR_WEBHOOK_TOKEN`.
- List every application that must never be interrupted (billing, backups, replication,
  migrations) under `protected` in the policy.
- Keep the approver group small, and review reports where actions ran with approval.
